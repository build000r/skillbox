from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.pressure_report import collect_pressure_report, pressure_report_text_lines  # noqa: E402


class PressureReportTests(unittest.TestCase):
    def _write_box_inventory(self, root: Path) -> None:
        workspace = root / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "boxes.json").write_text(
            json.dumps(
                {
                    "boxes": [
                        {
                            "id": "worker-devbox",
                            "state": "ready",
                            "profile": "dev-small",
                            "tailscale_hostname": "skillbox-worker-devbox",
                            "tailscale_ip": "100.86.253.9",
                            "state_root": "/srv/skillbox",
                            "storage_filesystem": "ext4",
                            "storage_min_free_gb": 10.0,
                            "volume_name": "skillbox-state-worker-devbox",
                            "volume_size_gb": 20,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_collect_pressure_report_is_read_only_and_marks_protected_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            home = Path(tmpdir) / "home"
            root.mkdir()
            home.mkdir()
            (home / ".codex").mkdir()
            (home / ".claude").mkdir()
            self._write_box_inventory(root)

            with (
                mock.patch(
                    "runtime_manager.pressure_report.shutil.disk_usage",
                    return_value=SimpleNamespace(total=100 * 1024 ** 3, used=70 * 1024 ** 3, free=30 * 1024 ** 3),
                ),
                mock.patch("runtime_manager.pressure_report.shutil.which", return_value=None),
            ):
                payload = collect_pressure_report(root, home=home)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "read_only")
        self.assertFalse(payload["mutates"])
        self.assertFalse(payload["target_policy"]["cleanup_allowed"])
        self.assertFalse(payload["target_policy"]["remote_writes_allowed"])
        self.assertIn("primary-prod", payload["target_policy"]["excluded_box_ids"])
        self.assertEqual(payload["local_disk"]["pressure_level"], "normal")
        self.assertEqual(payload["box"]["target_box"], "worker-devbox")
        self.assertFalse(payload["box"]["live_free_known"])
        self.assertEqual(payload["box"]["volume_size_gib"], 20.0)

        policies = {bucket["id"]: bucket["policy"] for bucket in payload["protected_buckets"]}
        self.assertEqual(policies["codex-state"], "protected_no_touch")
        self.assertEqual(policies["claude-state"], "protected_no_touch")
        self.assertFalse(payload["tools"]["rch"]["installed"])
        self.assertFalse(payload["tools"]["sbh"]["installed"])
        self.assertIn("RCH is not configured here yet", "\n".join(payload["next_actions"]))

    def test_collect_pressure_report_detects_tool_binaries_and_daemon_state(self) -> None:
        def fake_which(name: str) -> str | None:
            return {
                "rch": "/usr/local/bin/rch",
                "sbh": "/usr/local/bin/sbh",
                "pgrep": "/usr/bin/pgrep",
            }.get(name)

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            process_name = args[-1]
            return subprocess.CompletedProcess(args, 0 if process_name == "rchd" else 1, stdout="123\n", stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            home = Path(tmpdir) / "home"
            root.mkdir()
            home.mkdir()
            self._write_box_inventory(root)
            with (
                mock.patch(
                    "runtime_manager.pressure_report.shutil.disk_usage",
                    return_value=SimpleNamespace(total=100 * 1024 ** 3, used=95 * 1024 ** 3, free=5 * 1024 ** 3),
                ),
                mock.patch("runtime_manager.pressure_report.shutil.which", side_effect=fake_which),
                mock.patch("runtime_manager.pressure_report.subprocess.run", side_effect=fake_run),
            ):
                payload = collect_pressure_report(root, home=home)

        self.assertTrue(payload["tools"]["rch"]["installed"])
        self.assertTrue(payload["tools"]["rch"]["daemon"]["running"])
        self.assertTrue(payload["tools"]["sbh"]["installed"])
        self.assertFalse(payload["tools"]["sbh"]["daemon"]["running"])
        self.assertEqual(payload["local_disk"]["pressure_level"], "critical")
        self.assertIn("rch exec -- <build-or-test-command>", "\n".join(payload["next_actions"]))

    def test_pressure_report_text_is_compact_and_safe(self) -> None:
        payload = {
            "local_disk": {
                "path": "/home/test",
                "free_gib": 12.5,
                "total_gib": 100.0,
                "free_percent": 12.5,
                "pressure_level": "elevated",
            },
            "box": {
                "found": True,
                "target_box": "worker-devbox",
                "state": "ready",
                "volume_size_gib": 20.0,
                "min_free_gib": 10.0,
            },
            "tools": {
                "rch": {"installed": False, "daemon": {"running": None}},
                "sbh": {"installed": True, "daemon": {"running": False}},
            },
            "protected_buckets": [
                {"id": "codex-state", "display_path": "~/.codex", "present": True},
            ],
            "next_actions": ["Do not delete, truncate, or clean protected buckets without explicit operator approval."],
        }

        lines = pressure_report_text_lines(payload)

        self.assertIn("pressure report: read-only", lines[0])
        self.assertTrue(any("box: worker-devbox" in line for line in lines))
        self.assertTrue(any("codex-state" in line and "no-touch" in line for line in lines))
        self.assertIn("Do not delete", "\n".join(lines))

    def test_pressure_report_cli_emits_json_for_temp_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    ".env-manager/manage.py",
                    "pressure-report",
                    "--format",
                    "json",
                    "--home",
                    str(home),
                ],
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "read_only")
        self.assertEqual(payload["target_policy"]["target_box"], "worker-devbox")
        self.assertTrue(any(bucket["id"] == "codex-state" for bucket in payload["protected_buckets"]))


if __name__ == "__main__":
    unittest.main()
