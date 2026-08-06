"""Proof that the state-root mutation lease is a real single-writer lease.

The hard parts are proved with **real processes**, not mocks: a mock cannot
demonstrate that two ``python3`` interpreters serialize, that a ``SIGKILL``ed
holder's lock is reclaimed by the kernel, or that ``O_CLOEXEC`` survives an
``execve``. Mocking is used in exactly one place — forcing ``flock`` to be
unsupported — because there is no portable way to produce a filesystem without
lock support inside a unit test, and the fail-closed behaviour must still be
pinned.

Every test uses a **temporary** state root. Nothing here ever touches the real
``SKILLBOX_STATE_ROOT``: taking a real lease on a live box would block the
operator's sessions.

Run it the way the rest of the tree is run::

    python3 -m unittest tests.test_state_mutation_lock
"""

from __future__ import annotations

import errno
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import state_mutation as SM  # noqa: E402


#: A real mutating boundary from the inventory. The lease refuses anything else.
BOUNDARY = "manage.snap.create"
OTHER_BOUNDARY = "manage.focus"

_PREAMBLE = f"""
import json, os, signal, sys, time
sys.path.insert(0, {str(ENV_MANAGER_DIR)!r})
from runtime_manager import state_mutation as SM
"""


def _run_child(body: str, *args: str, timeout: float = 60.0, **popen_kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _PREAMBLE + body, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        **popen_kwargs,
    )


def _spawn_child(body: str, *args: str, **popen_kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _PREAMBLE + body, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen_kwargs,
    )


