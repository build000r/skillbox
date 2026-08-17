"""Wiring tests for the ``manage env-inventory`` surface.

``environment_inventory`` itself is covered by ``tests/test_environment_inventory.py``
(versioning, redaction, stable ids, hot-path accounting). This file covers only
what wiring it to a CLI added, and each case is written against the way that
wiring could quietly come undone:

* the surface **splits into two leaves** — ``show`` reads, ``refresh`` writes —
  and both enumerators (the MANIFEST's and the contract linter's) see the split,
  which is the property a positional ``action`` would satisfy only halfway;
* the write is **gated by the state-root lease**, provably from another process;
* the write **refuses** rather than landing in an unleased root when the module's
  repo-relative cache path and ``canonical_runtime_state_root`` disagree;
* the read path **cannot write**, even by accident;
* the post-sync hook **honours ``--dry-run``** and reuses sync's held lease
  instead of taking a second one.

Every filesystem case runs against a throwaway root, so nothing here touches
this repo's real ``.skillbox-state``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"

if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import cli  # noqa: E402
from runtime_manager import command_contract  # noqa: E402
from runtime_manager import command_registry  # noqa: E402
from runtime_manager import environment_inventory as ei  # noqa: E402
from runtime_manager import state_mutation as sm  # noqa: E402

SHOW_BOUNDARY = "manage.env-inventory.show"
REFRESH_BOUNDARY = "manage.env-inventory.refresh"


def parse(argv: list[str]):
    return cli._build_parser().parse_args(argv)  # noqa: SLF001


class SurfaceSplitTests(unittest.TestCase):
    """One command, two leaves, and every enumerator agrees on the split."""

    def test_both_leaves_are_live_manage_surfaces(self) -> None:
        live = sm.enumerate_manage_surfaces(ROOT_DIR)
        self.assertIn("env-inventory show", live)
        self.assertIn("env-inventory refresh", live)
        self.assertNotIn("env-inventory", live)

    def test_show_is_a_read_and_refresh_is_a_mutation(self) -> None:
        self.assertFalse(sm.boundary(SHOW_BOUNDARY).is_mutation)
        self.assertTrue(sm.boundary(REFRESH_BOUNDARY).is_mutation)

    def test_the_read_leaf_claims_no_lock(self) -> None:
        """A read that names a lock is either mislabelled or over-serialized."""
        entry = sm.boundary(SHOW_BOUNDARY)
        self.assertEqual("n/a", entry.lock_owner)
        self.assertEqual("n/a", entry.lease_span)

    def test_the_write_leaf_names_the_real_lease(self) -> None:
        entry = sm.boundary(REFRESH_BOUNDARY)
        self.assertIn("state_mutation_lease", entry.lock_owner)
        self.assertIn(REFRESH_BOUNDARY, entry.lock_owner)
        self.assertEqual("environment_inventory.cache_rel", entry.state_root_source)
        self.assertIn(entry.state_root_source, sm.STATE_ROOT_SOURCES)

    def test_dispatch_gates_the_write_and_not_the_read(self) -> None:
        """Classified means gated: the dispatcher derives this from the MANIFEST."""
        self.assertIsNone(sm.manage_boundary_for(parse(["env-inventory"])))
        self.assertIsNone(sm.manage_boundary_for(parse(["env-inventory", "show"])))
        self.assertIsNone(
            sm.manage_boundary_for(parse(["env-inventory", "show", "--cached"]))
        )
        self.assertEqual(
            REFRESH_BOUNDARY, sm.manage_boundary_for(parse(["env-inventory", "refresh"]))
        )

    def test_sync_records_that_it_delegates_the_cache_write(self) -> None:
        """`sync` triggers the refresh; the refresh leaf owns it."""
        self.assertIn("manage.sync", {entry.boundary_id for entry in sm.MANIFEST})
        self.assertEqual(
            (REFRESH_BOUNDARY,), sm.boundary("manage.sync").delegates_to
        )

    def test_both_leaves_resolve_to_a_registry_spec(self) -> None:
        """Pins the parser SHAPE, not just its names.

        The contract linter walks real subparsers only. A positional ``action``
        with choices still decomposes for the MANIFEST, so the classification
        would look right while the registered write resolved to no live command
        at all. This test fails on that regression.
        """
        report = command_contract.build_report()
        live = {command.name for command in report.commands if command.surface == "runtime"}
        self.assertIn("env-inventory show", live)
        self.assertIn("env-inventory refresh", live)
        by_surface = {"runtime": live}
        for spec_id in ("runtime.env_inventory_show", "runtime.env_inventory_refresh"):
            with self.subTest(spec_id=spec_id):
                resolved, _matches = command_contract.resolve_registry_command(
                    spec_id, by_surface
                )
                self.assertIsNotNone(resolved, f"{spec_id} resolves to no live command")

    def test_the_registry_declares_the_read_as_side_effect_free(self) -> None:
        specs = {spec.id: spec for spec in command_registry.default_registry()}
        self.assertEqual("none", specs["runtime.env_inventory_show"].side_effect)
        self.assertEqual("local_write", specs["runtime.env_inventory_refresh"].side_effect)


class CacheRoundTripTests(unittest.TestCase):
    """The cache the whole bead exists for: something finally writes it."""

    def test_refresh_writes_a_cache_the_hot_path_can_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(ei.read_inventory_cache(root), "precondition: no cache")
            record = cli.refresh_environment_inventory_cache(root)
            self.assertTrue(record["ok"], record)
            self.assertTrue(record["wrote"])
            self.assertTrue(Path(record["path"]).is_file())
            payload = ei.read_inventory_cache(root)
            self.assertIsNotNone(payload)
            self.assertEqual(
                ei.ENVIRONMENT_INVENTORY_SCHEMA_VERSION, payload["schema_version"]
            )
            self.assertFalse(ei.is_stale(payload))

    def test_the_cache_lands_where_the_module_says_it_does(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = cli.refresh_environment_inventory_cache(root)
            self.assertEqual(str(ei.inventory_cache_path(root)), record["path"])

    def test_observation_is_opt_out_on_the_cache_path(self) -> None:
        """The cache exists to answer "what is actually here", so it observes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observed = cli.refresh_environment_inventory_cache(root)
            self.assertTrue(observed["observed"])
            self.assertTrue(
                (ei.read_inventory_cache(root)["freshness"])["observed"]
            )
            plain = cli.refresh_environment_inventory_cache(root, observe=False)
            self.assertFalse(plain["observed"])
            self.assertFalse(
                (ei.read_inventory_cache(root)["freshness"])["observed"]
            )


