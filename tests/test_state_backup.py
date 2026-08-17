from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.state_backup import (  # noqa: E402
    DEFAULT_EXCLUDES,
    StateBackupError,
    create_state_backup,
    drill_state_backup,
    list_state_backups,
    restore_state_backup,
    verify_state_backup,
)
from runtime_manager import state_mutation as SM  # noqa: E402
from runtime_manager import workflows as WORKFLOWS  # noqa: E402



def _leased(state_root):
    """Hold the single-writer lease for ``state_root`` the way dispatch does.

    ``restore`` replaces an entire root, so it refuses unless the caller already
    holds the lease for exactly that root. Every restore test therefore runs
    inside this.
    """
    return SM.state_mutation_lease(state_root, "manage.state-backup.restore")

class StateBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.state_root = self.root / "state"
        self.backup_root = self.root / "backups"
        self.state_root.mkdir()
        (self.state_root / "logs").mkdir()
        (self.state_root / "logs" / "runtime.log").write_text("runtime\n", encoding="utf-8")
        (self.state_root / "clients").mkdir()
        (self.state_root / "clients" / "personal.json").write_text('{"ok": true}\n', encoding="utf-8")
        (self.state_root / "monoserver").mkdir()
        (self.state_root / "monoserver" / "skip.txt").write_text("skip\n", encoding="utf-8")
        (self.state_root / "pkg" / "__pycache__").mkdir(parents=True)
        (self.state_root / "pkg" / "__pycache__" / "skip.pyc").write_bytes(b"skip")
        (self.state_root / "pruned-skill-repo-extras-old").mkdir()
        (self.state_root / "pruned-skill-repo-extras-old" / "skip.txt").write_text("skip\n", encoding="utf-8")
        self.addCleanup(self.tmpdir.cleanup)

    def test_create_and_verify_round_trip(self) -> None:
        payload = create_state_backup(state_root=self.state_root, backup_root=self.backup_root)

        self.assertTrue(payload["ok"])
        backup = payload["backup"]
        archive = Path(backup["archive"])
        manifest = Path(backup["manifest"])
        self.assertTrue(archive.is_file())
        self.assertTrue(manifest.is_file())
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)

        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest_payload["file_count"], 2)
        self.assertEqual(manifest_payload["source_root"], str(self.state_root.resolve()))
        self.assertEqual(manifest_payload["excludes_applied"], list(DEFAULT_EXCLUDES))
        self.assertEqual(manifest_payload["top_level_entries"], ["clients", "logs", "pkg"])

        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
        self.assertIn("logs/runtime.log", names)
        self.assertIn("clients/personal.json", names)
        self.assertFalse(any(name.startswith("monoserver/") for name in names))
        self.assertFalse(any("__pycache__" in name for name in names))
        self.assertFalse(any(name.startswith("pruned-skill-repo-extras-old/") for name in names))

        verify = verify_state_backup(manifest)
        self.assertTrue(verify["ok"])
        self.assertTrue(all(check["ok"] for check in verify["checks"]))

        listed = list_state_backups(backup_root=self.backup_root)
        self.assertEqual(listed["count"], 1)
        self.assertTrue(listed["backups"][0]["verified"])

    def test_verify_detects_flipped_archive_byte(self) -> None:
        payload = create_state_backup(state_root=self.state_root, backup_root=self.backup_root)
        archive = Path(payload["backup"]["archive"])
        manifest = Path(payload["backup"]["manifest"])

        with archive.open("r+b") as handle:
            handle.seek(10)
            original = handle.read(1)
            handle.seek(10)
            handle.write(bytes([original[0] ^ 0xFF]))

        verify = verify_state_backup(manifest)
        self.assertFalse(verify["ok"])
        sha_check = next(check for check in verify["checks"] if check["name"] == "sha256")
        self.assertFalse(sha_check["ok"])

    def test_drill_round_trip_writes_evidence_and_checks_yaml(self) -> None:
        (self.state_root / "workspace").mkdir()
        (self.state_root / "workspace" / "runtime.yaml").write_text("services: []\n", encoding="utf-8")
        create_state_backup(state_root=self.state_root, backup_root=self.backup_root)

        drill = drill_state_backup(state_root=self.state_root, backup_root=self.backup_root)

        self.assertTrue(drill["ok"])
        evidence_path = Path(drill["evidence_path"])
        self.assertTrue(evidence_path.is_file())
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["action"], "drill")
        self.assertTrue(evidence["ok"])
        names = {check["name"] for check in evidence["checks"]}
        self.assertIn("path_escape", names)
        yaml_check = next(check for check in evidence["checks"] if check["name"] == "yaml_parse")
        self.assertTrue(yaml_check["ok"])
        self.assertEqual(yaml_check["checked"], 1)

    def test_drill_detects_flipped_archive_byte_and_writes_failed_evidence(self) -> None:
        payload = create_state_backup(state_root=self.state_root, backup_root=self.backup_root)
        archive = Path(payload["backup"]["archive"])
        with archive.open("r+b") as handle:
            handle.seek(10)
            original = handle.read(1)
            handle.seek(10)
            handle.write(bytes([original[0] ^ 0xFF]))

        drill = drill_state_backup(state_root=self.state_root, backup_root=self.backup_root)

        self.assertFalse(drill["ok"])
        sha_check = next(check for check in drill["checks"] if check["name"] == "sha256")
        self.assertFalse(sha_check["ok"])
        evidence = json.loads(Path(drill["evidence_path"]).read_text(encoding="utf-8"))
        self.assertFalse(evidence["ok"])

    def test_destination_inside_source_is_rejected(self) -> None:
        with self.assertRaises(StateBackupError) as raised:
            create_state_backup(state_root=self.state_root, backup_root=self.state_root / "backups")

        self.assertEqual(raised.exception.code, "STATE_BACKUP_DEST_INSIDE_SOURCE")

    def test_free_space_check_rejects_insufficient_destination(self) -> None:
        with mock.patch(
            "runtime_manager.state_backup.shutil.disk_usage",
            return_value=SimpleNamespace(total=100, used=99, free=1),
        ):
            with self.assertRaises(StateBackupError) as raised:
                create_state_backup(state_root=self.state_root, backup_root=self.backup_root)

        self.assertEqual(raised.exception.code, "STATE_BACKUP_INSUFFICIENT_SPACE")
        self.assertFalse(list(self.backup_root.glob("*.tar.gz")))

    def test_restore_guardrails_and_successful_swap(self) -> None:
        create = create_state_backup(state_root=self.state_root, backup_root=self.backup_root)
        manifest = Path(create["backup"]["manifest"])
        original = (self.state_root / "logs" / "runtime.log").read_text(encoding="utf-8")
        (self.state_root / "logs" / "runtime.log").write_text("changed\n", encoding="utf-8")

        with _leased(self.state_root):
            with self.assertRaises(StateBackupError) as raised:
                restore_state_backup(
                    manifest, state_root=self.state_root, backup_root=self.backup_root
                )
            self.assertEqual(
                raised.exception.code, "STATE_BACKUP_RESTORE_CONFIRMATION_REQUIRED"
            )

        pulse_pid = self.state_root / "logs" / "runtime" / "pulse.pid"
        pulse_pid.parent.mkdir(parents=True)
        pulse_pid.write_text(f"{os.getpid()}\n", encoding="utf-8")
        with _leased(self.state_root):
            with self.assertRaises(StateBackupError) as raised:
                restore_state_backup(
                    manifest,
                    state_root=self.state_root,
                    backup_root=self.backup_root,
                    i_understand_data_loss=True,
                )
            self.assertEqual(raised.exception.code, "STATE_BACKUP_PULSE_RUNNING")
        pulse_pid.unlink()

        with _leased(self.state_root):
            restore = restore_state_backup(
                manifest,
                state_root=self.state_root,
                backup_root=self.backup_root,
                i_understand_data_loss=True,
            )

        self.assertTrue(restore["ok"])
        self.assertEqual((self.state_root / "logs" / "runtime.log").read_text(encoding="utf-8"), original)
        self.assertIn("archive", restore["safety_backup"])
        self.assertGreaterEqual(len(list(self.backup_root.glob("*.manifest.json"))), 2)

    def test_restore_refuses_sha256_mismatch(self) -> None:
        payload = create_state_backup(state_root=self.state_root, backup_root=self.backup_root)
        archive = Path(payload["backup"]["archive"])
        manifest = Path(payload["backup"]["manifest"])
        with archive.open("r+b") as handle:
            handle.seek(10)
            original = handle.read(1)
            handle.seek(10)
            handle.write(bytes([original[0] ^ 0xFF]))

        with _leased(self.state_root):
            with self.assertRaises(StateBackupError) as raised:
                restore_state_backup(
                    manifest,
                    state_root=self.state_root,
                    backup_root=self.backup_root,
                    i_understand_data_loss=True,
                )

        self.assertEqual(raised.exception.code, "STATE_BACKUP_SHA256_MISMATCH")

    def test_stewardship_backup_restore_evidence_uses_last_drill(self) -> None:
        create_state_backup(state_root=self.state_root, backup_root=self.backup_root)
        drill = drill_state_backup(state_root=self.state_root, backup_root=self.backup_root)
        now = WORKFLOWS._parse_utc_z(drill["drilled_at"]) + 10  # noqa: SLF001

        evidence = WORKFLOWS._stewardship_backup_restore_evidence(  # noqa: SLF001
            {"storage": {"state_root": str(self.state_root)}},
            now,
        )

        self.assertEqual(evidence["status"], "ready")
        self.assertTrue(evidence["ok"])
        self.assertEqual(evidence["last_drill"], drill["drilled_at"])
        self.assertEqual(evidence["age_days"], 0.0)
        self.assertFalse(any(item["id"] == "backup-recovery" for item in WORKFLOWS._stewardship_not_assessed(evidence)))  # noqa: SLF001
        stale = WORKFLOWS._stewardship_backup_restore_evidence(  # noqa: SLF001
            {"storage": {"state_root": str(self.state_root)}},
            now + 31 * 86400,
        )
        self.assertEqual(stale["status"], "not_assessed")
        self.assertTrue(any(item["id"] == "backup-recovery" for item in WORKFLOWS._stewardship_not_assessed(stale)))  # noqa: SLF001

    def test_cli_create_and_verify_latest_accepts_flags_after_action(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR), "SKILLBOX_BACKUP_ROOT": str(self.backup_root)}
        create = subprocess.run(
            [
                sys.executable,
                ".env-manager/manage.py",
                "state-backup",
                "create",
                "--state-root",
                str(self.state_root),
                "--format",
                "json",
            ],
            cwd=ROOT_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(create.returncode, 0, create.stderr)
        self.assertTrue(json.loads(create.stdout)["ok"])

        verify = subprocess.run(
            [sys.executable, ".env-manager/manage.py", "state-backup", "verify", "--format", "json"],
            cwd=ROOT_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)["ok"])


