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
from runtime_manager import workflows as WORKFLOWS  # noqa: E402


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

        with self.assertRaises(StateBackupError) as raised:
            restore_state_backup(manifest, state_root=self.state_root, backup_root=self.backup_root)
        self.assertEqual(raised.exception.code, "STATE_BACKUP_RESTORE_CONFIRMATION_REQUIRED")

        pulse_pid = self.state_root / "logs" / "runtime" / "pulse.pid"
        pulse_pid.parent.mkdir(parents=True)
        pulse_pid.write_text(f"{os.getpid()}\n", encoding="utf-8")
        with self.assertRaises(StateBackupError) as raised:
            restore_state_backup(
                manifest,
                state_root=self.state_root,
                backup_root=self.backup_root,
                i_understand_data_loss=True,
            )
        self.assertEqual(raised.exception.code, "STATE_BACKUP_PULSE_RUNNING")
        pulse_pid.unlink()

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


if __name__ == "__main__":
    unittest.main()