class GateTests(unittest.TestCase):
    """The write is serialized by the real lease, proved across processes."""

    HOLDER = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from runtime_manager import state_mutation as sm
        root = Path(sys.argv[2])
        ready = Path(sys.argv[3])
        with sm.runtime_mutation_lease("manage.sync", root_dir=root):
            ready.write_text("held", encoding="utf-8")
            time.sleep(float(sys.argv[4]))
        """
    ).strip()

    def test_a_concurrent_holder_blocks_the_refresh(self) -> None:
        """flock is per open file description, so this needs a real process."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "holder.py"
            script.write_text(self.HOLDER, encoding="utf-8")
            ready = root / "ready.marker"
            holder = subprocess.Popen(
                [sys.executable, str(script), str(ENV_MANAGER_DIR), str(root), str(ready), "6"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 20.0
                while not ready.is_file() and time.monotonic() < deadline:
                    if holder.poll() is not None:
                        self.fail(f"holder died: {holder.communicate()[1].decode()}")
                    time.sleep(0.02)
                self.assertTrue(ready.is_file(), "holder never acquired the lease")

                with self.assertRaises(sm.StateMutationLeaseError) as caught:
                    cli.refresh_environment_inventory_cache(root, lease_timeout=1.0)
            finally:
                holder.terminate()
                holder.communicate(timeout=20)

            self.assertIsNone(
                ei.read_inventory_cache(root),
                "the refusal must leave no cache behind",
            )
            # The holder is named, not just "busy" -- a caller that cannot get the
            # lease should learn who has it.
            self.assertEqual(REFRESH_BOUNDARY, caught.exception.context["boundary_id"])

    def test_a_held_lease_is_reused_not_taken_twice(self) -> None:
        """This is the `manage sync` path: sync holds, the hook writes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with sm.runtime_mutation_lease("manage.sync", root_dir=root):
                record = cli.refresh_environment_inventory_cache(root)
                self.assertEqual(
                    (str(sm.canonical_runtime_state_root(root)),),
                    tuple(str(item) for item in sm.held_lease_roots()),
                    "a second flock would mean two writers believing they are the one",
                )
            self.assertTrue(record["wrote"])
            self.assertEqual((), sm.held_lease_roots())


class StateRootMismatchTests(unittest.TestCase):
    """Fail closed when the two state-root resolvers name different places."""

    def setUp(self) -> None:
        self._saved = os.environ.get(sm.RUNTIME_STATE_ROOT_ENV)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(sm.RUNTIME_STATE_ROOT_ENV, None)
        else:
            os.environ[sm.RUNTIME_STATE_ROOT_ENV] = self._saved

    def test_refresh_refuses_when_the_state_root_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            elsewhere = Path(tmp) / "elsewhere"
            os.environ[sm.RUNTIME_STATE_ROOT_ENV] = str(elsewhere)

            record = cli.refresh_environment_inventory_cache(root)

            self.assertFalse(record["ok"])
            self.assertEqual("state_root_mismatch", record["reason"])
            self.assertFalse(record["wrote"])
            self.assertIsNone(ei.read_inventory_cache(root))
            self.assertFalse(
                (elsewhere / "inventory").exists(),
                "refusing means writing nowhere, not writing somewhere else",
            )

    def test_the_mismatch_check_reads_the_modules_own_constant(self) -> None:
        """So it cannot drift from the path the module actually writes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ.pop(sm.RUNTIME_STATE_ROOT_ENV, None)
            self.assertEqual(root, cli._env_inventory_cache_root(root))  # noqa: SLF001
            first_segment = Path(str(ei.INVENTORY_CACHE_REL)).parts[0]
            self.assertEqual(
                sm.canonical_runtime_state_root(root), (root / first_segment).resolve()
            )

    def test_cached_show_reports_the_mismatch_rather_than_an_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            os.environ[sm.RUNTIME_STATE_ROOT_ENV] = str(Path(tmp) / "elsewhere")
            args = parse(["env-inventory", "show", "--cached"])
            payload = cli._env_inventory_show_payload(args, root, ei)  # noqa: SLF001
            self.assertFalse(payload["ok"])
            self.assertEqual("state_root_mismatch", payload["reason"])
            self.assertIsNone(payload["cache_path"])


class ReadPathTests(unittest.TestCase):
    """The read leaf must stay a read."""

    def test_show_never_reaches_the_cache_writer(self) -> None:
        original = ei.write_inventory_cache

        def refuse(*_args, **_kwargs):
            raise AssertionError("the show path must not write the cache")

        with tempfile.TemporaryDirectory() as tmp:
            ei.write_inventory_cache = refuse  # type: ignore[assignment]
            try:
                payload = cli._env_inventory_show_payload(  # noqa: SLF001
                    parse(["env-inventory", "show"]), Path(tmp), ei
                )
            finally:
                ei.write_inventory_cache = original  # type: ignore[assignment]
            self.assertTrue(payload["ok"])
            self.assertEqual("build", payload["source"])
            self.assertEqual([], sorted(Path(tmp).iterdir()))

    def test_a_cache_miss_is_reported_not_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = cli._env_inventory_show_payload(  # noqa: SLF001
                parse(["env-inventory", "show", "--cached"]), Path(tmp), ei
            )
            self.assertFalse(payload["ok"])
            self.assertIsNone(payload["inventory"])
            self.assertTrue(payload["stale"])
            self.assertIn(
                "python3 .env-manager/manage.py env-inventory refresh --format json",
                payload["next_actions"],
            )
            self.assertIsNone(ei.read_inventory_cache(Path(tmp)))

    def test_a_stale_hit_is_still_a_hit(self) -> None:
        """The module's contract: render stale, do not block on a rebuild."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli.refresh_environment_inventory_cache(root, observe=False)
            path = ei.inventory_cache_path(root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["freshness"]["expires_at"] = 0.0
            path.write_text(ei.canonical_json(payload), encoding="utf-8")

            shown = cli._env_inventory_show_payload(  # noqa: SLF001
                parse(["env-inventory", "show", "--cached"]), root, ei
            )
            self.assertTrue(shown["ok"])
            self.assertTrue(shown["stale"])
            self.assertIsNotNone(shown["inventory"])
            self.assertTrue(shown["next_actions"])

    def test_a_foreign_schema_version_reads_as_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli.refresh_environment_inventory_cache(root, observe=False)
            path = ei.inventory_cache_path(root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = "1999-01-01+environment_inventory.v0"
            path.write_text(ei.canonical_json(payload), encoding="utf-8")

            shown = cli._env_inventory_show_payload(  # noqa: SLF001
                parse(["env-inventory", "show", "--cached"]), root, ei
            )
            self.assertFalse(shown["ok"])
            self.assertIsNone(shown["inventory"])


class SyncHookTests(unittest.TestCase):
    """The trigger: sync is where declared intent becomes filesystem reality."""

    def test_dry_run_previews_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = cli.refresh_environment_inventory_cache(root, dry_run=True)
            self.assertTrue(record["ok"])
            self.assertFalse(record["wrote"])
            self.assertEqual("dry_run", record["reason"])
            self.assertIn("would refresh", record["text"])
            self.assertIsNone(ei.read_inventory_cache(root))

    def test_the_record_is_shaped_like_every_other_sync_action(self) -> None:
        """`sync --format json` emits action OBJECTS; this must be one of them."""
        from runtime_manager.runtime_ops import normalize_action_record

        with tempfile.TemporaryDirectory() as tmp:
            record = cli.refresh_environment_inventory_cache(Path(tmp), dry_run=True)
            normalized = normalize_action_record(record)
            self.assertEqual(record["id"], normalized["id"])
            self.assertEqual(record["kind"], normalized["kind"])
            self.assertTrue(normalized["text"].strip())

    def test_a_build_failure_is_reported_not_raised(self) -> None:
        """A cache must never be able to fail a converge."""
        original = ei.build_environment_inventory

        def explode(*_args, **_kwargs):
            raise RuntimeError("synthetic config failure")

        with tempfile.TemporaryDirectory() as tmp:
            ei.build_environment_inventory = explode  # type: ignore[assignment]
            try:
                record = cli.refresh_environment_inventory_cache(Path(tmp))
            finally:
                ei.build_environment_inventory = original  # type: ignore[assignment]
            self.assertFalse(record["ok"])
            self.assertFalse(record["wrote"])
            self.assertIn("synthetic config failure", record["reason"])
            self.assertIsNone(ei.read_inventory_cache(Path(tmp)))

    def test_the_hook_is_wired_into_the_sync_handler(self) -> None:
        """Hooked at the `manage sync` handler, not inside sync_runtime.

        `up`, `bootstrap`, `restart`, `focus` and `doctor --fix` all call
        sync_runtime; none of them asked to own a cache write.
        """
        source = (ENV_MANAGER_DIR / "runtime_manager" / "cli.py").read_text(encoding="utf-8")
        handler = source.split("def _handle_sync(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("refresh_environment_inventory_cache(root_dir, dry_run=args.dry_run)", handler)
        runtime_ops = (ENV_MANAGER_DIR / "runtime_manager" / "runtime_ops.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("refresh_environment_inventory_cache", runtime_ops)


class ExitCodeTests(unittest.TestCase):
    """Exit codes a script can branch on."""

    def _run(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ENV_MANAGER_DIR / "manage.py"), "--root-dir", str(cwd), *argv],
            capture_output=True,
            text=True,
            cwd=str(ROOT_DIR),
            timeout=180,
        )

    def test_cache_miss_is_drift_and_a_hit_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            miss = self._run(["env-inventory", "show", "--cached", "--format", "json"], root)
            self.assertEqual(4, miss.returncode, miss.stderr)
            self.assertFalse(json.loads(miss.stdout)["ok"])

            wrote = self._run(["env-inventory", "refresh", "--format", "json"], root)
            self.assertEqual(0, wrote.returncode, wrote.stderr)
            self.assertTrue(json.loads(wrote.stdout)["cache"]["wrote"])

            hit = self._run(["env-inventory", "show", "--cached", "--format", "json"], root)
            self.assertEqual(0, hit.returncode, hit.stderr)
            payload = json.loads(hit.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("cache", payload["source"])


if __name__ == "__main__":
    unittest.main()
