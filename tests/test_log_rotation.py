from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager._shared import events  # noqa: E402

PULSE_MODULE = SourceFileLoader(
    "skillbox_pulse_rotation",
    str((ENV_MANAGER_DIR / "pulse.py").resolve()),
).load_module()

DAY_SECONDS = 86400.0


class RotateLogFileTests(unittest.TestCase):
    def test_below_threshold_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "runtime.log"
            log_path.write_text("small\n", encoding="utf-8")
            self.assertFalse(events.rotate_log_file(log_path, max_bytes=1024, keep=3))
            self.assertEqual(log_path.read_text(encoding="utf-8"), "small\n")
            self.assertFalse((Path(tmpdir) / "runtime.log.1").exists())

    def test_missing_file_and_disabled_limits_are_noops(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "runtime.log"
            self.assertFalse(events.rotate_log_file(log_path, max_bytes=1, keep=3))
            log_path.write_text("x" * 64, encoding="utf-8")
            self.assertFalse(events.rotate_log_file(log_path, max_bytes=0, keep=3))
            self.assertFalse(events.rotate_log_file(log_path, max_bytes=1, keep=0))

    def test_rotation_shifts_archives_and_caps_at_keep(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "runtime.log"
            for generation in ("first", "second", "third", "fourth"):
                log_path.write_text(f"{generation}:" + "x" * 64, encoding="utf-8")
                self.assertTrue(events.rotate_log_file(log_path, max_bytes=32, keep=2))
                self.assertFalse(log_path.exists())

            archive_1 = (Path(tmpdir) / "runtime.log.1").read_text(encoding="utf-8")
            archive_2 = (Path(tmpdir) / "runtime.log.2").read_text(encoding="utf-8")
            self.assertTrue(archive_1.startswith("fourth:"))
            self.assertTrue(archive_2.startswith("third:"))
            self.assertFalse((Path(tmpdir) / "runtime.log.3").exists())


class LogRuntimeEventRotationTests(unittest.TestCase):
    def test_log_runtime_event_rotates_once_past_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_path = root / events.RUNTIME_LOG_REL
            log_path.parent.mkdir(parents=True)
            log_path.write_text("x" * 256, encoding="utf-8")

            with mock.patch.object(events, "RUNTIME_LOG_MAX_BYTES", 128):
                events.log_runtime_event("test.event", "subject", {"k": "v"}, root)

            rotated = log_path.with_name("runtime.log.1")
            self.assertTrue(rotated.is_file())
            self.assertEqual(rotated.read_text(encoding="utf-8"), "x" * 256)
            fresh = log_path.read_text(encoding="utf-8")
            self.assertIn("test.event subject", fresh)
            self.assertLess(len(fresh), 256)

    def test_log_runtime_event_appends_without_rotation_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            events.log_runtime_event("test.event", "subject", None, root)
            events.log_runtime_event("test.event2", "subject", None, root)
            log_path = root / events.RUNTIME_LOG_REL
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertFalse(log_path.with_name("runtime.log.1").exists())


class PulseLogRotationTests(unittest.TestCase):
    def test_open_log_rotates_oversized_pulse_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "pulse.log"
            log_path.write_text("y" * 64, encoding="utf-8")
            with (
                mock.patch.object(PULSE_MODULE, "pulse_log_path", return_value=log_path),
                mock.patch.object(PULSE_MODULE, "RUNTIME_LOG_MAX_BYTES", 32),
                mock.patch.object(
                    PULSE_MODULE,
                    "rotate_log_file",
                    side_effect=lambda path: events.rotate_log_file(path, max_bytes=32, keep=3),
                ),
            ):
                PULSE_MODULE._open_log(Path(tmpdir))
            try:
                self.assertTrue((Path(tmpdir) / "pulse.log.1").is_file())
                PULSE_MODULE.log("info", "post-rotation entry")
                self.assertIn("post-rotation entry", log_path.read_text(encoding="utf-8"))
            finally:
                if PULSE_MODULE._log_handle:
                    PULSE_MODULE._log_handle.close()
                    PULSE_MODULE._log_handle = None
                    PULSE_MODULE._log_path = None

    def test_restart_log_rotation_guard_is_invoked(self) -> None:
        # _restart_service caps crash-looping service stdout before reopening.
        src = Path(ENV_MANAGER_DIR / "pulse.py").read_text(encoding="utf-8")
        restart_body = src.split("def _restart_service(")[1].split("\ndef ")[0]
        self.assertIn('rotate_log_file(paths["log_file"])', restart_body)


class RetentionPruneTests(unittest.TestCase):
    def _model(self, log_dir: Path, retention_days: float | str = 14) -> dict:
        return {
            "logs": [
                {
                    "id": "runtime",
                    "host_path": str(log_dir),
                    "retention_days": retention_days,
                }
            ]
        }

    def test_prunes_only_backdated_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "runtime"
            nested = log_dir / "svc"
            nested.mkdir(parents=True)
            now = time.time()
            old = now - 20 * DAY_SECONDS

            expired = log_dir / "old.log"
            expired_rotated = log_dir / "old.log.2"
            expired_nested = nested / "old.jsonl"
            expired_pid = log_dir / "stale.pid"
            expired_conf = log_dir / "ingress-nginx.conf"
            fresh = log_dir / "fresh.log"
            for path in (expired, expired_rotated, expired_nested, expired_pid, expired_conf, fresh):
                path.write_text("data\n", encoding="utf-8")
            for path in (expired, expired_rotated, expired_nested, expired_pid, expired_conf):
                os.utime(path, (old, old))

            removed = PULSE_MODULE.prune_expired_log_files(self._model(log_dir), now=now)

            removed_paths = {entry["path"] for entry in removed}
            self.assertEqual(
                removed_paths,
                {str(expired), str(expired_rotated), str(expired_nested)},
            )
            self.assertFalse(expired.exists())
            self.assertFalse(expired_rotated.exists())
            self.assertFalse(expired_nested.exists())
            # Non-log artifacts survive even when backdated.
            self.assertTrue(expired_pid.exists())
            self.assertTrue(expired_conf.exists())
            self.assertTrue(fresh.exists())
            self.assertEqual({entry["log_id"] for entry in removed}, {"runtime"})

    def test_zero_invalid_or_missing_retention_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "runtime"
            log_dir.mkdir()
            old = time.time() - 30 * DAY_SECONDS
            stale = log_dir / "stale.log"
            stale.write_text("data\n", encoding="utf-8")
            os.utime(stale, (old, old))

            for retention in (0, "", "nope"):
                removed = PULSE_MODULE.prune_expired_log_files(self._model(log_dir, retention))
                self.assertEqual(removed, [])
            self.assertTrue(stale.exists())

            missing = self._model(Path(tmpdir) / "does-not-exist")
            self.assertEqual(PULSE_MODULE.prune_expired_log_files(missing), [])

    def test_reconcile_log_retention_gates_on_interval_and_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "runtime"
            log_dir.mkdir()
            old = time.time() - 20 * DAY_SECONDS
            stale = log_dir / "stale.log"
            stale.write_text("data\n", encoding="utf-8")
            os.utime(stale, (old, old))

            model = self._model(log_dir)
            state = PULSE_MODULE.PulseState()
            with (
                mock.patch.object(PULSE_MODULE, "log_runtime_event") as log_event,
                mock.patch.object(PULSE_MODULE, "log"),
            ):
                PULSE_MODULE._reconcile_log_retention(model, state, now=1000.0)
                self.assertFalse(stale.exists())
                self.assertEqual(state.events_emitted, 1)
                log_event.assert_called_once()
                self.assertEqual(log_event.call_args.args[0], "pulse.logs_pruned")
                self.assertEqual(log_event.call_args.args[2]["removed_count"], 1)

                # Within the interval: no second prune attempt.
                with mock.patch.object(PULSE_MODULE, "prune_expired_log_files") as prune:
                    PULSE_MODULE._reconcile_log_retention(model, state, now=1000.0 + 60.0)
                    prune.assert_not_called()
                    PULSE_MODULE._reconcile_log_retention(
                        model,
                        state,
                        now=1000.0 + PULSE_MODULE.RETENTION_PRUNE_INTERVAL_SECONDS + 1.0,
                    )
                    prune.assert_called_once()


if __name__ == "__main__":
    unittest.main()
