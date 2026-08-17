"""Concurrency and I/O contract tests for the Oracle browser receipt store.

The defect these exist for: a launcher that cleared the receipt with
``os.unlink`` left ``browser.json`` absent for the whole of a browser launch, so
concurrent ``status`` readers reported ``browser_receipt_invalid`` about a
session that was perfectly healthy.

The headline test is the bead's own validation — parallel readers hammering the
receipt while a launcher rewrite runs must never see ``browser_receipt_invalid``
— and it is paired with a negative control that runs the *same harness* against
the old unlink-then-write writer and requires it to fail. A "we never saw the
error" test is worthless if the harness could not have seen it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.oracle_broker import OracleBrokerError  # noqa: E402
from runtime_manager.oracle_receipt_store import (  # noqa: E402
    DEFAULT_READ_RETRIES,
    INVALIDATION_REASONS,
    MAX_RECEIPT_BYTES,
    ORACLE_BROWSER_RECEIPT_SCHEMA,
    ORACLE_BROWSER_TEST_RECEIPT_SCHEMA,
    RECEIPT_FILENAME,
    REFUSAL_CODES,
    STATE_INVALIDATED,
    STATE_READY,
    OracleReceiptStoreError,
    invalidate_receipt,
    publish_receipt,
    read_receipt,
    receipt_path,
    receipt_state,
)

#: A reader process: exactly what `auth status --json` does in a loop, reduced
#: to the one thing under test. Prints a JSON tally of outcome codes.
READER_WORKER = """
import json
import sys
sys.path.insert(0, {env_manager!r})
import time
from runtime_manager.oracle_receipt_store import read_receipt, OracleReceiptStoreError

root, cycles, pause = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
tally = {{"ok": 0}}
for _ in range(cycles):
    try:
        read_receipt(root)
        tally["ok"] += 1
    except OracleReceiptStoreError as error:
        tally[error.code] = tally.get(error.code, 0) + 1
    time.sleep(pause)