class RestoreLeaseGateTests(unittest.TestCase):
    """Restore replaces a whole root; it must do that under one held lease."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.state_root = root / "state"
        self.backup_root = root / "backups"
        (self.state_root / "logs").mkdir(parents=True)
        (self.state_root / "logs" / "runtime.log").write_text("one\n", encoding="utf-8")
        self.backup_root.mkdir(parents=True)
        self.addCleanup(setattr, SM, "_ACTIVE_RUNTIME_LEASE", None)

    def _manifest(self) -> Path:
        created = create_state_backup(
            state_root=self.state_root, backup_root=self.backup_root
        )
        return Path(created["backup"]["manifest"])

    def test_an_ungated_restore_is_refused(self) -> None:
        """An ungated root swap is never the degrade path."""
        manifest = self._manifest()
        with self.assertRaises(StateBackupError) as raised:
            restore_state_backup(
                manifest,
                state_root=self.state_root,
                backup_root=self.backup_root,
                i_understand_data_loss=True,
            )
        self.assertEqual(raised.exception.code, "STATE_BACKUP_RESTORE_UNGATED")

    def test_a_lease_on_a_different_root_is_refused(self) -> None:
        """Holding a lock over a root nothing is touching is not protection."""
        manifest = self._manifest()
        other = Path(self._tmp.name).resolve() / "other-state"
        other.mkdir()
        with SM.state_mutation_lease(other, "manage.state-backup.restore"):
            with self.assertRaises(StateBackupError) as raised:
                restore_state_backup(
                    manifest,
                    state_root=self.state_root,
                    backup_root=self.backup_root,
                    i_understand_data_loss=True,
                )
        self.assertEqual(raised.exception.code, "STATE_BACKUP_RESTORE_ROOT_MISMATCH")
        self.assertIn("other-state", str(raised.exception))

    def test_the_lock_is_a_stable_sibling_that_survives_the_root_swap(self) -> None:
        """The property that makes a one-lease restore possible at all.

        The lock lives beside the root, not inside it, so renaming the root away
        and deleting it cannot move the inode the holder is flocked to.
        """
        lock_path = SM.lease_lock_path(self.state_root)
        self.assertEqual(lock_path.parent, self.state_root.parent)
        self.assertFalse(
            str(lock_path).startswith(str(self.state_root) + "/"),
            "the lock must not live inside the root being replaced",
        )

        manifest = self._manifest()
        (self.state_root / "logs" / "runtime.log").write_text("two\n", encoding="utf-8")

        with SM.state_mutation_lease(
            self.state_root, "manage.state-backup.restore"
        ) as held:
            before = lock_path.stat().st_ino
            result = restore_state_backup(
                manifest,
                state_root=self.state_root,
                backup_root=self.backup_root,
                i_understand_data_loss=True,
            )
            # One lease, one lock inode, across a rename of the whole root.
            self.assertTrue(held.held)
            self.assertEqual(lock_path.stat().st_ino, before)

        self.assertTrue(result["ok"])
        self.assertEqual(
            (self.state_root / "logs" / "runtime.log").read_text(encoding="utf-8"),
            "one\n",
        )

    def test_create_list_and_verify_stay_readers(self) -> None:
        """Only restore is gated; the scan paths take no lock (an explicit non-goal)."""
        self.assertIsNone(SM.active_runtime_lease())
        created = create_state_backup(
            state_root=self.state_root, backup_root=self.backup_root
        )
        manifest = Path(created["backup"]["manifest"])
        self.assertTrue(verify_state_backup(manifest)["ok"])
        self.assertTrue(list_state_backups(backup_root=self.backup_root)["ok"])
        self.assertIsNone(SM.active_runtime_lease())


if __name__ == "__main__":
    unittest.main()
