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
    list_state_backups,
    verify_state_backup,
)


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
