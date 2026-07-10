"""Tests for the locked + atomic JSON state helpers in runtime_manager.shared.

Covers the race-class fix for shared mutable state files (.focus.json and
pulse.state.json):
  * atomic write survives a kill-9-style interruption (old-or-new, never torn);
  * a small concurrent writer/reader stress sees zero torn/lost updates;
  * lock contention always times out with StateLockTimeout (path + age);
  * flock-unsupported filesystems degrade to atomic-rename-only with a warning.

All tests are intentionally BOUNDED (small N, short timeouts) — wall time is a
few seconds at most.
"""
from __future__ import annotations

import io
import multiprocessing as mp
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import shared as SHARED  # noqa: E402
from runtime_manager.shared import (  # noqa: E402
    StateLockTimeout,
    atomic_write_json,
    locked_json_update,
    read_json_tolerant,
)


def _focus_worker(path_str: str, iterations: int, marker: int) -> None:
    """Child process: bump a per-writer counter, replacing the whole snapshot."""
    path = Path(path_str)

    def mutate(current):
        data = dict(current or {})
        counters = dict(data.get("counters") or {})
        counters[str(marker)] = int(counters.get(str(marker), 0)) + 1
        data["counters"] = counters
        data["client_id"] = f"writer-{marker}"
        return data

    for _ in range(iterations):
        locked_json_update(path, mutate, timeout=5.0)


def _reader_worker(path_str: str, iterations: int, torn_flag) -> None:
    """Child process: read repeatedly; flag if a torn/partial read is observed."""
    path = Path(path_str)
    for _ in range(iterations):
        value = read_json_tolerant(path, default=None)
        # A torn read would surface as a non-dict (None on a permanent decode
        # failure) once the file exists. read_json_tolerant should always
        # recover to a dict because every write is an atomic rename.
        if value is not None and not isinstance(value, dict):
            torn_flag.value = 1
        time.sleep(0)


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_json_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"client_id": "personal", "n": 1})
            self.assertEqual(read_json_tolerant(path), {"client_id": "personal", "n": 1})

    def test_interrupted_write_leaves_original_intact(self) -> None:
        """Simulate a kill-9 between temp-write and rename: original survives.

        We monkeypatch os.replace to raise so the rename never lands, then assert
        the file is still the OLD value (never a partial). Bounded loop.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"v": "old"})
            for i in range(10):
                with mock.patch.object(
                    SHARED.os, "replace", side_effect=RuntimeError("kill -9")
                ):
                    with self.assertRaises(RuntimeError):
                        atomic_write_json(path, {"v": f"new-{i}"})
                # Original is intact, and no stray temp file leaked.
                self.assertEqual(read_json_tolerant(path), {"v": "old"})
                leftover = [p for p in Path(tmp).iterdir() if p.name != "state.json"]
                self.assertEqual(leftover, [], f"temp leaked: {leftover}")

    def test_read_json_tolerant_missing_and_bad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            self.assertEqual(read_json_tolerant(path, default={"d": 1}), {"d": 1})
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(read_json_tolerant(path, default="fallback"), "fallback")


class LockedUpdateStressTests(unittest.TestCase):
    def test_concurrent_writers_and_readers_no_lost_updates(self) -> None:
        """4 writer + 2 reader processes, bounded iterations, zero lost updates.

        Each writer increments its own counter key N times. With correct
        locking the final per-writer count MUST equal N exactly (a lost update
        would leave it below N). Readers assert they never see a torn file.
        """
        writers = 4
        readers = 2
        iterations = 20
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".focus.json"
            atomic_write_json(path, {"counters": {}})

            ctx = mp.get_context("spawn")
            torn_flag = ctx.Value("i", 0)
            procs = []
            for marker in range(writers):
                procs.append(
                    ctx.Process(target=_focus_worker, args=(str(path), iterations, marker))
                )
            for _ in range(readers):
                procs.append(
                    ctx.Process(target=_reader_worker, args=(str(path), iterations * 4, torn_flag))
                )

            start = time.monotonic()
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=20)
                self.assertIsNotNone(proc.exitcode, "worker process hung")
                self.assertEqual(proc.exitcode, 0, "worker process failed")
            elapsed = time.monotonic() - start

            self.assertEqual(torn_flag.value, 0, "a reader observed a torn read")
            final = read_json_tolerant(path)
            counters = final.get("counters") or {}
            for marker in range(writers):
                self.assertEqual(
                    counters.get(str(marker)),
                    iterations,
                    f"writer {marker} lost updates: {counters}",
                )
            # Sanity: this should be quick even on a loaded box.
            self.assertLess(elapsed, 20.0)

    def test_threaded_writers_consistent(self) -> None:
        """In-process threads exercise the same lock without lost updates."""
        threads_n = 4
        iterations = 20
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".focus.json"
            atomic_write_json(path, {"counters": {}})

            def run(marker: int) -> None:
                def mutate(current):
                    data = dict(current or {})
                    counters = dict(data.get("counters") or {})
                    counters[str(marker)] = int(counters.get(str(marker), 0)) + 1
                    data["counters"] = counters
                    return data

                for _ in range(iterations):
                    locked_json_update(path, mutate, timeout=5.0)

            threads = [threading.Thread(target=run, args=(m,)) for m in range(threads_n)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive(), "writer thread hung")

            counters = read_json_tolerant(path).get("counters") or {}
            for marker in range(threads_n):
                self.assertEqual(counters.get(str(marker)), iterations)


class LockTimeoutTests(unittest.TestCase):
    def test_held_lock_times_out_with_path_and_age(self) -> None:
        """A second update raises StateLockTimeout carrying lock path + age."""
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".focus.json"
            atomic_write_json(path, {"v": 0})
            lock_path = path.with_name(path.name + ".lock")

            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                start = time.monotonic()
                with self.assertRaises(StateLockTimeout) as ctx:
                    locked_json_update(path, lambda cur: cur, timeout=0.2)
                elapsed = time.monotonic() - start
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

            exc = ctx.exception
            self.assertEqual(exc.lock_path, lock_path)
            self.assertIsNotNone(exc.age_seconds)
            self.assertGreaterEqual(exc.age_seconds, 0.0)
            self.assertIn(str(lock_path), str(exc))
            # It timed out promptly (bounded), never blocked forever.
            self.assertGreaterEqual(elapsed, 0.2)
            self.assertLess(elapsed, 3.0)


class FlockDegradeTests(unittest.TestCase):
    def setUp(self) -> None:
        # The one-time warning latch is module global; reset it per test.
        SHARED._flock_unsupported_warned = False

    def tearDown(self) -> None:
        SHARED._flock_unsupported_warned = False

    def test_flock_unsupported_degrades_with_one_time_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".focus.json"
            buf = io.StringIO()
            with redirect_stderr(buf):
                with mock.patch.object(
                    SHARED.fcntl, "flock", side_effect=OSError("flock unsupported")
                ):
                    # Write still succeeds via atomic-rename-only.
                    locked_json_update(path, lambda cur: {"v": "first"})
                    locked_json_update(path, lambda cur: {"v": "second"})

            self.assertEqual(read_json_tolerant(path), {"v": "second"})
            warning = buf.getvalue()
            self.assertIn("flock unsupported", warning)
            self.assertIn("atomic-rename-only", warning)
            # One-time: the warning text appears exactly once across two writes.
            self.assertEqual(warning.count("atomic-rename-only"), 1)


if __name__ == "__main__":
    unittest.main()