def _wait_for_file(path: Path, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text.strip():
            return text
        time.sleep(0.01)
    raise AssertionError(f"child never produced {path} within {timeout}s")


class LeaseTestCase(unittest.TestCase):
    """Temporary state roots only. Never the live one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="state-lease-test-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "a-state"
        self.root.mkdir()

    def make_root(self, name: str) -> Path:
        path = self.base / name
        path.mkdir()
        return path


# ==========================================================================
# Canonical root — the five-resolver ambiguity, resolved by refusing to guess
# ==========================================================================


class CanonicalRootTests(LeaseTestCase):
    def test_relative_root_is_refused_and_names_both_readings(self) -> None:
        previous = os.getcwd()
        os.chdir(self.base)  # the resolvers only fork when cwd != repo root
        try:
            with self.assertRaises(SM.StateMutationRootAmbiguous) as caught:
                SM.canonical_state_root(".skillbox-state")
        finally:
            os.chdir(previous)
        payload = caught.exception.payload()
        self.assertEqual(payload["code"], "STATE_LEASE_ROOT_AMBIGUOUS")
        self.assertEqual(payload["cwd_interpretation"], str(self.base / ".skillbox-state"))
        self.assertEqual(payload["repo_interpretation"], str(ROOT_DIR / ".skillbox-state"))
        self.assertNotEqual(payload["cwd_interpretation"], payload["repo_interpretation"])
        self.assertTrue(payload["cwd_relative_resolvers"])
        self.assertTrue(payload["repo_relative_resolvers"])

    def test_a_relative_root_is_refused_even_when_the_readings_agree(self) -> None:
        """Refusal is unconditional; it does not depend on today's cwd."""
        with self.assertRaises(SM.StateMutationRootAmbiguous):
            SM.canonical_state_root(".skillbox-state")

    def test_relative_root_is_accepted_with_an_explicit_base(self) -> None:
        resolved = SM.canonical_state_root("a-state", base=self.base)
        self.assertEqual(resolved, self.root)

    def test_relative_base_is_itself_refused(self) -> None:
        with self.assertRaises(SM.StateMutationRootInvalid):
            SM.canonical_state_root("a-state", base="relative/base")

    def test_empty_root_is_refused(self) -> None:
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaises(SM.StateMutationRootInvalid):
                    SM.canonical_state_root(value)

    def test_filesystem_root_has_no_sibling_and_is_refused(self) -> None:
        with self.assertRaises(SM.StateMutationRootInvalid):
            SM.canonical_state_root("/")

    def test_symlinked_and_dotted_spellings_collapse_to_one_lock(self) -> None:
        link = self.base / "link-state"
        link.symlink_to(self.root)
        dotted = self.base / "." / "a-state" / ".." / "a-state"
        spellings = [self.root, link, dotted, str(self.root) + "/"]
        locks = {str(SM.lease_lock_path(spelling)) for spelling in spellings}
        self.assertEqual(len(locks), 1, f"spellings disagreed on the lock path: {locks}")

    def test_lock_path_is_a_sibling_never_inside_the_root(self) -> None:
        lock = SM.lease_lock_path(self.root)
        self.assertEqual(lock.parent, self.root.parent)
        self.assertFalse(str(lock).startswith(str(self.root) + os.sep))
        self.assertTrue(lock.name.endswith(SM.LEASE_LOCK_SUFFIX))

    def test_root_need_not_exist(self) -> None:
        missing = self.base / "not-created-yet"
        self.assertEqual(SM.canonical_state_root(missing), missing)


# ==========================================================================
# Boundary welding — the lease consumes the inventory's IDs
# ==========================================================================


class BoundaryWeldTests(LeaseTestCase):
    def test_unknown_boundary_id_is_refused(self) -> None:
        with self.assertRaises(SM.StateMutationBoundaryError) as caught:
            with SM.state_mutation_lease(self.root, "manage.does-not-exist"):
                pass
        self.assertEqual(caught.exception.payload()["code"], "STATE_LEASE_BOUNDARY")

    def test_read_boundary_may_not_take_the_write_lease(self) -> None:
        reads = [entry for entry in SM.MANIFEST if entry.classification == SM.READ]
        self.assertTrue(reads, "inventory has no READ boundary to test with")
        with self.assertRaises(SM.StateMutationBoundaryError) as caught:
            with SM.state_mutation_lease(self.root, reads[0].boundary_id):
                pass
        self.assertIn("no read lock", str(caught.exception))

    def test_every_mutating_boundary_id_is_accepted(self) -> None:
        sample = [entry.boundary_id for entry in SM.mutations()][:12]
        for boundary_id in sample:
            with self.subTest(boundary=boundary_id):
                with SM.state_mutation_lease(self.root, boundary_id) as lease:
                    self.assertTrue(lease.held)


# ==========================================================================
# Real cross-process mutual exclusion
# ==========================================================================


_JOURNAL_CHILD = """
root, journal, boundary, hold, timeout = sys.argv[1:6]
with SM.state_mutation_lease(root, boundary, timeout=float(timeout)) as lease:
    with open(journal, "a") as fh:
        fh.write("ENTER %d %.6f\\n" % (os.getpid(), time.time()))
        fh.flush()
    time.sleep(float(hold))
    with open(journal, "a") as fh:
        fh.write("EXIT %d %.6f\\n" % (os.getpid(), time.time()))
        fh.flush()
print(json.dumps({"pid": os.getpid(), "acquired_at": lease.acquired_at}))
"""

_HOLD_UNTIL_SIGNALLED_CHILD = """
root, ready, release, boundary, timeout = sys.argv[1:6]
with SM.state_mutation_lease(root, boundary, timeout=float(timeout)) as lease:
    open(ready, "w").write(json.dumps({"pid": os.getpid(), "lock": str(lease.lock_path)}))
    deadline = time.time() + 60
    while time.time() < deadline and not os.path.exists(release):
        time.sleep(0.02)
print("released")
"""


class CrossProcessMutualExclusionTests(LeaseTestCase):
    def test_two_processes_never_hold_the_same_root_at_once(self) -> None:
        journal = self.base / "journal.txt"
        journal.touch()
        children = [
            _spawn_child(_JOURNAL_CHILD, str(self.root), str(journal), BOUNDARY, "0.35", "30")
            for _ in range(2)
        ]
        outs = [child.communicate(timeout=90) for child in children]
        for child, (out, err) in zip(children, outs):
            self.assertEqual(child.returncode, 0, f"child failed: {err}")
            self.assertIn("acquired_at", out)

        lines = [line.split() for line in journal.read_text().splitlines() if line.strip()]
        self.assertEqual(len(lines), 4, f"expected 4 journal lines, got {lines}")
        kinds = [line[0] for line in lines]
        pids = [line[1] for line in lines]
        self.assertEqual(kinds, ["ENTER", "EXIT", "ENTER", "EXIT"], f"holds interleaved: {lines}")
        self.assertEqual(pids[0], pids[1], "first holder exited after the second entered")
        self.assertEqual(pids[2], pids[3])
        self.assertNotEqual(pids[0], pids[2], "the same process ran twice")
        first_exit = float(lines[1][2])
        second_enter = float(lines[2][2])
        self.assertGreaterEqual(second_enter, first_exit, "second holder entered before the first exited")

    def test_distinct_roots_are_independent_across_processes(self) -> None:
        root_a = self.make_root("m-state")
        root_b = self.make_root("z-state")
        ready_a = self.base / "ready-a.json"
        ready_b = self.base / "ready-b.json"
        release = self.base / "release"
        child_a = _spawn_child(
            _HOLD_UNTIL_SIGNALLED_CHILD, str(root_a), str(ready_a), str(release), BOUNDARY, "30"
        )
        child_b = _spawn_child(
            _HOLD_UNTIL_SIGNALLED_CHILD, str(root_b), str(ready_b), str(release), BOUNDARY, "5"
        )
        try:
            info_a = json.loads(_wait_for_file(ready_a))
            info_b = json.loads(_wait_for_file(ready_b))
            self.assertNotEqual(info_a["lock"], info_b["lock"])
            self.assertNotEqual(info_a["pid"], info_b["pid"])
            # Both are genuinely held at the same instant.
            self.assertEqual(SM.read_lease_metadata(root_a)["kernel_holders"], [info_a["pid"]])
            self.assertEqual(SM.read_lease_metadata(root_b)["kernel_holders"], [info_b["pid"]])
        finally:
            release.touch()
            child_a.communicate(timeout=60)
            child_b.communicate(timeout=60)
        self.assertEqual((child_a.returncode, child_b.returncode), (0, 0))

    def test_lease_survives_the_root_being_renamed_out_from_under_it(self) -> None:
        """``state_backup.restore`` renames the root (``state_backup.py:850``)."""
        ready = self.base / "ready.json"
        release = self.base / "release"
        child = _spawn_child(
            _HOLD_UNTIL_SIGNALLED_CHILD, str(self.root), str(ready), str(release), BOUNDARY, "30"
        )
        try:
            holder = json.loads(_wait_for_file(ready))
            swapped = self.root.with_name("." + self.root.name + ".pre-restore-20260725")
            self.root.rename(swapped)
            self.addCleanup(lambda: swapped.exists() and swapped.rename(self.root))
            # The lock inode is untouched by the rename, so we still contend.
            with self.assertRaises(SM.StateMutationLeaseTimeout) as caught:
                with SM.state_mutation_lease(self.root, BOUNDARY, timeout=0.2):
                    pass
            self.assertEqual(caught.exception.payload()["holder"]["pid"], holder["pid"])
        finally:
            release.touch()
            child.communicate(timeout=60)


# ==========================================================================
# Bounded wait + the timeout payload
# ==========================================================================


class TimeoutPayloadTests(LeaseTestCase):
    def test_timeout_is_bounded_and_carries_the_full_forensics(self) -> None:
        ready = self.base / "ready.json"
        release = self.base / "release"
        child = _spawn_child(
            _HOLD_UNTIL_SIGNALLED_CHILD, str(self.root), str(ready), str(release), BOUNDARY, "30"
        )
        try:
            holder_info = json.loads(_wait_for_file(ready))
            started = time.monotonic()
            with self.assertRaises(SM.StateMutationLeaseTimeout) as caught:
                with SM.state_mutation_lease(
                    self.root, OTHER_BOUNDARY, operation_id="op-under-test", timeout=0.75
                ):
                    pass
            elapsed = time.monotonic() - started
            payload = caught.exception.payload()
            print("\nTIMEOUT PAYLOAD:\n" + json.dumps(payload, indent=2, sort_keys=True))

            self.assertEqual(payload["code"], "STATE_LEASE_TIMEOUT")
            self.assertEqual(payload["state_root"], str(self.root))
            self.assertEqual(payload["boundary_id"], OTHER_BOUNDARY)
            self.assertEqual(payload["operation_id"], "op-under-test")
            self.assertEqual(payload["lock_path"], str(SM.lease_lock_path(self.root)))
            self.assertGreaterEqual(payload["waited_seconds"], 0.75)
            self.assertLess(payload["waited_seconds"], 5.0)
            self.assertLess(elapsed, 5.0, "the wait was not bounded")

            holder = payload["holder"]
            self.assertEqual(holder["pid"], holder_info["pid"])
            self.assertTrue(holder["verified"], "holder was not confirmed against /proc/locks")
            self.assertEqual(holder["source"], "proc_locks")
            self.assertIsInstance(holder["start_ticks"], int)
            self.assertTrue(holder["command"], "no holder command recorded")
            self.assertTrue(holder["alive"])
            self.assertTrue(holder["advisory_matches_kernel"])
        finally:
            release.touch()
            child.communicate(timeout=60)

    def test_zero_timeout_fails_immediately_rather_than_blocking(self) -> None:
        ready = self.base / "ready.json"
        release = self.base / "release"
        child = _spawn_child(
            _HOLD_UNTIL_SIGNALLED_CHILD, str(self.root), str(ready), str(release), BOUNDARY, "30"
        )
        try:
            _wait_for_file(ready)
            started = time.monotonic()
            with self.assertRaises(SM.StateMutationLeaseTimeout) as caught:
                with SM.state_mutation_lease(self.root, BOUNDARY, timeout=0):
                    pass
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(caught.exception.payload()["timeout_seconds"], 0.0)
        finally:
            release.touch()
            child.communicate(timeout=60)

    def test_a_failed_acquisition_leaves_no_registry_residue(self) -> None:
        ready = self.base / "ready.json"
        release = self.base / "release"
        child = _spawn_child(
            _HOLD_UNTIL_SIGNALLED_CHILD, str(self.root), str(ready), str(release), BOUNDARY, "30"
        )
        try:
            _wait_for_file(ready)
            with self.assertRaises(SM.StateMutationLeaseTimeout):
                with SM.state_mutation_lease(self.root, BOUNDARY, timeout=0):
                    pass
            self.assertNotIn(str(self.root), SM.held_lease_roots())
        finally:
            release.touch()
            child.communicate(timeout=60)


# ==========================================================================
# Crash release
# ==========================================================================


class CrashReleaseTests(LeaseTestCase):
    def test_sigkill_releases_the_lease_and_leaves_stale_metadata(self) -> None:
        ready = self.base / "ready.json"
        release = self.base / "release"
        child = _spawn_child(
            _HOLD_UNTIL_SIGNALLED_CHILD, str(self.root), str(ready), str(release), BOUNDARY, "30"
        )
        holder = json.loads(_wait_for_file(ready))
        os.kill(child.pid, signal.SIGKILL)
        child.communicate(timeout=60)
        self.assertEqual(child.returncode, -signal.SIGKILL)

        # The metadata still LIES: it claims held, by a pid that no longer holds.
        before = SM.read_lease_metadata(self.root)
        self.assertEqual(before["state"], "held")
        self.assertEqual(before["metadata"]["pid"], holder["pid"])
        self.assertEqual(before["kernel_holders"], [], "kernel did not reclaim the flock")
        self.assertTrue(before["stale"])
        self.assertFalse(before["metadata_matches_kernel"])

        # And the kernel does not care about the lie: we acquire immediately.
        started = time.monotonic()
        with SM.state_mutation_lease(self.root, BOUNDARY, timeout=1.0) as lease:
            self.assertLess(time.monotonic() - started, 1.0)
            during = SM.read_lease_metadata(self.root)
            self.assertEqual(during["metadata"]["pid"], os.getpid())
            self.assertEqual(during["kernel_holders"], [os.getpid()])
            self.assertTrue(during["metadata_matches_kernel"])
            self.assertTrue(lease.held)

    def test_stale_metadata_is_replaced_only_after_acquisition(self) -> None:
        """A pre-existing lie must survive untouched while someone else holds."""
        lock = SM.lease_lock_path(self.root)
        lock.write_text(json.dumps({"state": "held", "pid": 999999, "marker": "planted"}))

        ready = self.base / "ready.json"
        release = self.base / "release"
        child = _spawn_child(
            _HOLD_UNTIL_SIGNALLED_CHILD, str(self.root), str(ready), str(release), BOUNDARY, "30"
        )
        try:
            _wait_for_file(ready)
            # The child acquired, so the planted lie is gone: replaced AFTER acquisition.
            self.assertNotEqual(SM.read_lease_metadata(self.root)["metadata"].get("marker"), "planted")

            # A loser must NOT touch the metadata on its way out.
            snapshot = lock.read_bytes()
            with self.assertRaises(SM.StateMutationLeaseTimeout):
                with SM.state_mutation_lease(self.root, BOUNDARY, timeout=0.1):
                    pass
            self.assertEqual(lock.read_bytes(), snapshot, "a losing waiter mutated the metadata")
        finally:
            release.touch()
            child.communicate(timeout=60)

    def test_the_lock_file_is_never_unlinked(self) -> None:
        lock = SM.lease_lock_path(self.root)
        with SM.state_mutation_lease(self.root, BOUNDARY):
            inode = lock.stat().st_ino
        self.assertTrue(lock.exists(), "release unlinked the lock file")
        self.assertEqual(lock.stat().st_ino, inode, "release replaced the lock inode")
        with SM.state_mutation_lease(self.root, BOUNDARY):
            self.assertEqual(lock.stat().st_ino, inode, "reacquire replaced the lock inode")

    def test_an_exception_inside_the_body_still_releases(self) -> None:
        with self.assertRaises(ZeroDivisionError):
            with SM.state_mutation_lease(self.root, BOUNDARY):
                raise ZeroDivisionError("boom")
        self.assertEqual(SM.held_lease_roots(), ())
        with SM.state_mutation_lease(self.root, BOUNDARY, timeout=0) as lease:
            self.assertTrue(lease.held)


# ==========================================================================
# Nesting
# ==========================================================================


class NestingTests(LeaseTestCase):
    def test_same_root_nesting_with_an_explicit_lease_reuses_the_kernel_lock(self) -> None:
        lock = SM.lease_lock_path(self.root)
        with SM.state_mutation_lease(self.root, BOUNDARY) as outer:
            outer_fd = outer._fd
            self.assertEqual(outer.depth, 1)
            with SM.state_mutation_lease(self.root, OTHER_BOUNDARY, lease=outer) as inner:
                self.assertIs(inner, outer)
                self.assertEqual(inner.depth, 2)
                self.assertEqual(inner._fd, outer_fd, "nesting opened a second descriptor")
                self.assertEqual(inner.boundary_id, OTHER_BOUNDARY)
                self.assertEqual(len(SM.held_lease_roots()), 1)
                owners = [owner["boundary_id"] for owner in inner.owners]
                self.assertEqual(owners, [BOUNDARY, OTHER_BOUNDARY])
                self.assertEqual(
                    SM.read_lease_metadata(self.root)["kernel_holders"], [os.getpid()]
                )
            self.assertEqual(outer.depth, 1)
            self.assertEqual(outer.boundary_id, BOUNDARY)
            self.assertTrue(outer.held, "the inner owner released the outer lease")
            self.assertEqual(SM.read_lease_metadata(self.root)["kernel_holders"], [os.getpid()])
        self.assertEqual(SM.read_lease_metadata(lock.parent / self.root.name)["state"], "released")
        self.assertEqual(SM.held_lease_roots(), ())

    def test_nested_owner_without_the_explicit_lease_is_refused(self) -> None:
        with SM.state_mutation_lease(self.root, BOUNDARY):
            with self.assertRaises(SM.StateMutationLeaseNesting) as caught:
                with SM.state_mutation_lease(self.root, OTHER_BOUNDARY, timeout=0):
                    pass
        payload = caught.exception.payload()
        self.assertEqual(payload["code"], "STATE_LEASE_NESTING")
        self.assertIn("MUST pass the held lease explicitly", payload["error"])
        self.assertEqual(payload["held_by"], BOUNDARY)

    def test_a_released_lease_cannot_be_presented_for_reuse(self) -> None:
        with SM.state_mutation_lease(self.root, BOUNDARY) as lease:
            pass
        with self.assertRaises(SM.StateMutationLeaseNesting):
            with SM.state_mutation_lease(self.root, BOUNDARY, lease=lease):
                pass

    def test_the_lease_is_not_shared_across_threads(self) -> None:
        errors: list[BaseException] = []
        started = threading.Event()
        finish = threading.Event()

        def hold() -> None:
            try:
                with SM.state_mutation_lease(self.root, BOUNDARY, timeout=10):
                    started.set()
                    finish.wait(20)
            except BaseException as exc:  # pragma: no cover - defensive
                errors.append(exc)
                started.set()

        worker = threading.Thread(target=hold)
        worker.start()
        try:
            self.assertTrue(started.wait(20))
            self.assertEqual(errors, [])
            with self.assertRaises(SM.StateMutationLeaseNesting) as caught:
                with SM.state_mutation_lease(self.root, BOUNDARY, timeout=0):
                    pass
            self.assertIn("another thread in this process holds", str(caught.exception))
        finally:
            finish.set()
            worker.join(20)

    def test_cross_root_nesting_is_allowed_in_ascending_canonical_order(self) -> None:
        root_a = self.make_root("aaa-state")
        root_b = self.make_root("bbb-state")
        self.assertLess(str(root_a), str(root_b))
        with SM.state_mutation_lease(root_a, BOUNDARY) as lease_a:
            with SM.state_mutation_lease(root_b, BOUNDARY) as lease_b:
                self.assertIsNot(lease_a, lease_b)
                self.assertNotEqual(lease_a.lock_path, lease_b.lock_path)
                self.assertEqual(len(SM.held_lease_roots()), 2)
                self.assertEqual(
                    SM.read_lease_metadata(root_a)["kernel_holders"], [os.getpid()]
                )
                self.assertEqual(
                    SM.read_lease_metadata(root_b)["kernel_holders"], [os.getpid()]
                )
        self.assertEqual(SM.held_lease_roots(), ())

    def test_cross_root_nesting_out_of_order_is_refused(self) -> None:
        root_a = self.make_root("aaa-state")
        root_b = self.make_root("bbb-state")
        with SM.state_mutation_lease(root_b, BOUNDARY):
            with self.assertRaises(SM.StateMutationLeaseOrder) as caught:
                with SM.state_mutation_lease(root_a, BOUNDARY, timeout=0):
                    pass
        payload = caught.exception.payload()
        self.assertEqual(payload["code"], "STATE_LEASE_LOCK_ORDER")
        self.assertEqual(payload["already_held"], [str(root_b)])
        self.assertIn("sorts strictly AFTER", payload["rule"])

    def test_presenting_another_roots_lease_still_obeys_the_order_rule(self) -> None:
        root_a = self.make_root("aaa-state")
        root_b = self.make_root("bbb-state")
        with SM.state_mutation_lease(root_b, BOUNDARY) as lease_b:
            with self.assertRaises(SM.StateMutationLeaseOrder):
                with SM.state_mutation_lease(root_a, BOUNDARY, lease=lease_b, timeout=0):
                    pass


# ==========================================================================
# Descriptor hygiene
# ==========================================================================


_FD_SCAN_CHILD = """
target = sys.argv[1]
found = []
for name in os.listdir("/proc/self/fd"):
    try:
        link = os.readlink("/proc/self/fd/" + name)
    except OSError:
        continue
    if link.split(" (deleted)")[0] == target:
        found.append(int(name))
print(json.dumps(found))
"""


class DescriptorHygieneTests(LeaseTestCase):
    def test_the_lock_descriptor_does_not_survive_exec(self) -> None:
        lock = str(SM.lease_lock_path(self.root))
        with SM.state_mutation_lease(self.root, BOUNDARY) as lease:
            self.assertIsNotNone(lease._fd)
            self.assertFalse(os.get_inheritable(lease._fd), "lock fd is marked inheritable")
            # close_fds=False deliberately: without O_CLOEXEC the fd WOULD leak here.
            result = _run_child(_FD_SCAN_CHILD, lock, close_fds=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            leaked = json.loads(result.stdout)
            self.assertEqual(leaked, [], f"lock descriptor leaked across execve as fd {leaked}")

    def test_a_child_cannot_take_the_lease_while_the_parent_holds_it(self) -> None:
        body = """
root, boundary = sys.argv[1:3]
try:
    with SM.state_mutation_lease(root, boundary, timeout=0.2):
        print(json.dumps({"acquired": True}))
except SM.StateMutationLeaseTimeout as exc:
    print(json.dumps({"acquired": False, "holder_pid": exc.payload()["holder"]["pid"]}))
"""
        with SM.state_mutation_lease(self.root, BOUNDARY):
            result = _run_child(body, str(self.root), BOUNDARY, close_fds=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["acquired"])
        self.assertEqual(report["holder_pid"], os.getpid())


# ==========================================================================
# Fail-closed when flock cannot be relied on
# ==========================================================================


class FailClosedTests(LeaseTestCase):
    """The one place mocking is unavoidable — and the behaviour that matters most."""

    def test_missing_fcntl_module_fails_closed(self) -> None:
        with mock.patch.object(SM, "fcntl", None):
            with self.assertRaises(SM.StateMutationLeaseUnsupported) as caught:
                with SM.state_mutation_lease(self.root, BOUNDARY):
                    pass
        self.assertEqual(caught.exception.payload()["code"], "STATE_LEASE_FLOCK_UNSUPPORTED")
        self.assertEqual(SM.held_lease_roots(), ())

    def test_unsupported_flock_errno_fails_closed_rather_than_proceeding(self) -> None:
        for code in (errno.ENOSYS, errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL, errno.EBADF):
            with self.subTest(errno=code):
                def boom(fd: int, _code: int = code) -> None:
                    raise OSError(_code, os.strerror(_code))

                with mock.patch.object(SM, "_flock_exclusive_nonblocking", boom):
                    with self.assertRaises(SM.StateMutationLeaseUnsupported) as caught:
                        with SM.state_mutation_lease(self.root, BOUNDARY, timeout=5):
                            pass
                self.assertEqual(caught.exception.payload()["errno"], code)
                self.assertEqual(SM.held_lease_roots(), ())

    def test_contention_errno_is_not_treated_as_unsupported(self) -> None:
        calls = {"n": 0}
        real = SM._flock_exclusive_nonblocking

        def busy_then_ok(fd: int) -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError(errno.EWOULDBLOCK, os.strerror(errno.EWOULDBLOCK))
            real(fd)

        with mock.patch.object(SM, "_flock_exclusive_nonblocking", busy_then_ok):
            with SM.state_mutation_lease(self.root, BOUNDARY, timeout=5) as lease:
                self.assertTrue(lease.held)
        self.assertGreaterEqual(calls["n"], 3)

    def test_an_unopenable_lock_path_fails_closed(self) -> None:
        blocked = self.base / "blocked"
        blocked.mkdir(mode=0o500)
        self.addCleanup(blocked.chmod, 0o700)
        if os.geteuid() == 0:
            self.skipTest("running as root; directory permissions are not enforced")
        with self.assertRaises(SM.StateMutationLeaseUnsupported):
            with SM.state_mutation_lease(blocked / "state", BOUNDARY):
                pass


# ==========================================================================
# Advisory metadata is advisory
# ==========================================================================


class AdvisoryMetadataTests(LeaseTestCase):
    def test_reads_take_no_lock_and_do_not_block_a_holder(self) -> None:
        ready = self.base / "ready.json"
        release = self.base / "release"
        child = _spawn_child(
            _HOLD_UNTIL_SIGNALLED_CHILD, str(self.root), str(ready), str(release), BOUNDARY, "30"
        )
        try:
            holder = json.loads(_wait_for_file(ready))
            started = time.monotonic()
            for _ in range(50):
                snapshot = SM.read_lease_metadata(self.root)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0, "reads appear to be taking a lock")
            self.assertEqual(snapshot["state"], "held")
            self.assertEqual(snapshot["kernel_holders"], [holder["pid"]])
            self.assertTrue(snapshot["advisory"])
            self.assertIn("ADVISORY ONLY", snapshot["authority"])
        finally:
            release.touch()
            child.communicate(timeout=60)

    def test_metadata_is_absent_before_any_acquisition(self) -> None:
        self.assertEqual(SM.read_lease_metadata(self.root)["state"], "absent")
        self.assertFalse(SM.lease_lock_path(self.root).exists())

    def test_garbage_metadata_is_tolerated_and_never_blocks(self) -> None:
        lock = SM.lease_lock_path(self.root)
        for payload in (b"", b"   ", b"{not json", b"[]", b"\xff\xfe\x00partial"):
            with self.subTest(payload=payload):
                lock.write_bytes(payload)
                self.assertIn(
                    SM.read_lease_metadata(self.root)["state"], {"empty", "unreadable"}
                )
                with SM.state_mutation_lease(self.root, BOUNDARY, timeout=1) as lease:
                    self.assertTrue(lease.held)

    def test_metadata_appears_only_after_acquisition(self) -> None:
        observed: list[str] = []
        real_acquire = SM._acquire_kernel_lock

        def spy(lease, *, timeout):
            observed.append(SM.read_lease_metadata(lease.state_root)["state"])
            return real_acquire(lease, timeout=timeout)

        with mock.patch.object(SM, "_acquire_kernel_lock", spy):
            with SM.state_mutation_lease(self.root, BOUNDARY):
                pass
        self.assertEqual(observed, ["absent"], "metadata existed before the flock was held")

    def test_no_public_clear_steal_or_break_operation_exists(self) -> None:
        banned = re.compile(r"(?i)(clear|steal|break|force|unlink|reset|release|revoke)")
        offenders = [name for name in SM.__all__ if banned.search(name)]
        self.assertEqual(offenders, [], f"lease exposes an ownership-overriding API: {offenders}")
        public = [
            name
            for name in dir(SM)
            if not name.startswith("_") and banned.search(name) and callable(getattr(SM, name))
        ]
        self.assertEqual(public, [])

    def test_the_lease_never_unlinks_and_never_reads_the_env(self) -> None:
        source = (ENV_MANAGER_DIR / "runtime_manager" / "state_mutation.py").read_text()
        lease_source = source.split("THE AUTHORITATIVE REENTRANT STATE-ROOT MUTATION LEASE", 1)[1]
        for forbidden in ("unlink(", "os.remove(", "shutil.rmtree(", "os.environ.get(", "getenv("):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, lease_source)

    def test_holder_command_is_redacted(self) -> None:
        argv = [
            "manage.py",
            "sync",
            "--token",
            "hunter2",
            "API_KEY=super-secret-value",
            "--path=/srv/skillbox/repos/opensource/skillbox/.skillbox-state",
            "sk-liveAAAAAAAAAAAAAAAAAAAAAAAA",
            "AKIAIOSFODNN7EXAMPLE",
            "0123456789abcdef0123456789abcdef",
            "plain-value",
        ]
        redacted = SM._redact_command(argv)
        joined = "\n".join(redacted)
        for secret in ("hunter2", "super-secret-value", "sk-liveAAAA", "AKIAIOSFODNN7EXAMPLE"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, joined)
        self.assertIn("sync", redacted)
        self.assertIn("plain-value", redacted)
        self.assertIn("--path=/srv/skillbox/repos/opensource/skillbox/.skillbox-state", redacted)
        # 4 whole-token masks: the swallowed --token value, the sk- prefix, the
        # AWS key id, and the opaque 32-char blob.
        self.assertEqual(redacted.count(SM.REDACTED), 4)
        # ...plus one key-preserving mask, so the shape of the command survives.
        self.assertIn(f"API_KEY={SM.REDACTED}", redacted)

    def test_annotations_are_redacted_before_they_reach_the_disk(self) -> None:
        with SM.state_mutation_lease(
            self.root,
            BOUNDARY,
            annotations={"reason": "restore drill", "do_password": "hunter2", "note": "ok"},
        ):
            metadata = SM.read_lease_metadata(self.root)["metadata"]
        self.assertEqual(metadata["annotations"]["reason"], "restore drill")
        self.assertEqual(metadata["annotations"]["do_password"], SM.REDACTED)
        self.assertNotIn("hunter2", json.dumps(metadata))

    def test_holder_description_is_best_effort_not_authority(self) -> None:
        lock = SM.lease_lock_path(self.root)
        holder = SM.describe_lease_holder(lock)
        self.assertFalse(holder["verified"])
        self.assertEqual(holder["source"], "unavailable")
        self.assertIn("never means the lock is free", holder["note"])

    def test_lease_payload_is_serialisable_and_carries_no_descriptor(self) -> None:
        with SM.state_mutation_lease(self.root, BOUNDARY) as lease:
            payload = lease.payload()
            json.dumps(payload)
            self.assertEqual(payload["state"], "held")
            self.assertEqual(payload["pid"], os.getpid())
            self.assertNotIn("fd", json.dumps(payload))
            self.assertTrue(lease.metadata_writable)
            self.assertGreaterEqual(lease.held_seconds(), 0.0)


if __name__ == "__main__":
    unittest.main()