print(json.dumps(tally))
"""

READY_RECEIPT = {
    "schema": ORACLE_BROWSER_RECEIPT_SCHEMA,
    "state": STATE_READY,
    "pid": 4242,
    "port": 9222,
}


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "runtime"
        self.root.mkdir(parents=True)
        os.chmod(self.root, 0o700)

    def assert_refused(self, code: str, action: object) -> OracleReceiptStoreError:
        with self.assertRaises(OracleReceiptStoreError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def readers(
        self, count: int, cycles: int, pause: float
    ) -> list[subprocess.Popen[str]]:
        script = READER_WORKER.format(env_manager=str(ENV_MANAGER_DIR))
        return [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(self.root),
                    str(cycles),
                    str(pause),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(count)
        ]

    def collect(self, processes: list[subprocess.Popen[str]]) -> dict[str, int]:
        totals: dict[str, int] = {}
        for process in processes:
            stdout, stderr = process.communicate(timeout=180)
            self.assertEqual(0, process.returncode, stderr)
            for code, count in json.loads(stdout).items():
                totals[code] = totals.get(code, 0) + count
        return totals


class PublishTests(StoreTestCase):
    """Publication is atomic: a reader sees the old receipt or the new one."""

    def test_publish_then_read_round_trips(self) -> None:
        publish_receipt(self.root, READY_RECEIPT)
        self.assertEqual(READY_RECEIPT, read_receipt(self.root))
        self.assertEqual(STATE_READY, receipt_state(self.root))

    def test_the_published_receipt_is_private(self) -> None:
        target = publish_receipt(self.root, READY_RECEIPT)
        self.assertEqual(receipt_path(self.root), target)
        self.assertEqual(RECEIPT_FILENAME, target.name)
        self.assertEqual(0o600, stat.S_IMODE(os.stat(target).st_mode))

    def test_publication_leaves_no_temporary_files(self) -> None:
        for _ in range(5):
            publish_receipt(self.root, READY_RECEIPT)
        self.assertEqual(
            [RECEIPT_FILENAME], sorted(entry.name for entry in self.root.iterdir())
        )

    def test_publication_replaces_the_inode_rather_than_truncating(self) -> None:
        # Truncate-then-write is the other way to expose a partial read; a
        # replace means the old inode stays whole until the swap.
        first = publish_receipt(self.root, READY_RECEIPT)
        before = os.stat(first).st_ino
        publish_receipt(self.root, {**READY_RECEIPT, "pid": 4243})
        self.assertNotEqual(before, os.stat(first).st_ino)

    def test_the_test_schema_is_accepted(self) -> None:
        publish_receipt(
            self.root,
            {"schema": ORACLE_BROWSER_TEST_RECEIPT_SCHEMA, "state": "test_ready"},
        )
        self.assertEqual("test_ready", receipt_state(self.root))

    def test_malformed_documents_are_refused(self) -> None:
        for document in (
            None,
            [],
            "receipt",
            {"state": STATE_READY},
            {"schema": "other.v1", "state": STATE_READY},
            {"schema": ORACLE_BROWSER_RECEIPT_SCHEMA},
            {"schema": ORACLE_BROWSER_RECEIPT_SCHEMA, "state": ""},
            {"schema": ORACLE_BROWSER_RECEIPT_SCHEMA, "state": 7},
            {"schema": ORACLE_BROWSER_RECEIPT_SCHEMA, "state": "x" * 65},
            {"schema": ORACLE_BROWSER_RECEIPT_SCHEMA, "state": STATE_READY, "n": float("nan")},
        ):
            self.assert_refused(
                "browser_receipt_invalid",
                lambda document=document: publish_receipt(self.root, document),
            )

    def test_an_oversize_receipt_is_refused(self) -> None:
        self.assert_refused(
            "browser_receipt_invalid",
            lambda: publish_receipt(
                self.root,
                {**READY_RECEIPT, "padding": "x" * (MAX_RECEIPT_BYTES + 1)},
            ),
        )

    def test_the_runtime_root_must_be_a_private_directory(self) -> None:
        missing = self.root.parent / "absent"
        self.assert_refused(
            "receipt_root_invalid", lambda: publish_receipt(missing, READY_RECEIPT)
        )
        self.assert_refused(
            "receipt_root_invalid", lambda: publish_receipt(None, READY_RECEIPT)
        )
        link = self.root.parent / "linked"
        link.symlink_to(self.root)
        self.assert_refused(
            "receipt_root_invalid", lambda: publish_receipt(link, READY_RECEIPT)
        )
        os.chmod(self.root, 0o755)
        self.assert_refused(
            "wrong_permissions", lambda: publish_receipt(self.root, READY_RECEIPT)
        )


class InvalidateTests(StoreTestCase):
    """Retiring a receipt publishes the truth; it never removes the file."""

    def test_invalidate_publishes_a_not_ready_receipt(self) -> None:
        publish_receipt(self.root, READY_RECEIPT)
        invalidate_receipt(self.root, "launcher_restart")
        document = read_receipt(self.root)
        self.assertEqual(STATE_INVALIDATED, document["state"])
        self.assertEqual("launcher_restart", document["reason"])
        self.assertEqual(ORACLE_BROWSER_RECEIPT_SCHEMA, document["schema"])

    def test_a_prior_ready_receipt_is_no_longer_evidence(self) -> None:
        # The launcher's real requirement: a previous ready receipt must not
        # vouch for the next invocation. Publishing 'invalidated' satisfies it.
        publish_receipt(self.root, READY_RECEIPT)
        invalidate_receipt(self.root, "launcher_restart")
        self.assertNotEqual(STATE_READY, receipt_state(self.root))

    def test_the_receipt_file_never_disappears(self) -> None:
        publish_receipt(self.root, READY_RECEIPT)
        target = receipt_path(self.root)
        missing: list[float] = []
        stop = threading.Event()

        def watch() -> None:
            while not stop.is_set():
                if not target.exists():
                    missing.append(time.monotonic())

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            for _ in range(50):
                invalidate_receipt(self.root, "launcher_restart")
                publish_receipt(self.root, READY_RECEIPT)
        finally:
            stop.set()
            watcher.join(timeout=5)
        self.assertEqual([], missing)

    def test_invalidation_reasons_are_a_closed_set(self) -> None:
        for reason in sorted(INVALIDATION_REASONS):
            invalidate_receipt(self.root, reason)
            self.assertEqual(reason, read_receipt(self.root)["reason"])
        for reason in ("because", "", None, "/tmp/host/path"):
            self.assert_refused(
                "browser_receipt_invalid",
                lambda reason=reason: invalidate_receipt(self.root, reason),
            )

    def test_an_unknown_schema_cannot_be_invalidated_into_existence(self) -> None:
        self.assert_refused(
            "browser_receipt_invalid",
            lambda: invalidate_receipt(self.root, "launcher_restart", schema="x.v1"),
        )


class ReadFailureTests(StoreTestCase):
    """Honest refusals for standing faults; retries only for races."""

    def test_a_corrupt_receipt_is_invalid_after_retries(self) -> None:
        receipt_path(self.root).write_bytes(b"{not json")
        os.chmod(receipt_path(self.root), 0o600)
        self.assert_refused(
            "browser_receipt_invalid", lambda: read_receipt(self.root)
        )

    def test_a_group_readable_receipt_is_a_permissions_fault(self) -> None:
        publish_receipt(self.root, READY_RECEIPT)
        os.chmod(receipt_path(self.root), 0o644)
        self.assert_refused("wrong_permissions", lambda: read_receipt(self.root))

    def test_a_symlinked_receipt_is_a_permissions_fault(self) -> None:
        publish_receipt(self.root, READY_RECEIPT)
        target = receipt_path(self.root)
        elsewhere = self.root / "elsewhere.json"
        elsewhere.write_bytes(target.read_bytes())
        os.chmod(elsewhere, 0o600)
        target.unlink()
        target.symlink_to(elsewhere)
        self.assert_refused("wrong_permissions", lambda: read_receipt(self.root))

    def test_a_permissions_fault_is_not_retried(self) -> None:
        # Retrying a standing fault would only delay an honest answer, so the
        # backoff must never be paid for it.
        publish_receipt(self.root, READY_RECEIPT)
        os.chmod(receipt_path(self.root), 0o644)
        started = time.monotonic()
        self.assert_refused(
            "wrong_permissions",
            lambda: read_receipt(self.root, retries=4, backoff_seconds=0.25),
        )
        self.assertLess(time.monotonic() - started, 0.25)

    def test_read_arguments_are_validated(self) -> None:
        publish_receipt(self.root, READY_RECEIPT)
        for kwargs in (
            {"retries": -1},
            {"retries": 9},
            {"retries": True},
            {"retries": "1"},
            {"backoff_seconds": -0.1},
            {"backoff_seconds": 2},
            {"backoff_seconds": True},
        ):
            self.assert_refused(
                "browser_receipt_invalid",
                lambda kwargs=kwargs: read_receipt(self.root, **kwargs),
            )


class RetryTests(StoreTestCase):
    """A reader that races a publication still gets the truth."""

    def test_a_receipt_that_appears_late_is_read_on_the_retry(self) -> None:
        publisher = threading.Timer(
            0.05, publish_receipt, args=(self.root, READY_RECEIPT)
        )
        publisher.start()
        self.addCleanup(publisher.cancel)
        document = read_receipt(self.root, retries=8, backoff_seconds=0.05)
        self.assertEqual(READY_RECEIPT, document)

    def test_without_retries_the_same_race_fails(self) -> None:
        # Proves the retry is load-bearing rather than incidental.
        self.assert_refused(
            "browser_receipt_invalid",
            lambda: read_receipt(self.root, retries=0, backoff_seconds=0),
        )

    def test_a_missing_receipt_still_fails_once_retries_are_spent(self) -> None:
        self.assert_refused(
            "browser_receipt_invalid",
            lambda: read_receipt(self.root, retries=1, backoff_seconds=0),
        )

    def test_the_default_offers_exactly_one_retry(self) -> None:
        self.assertEqual(1, DEFAULT_READ_RETRIES)


class ConcurrencyRegressionTests(StoreTestCase):
    """The bead's validation: readers must never see browser_receipt_invalid."""

    READERS = 6
    CYCLES = 20
    PAUSE = 0.02

    def churn(self, stop: threading.Event, legacy: bool) -> threading.Thread:
        """Loop a launcher-style rewrite until told to stop."""
        target = receipt_path(self.root)

        def run() -> None:
            while not stop.is_set():
                if legacy:
                    # The original defect, reproduced exactly: unlink, fsync the
                    # directory, and only publish once the "browser" is up. The
                    # window is a whole launch in production; 0.25s here.
                    try:
                        os.unlink(target)
                    except FileNotFoundError:
                        pass
                    directory = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                    stop.wait(0.25)
                else:
                    invalidate_receipt(self.root, "launcher_restart")
                    stop.wait(0.02)
                publish_receipt(self.root, READY_RECEIPT)
                stop.wait(0.02)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def run_harness(self, *, legacy: bool) -> dict[str, int]:
        publish_receipt(self.root, READY_RECEIPT)
        processes = self.readers(self.READERS, self.CYCLES, self.PAUSE)
        stop = threading.Event()
        writer = self.churn(stop, legacy)
        try:
            totals = self.collect(processes)
        finally:
            stop.set()
            writer.join(timeout=10)
        return totals

    def test_parallel_readers_never_see_an_invalid_receipt_during_a_rewrite(
        self,
    ) -> None:
        totals = self.run_harness(legacy=False)
        self.assertNotIn("browser_receipt_invalid", totals, totals)
        self.assertNotIn("wrong_permissions", totals, totals)
        self.assertEqual(self.READERS * self.CYCLES, totals.get("ok"), totals)

    def test_every_read_during_a_rewrite_returns_a_complete_receipt(self) -> None:
        # Not just "no error": every observed document must be one of the two
        # real states, never a half-written one.
        publish_receipt(self.root, READY_RECEIPT)
        stop = threading.Event()
        writer = self.churn(stop, legacy=False)
        states: set[str] = set()
        try:
            for _ in range(200):
                states.add(receipt_state(self.root))
        finally:
            stop.set()
            writer.join(timeout=10)
        self.assertTrue(states <= {STATE_READY, STATE_INVALIDATED}, states)


class NegativeControlTests(ConcurrencyRegressionTests):
    """The harness must be able to catch the bug it claims is gone."""

    def test_the_legacy_unlink_window_is_deterministically_visible(self) -> None:
        # No timing at all: this is the exact state the old launcher left the
        # directory in for the duration of a browser launch.
        publish_receipt(self.root, READY_RECEIPT)
        os.unlink(receipt_path(self.root))
        self.assert_refused("browser_receipt_invalid", lambda: read_receipt(self.root))

    def test_the_harness_detects_a_legacy_unlink_then_write_writer(self) -> None:
        totals = self.run_harness(legacy=True)
        self.assertGreater(
            totals.get("browser_receipt_invalid", 0),
            0,
            f"harness saw no failure against the legacy writer: {totals}",
        )

    def test_parallel_readers_never_see_an_invalid_receipt_during_a_rewrite(
        self,
    ) -> None:
        self.skipTest("inherited positive case; asserted on the fixed writer")

    def test_every_read_during_a_rewrite_returns_a_complete_receipt(self) -> None:
        self.skipTest("inherited positive case; asserted on the fixed writer")


class ContractTests(StoreTestCase):
    """Invariants that keep this store aligned with its neighbours."""

    def test_refusal_codes_are_declared(self) -> None:
        source = (
            ENV_MANAGER_DIR / "runtime_manager" / "oracle_receipt_store.py"
        ).read_text(encoding="utf-8")
        import re

        used = set(re.findall(r'_refuse\("([a-z_]+)"\)', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - REFUSAL_CODES)

    def test_the_reader_code_matches_the_js_doctor(self) -> None:
        # `browser_receipt_invalid` and `wrong_permissions` are the codes the JS
        # doctor already emits; the port must not invent new spellings.
        self.assertIn("browser_receipt_invalid", REFUSAL_CODES)
        self.assertIn("wrong_permissions", REFUSAL_CODES)

    def test_refusals_share_the_oracle_error_surface(self) -> None:
        error = self.assert_refused(
            "browser_receipt_invalid", lambda: read_receipt(self.root, retries=0)
        )
        self.assertIsInstance(error, OracleBrokerError)
        self.assertEqual("browser_receipt_invalid", error.to_payload()["error_code"])

    def test_the_store_never_unlinks_the_receipt(self) -> None:
        # The whole defect in one assertion: the module must contain no unlink
        # of the receipt path. Only the temp file may be removed, on failure.
        source = (
            ENV_MANAGER_DIR / "runtime_manager" / "oracle_receipt_store.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("os.unlink(target)", source)
        self.assertNotIn("os.unlink(receipt", source)
        self.assertIn("os.unlink(temporary)", source)


if __name__ == "__main__":
    unittest.main()
