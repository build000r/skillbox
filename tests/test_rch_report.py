from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.rch_report import classify_rch_posture, collect_rch_report, rch_report_text_lines  # noqa: E402


class RchReportTests(unittest.TestCase):
    def test_missing_rch_reports_not_configured_no_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"SKILLBOX_RCH_BIN": ""}, clear=False):
                with mock.patch("runtime_manager.rch_report.shutil.which", return_value=None):
                    payload = collect_rch_report(Path(tmpdir))

        self.assertEqual(payload["mode"], "read_only")
        self.assertFalse(payload["mutates"])
        self.assertFalse(payload["binary"]["present"])
        self.assertEqual(payload["posture"]["state"], "not-configured")
        self.assertEqual(payload["posture"]["worker_state"], "no-worker")
        self.assertTrue(payload["posture"]["fail_open_expected"])
        self.assertFalse(payload["global_hook_install"]["allowed"])
        self.assertIn("Do not run `rch hook install`", "\n".join(payload["next_actions"]))

    def test_worker_ready_fixture_classifies_healthy_worker(self) -> None:
        probes = [
            {
                "id": "status",
                "ok": True,
                "json": {"data": {"workers": [{"id": "portfolio-devbox", "healthy": True}]}},
                "stdout": "",
                "stderr": "",
            },
            {
                "id": "hook_status",
                "ok": True,
                "json": {"installed": False},
                "stdout": "not installed",
                "stderr": "",
            },
        ]

        posture = classify_rch_posture(True, probes)

        self.assertEqual(posture["state"], "worker-ready")
        self.assertEqual(posture["worker_state"], "worker-ready")
        self.assertEqual(posture["healthy_workers"], 1)
        self.assertEqual(posture["total_workers"], 1)
        self.assertFalse(posture["hook"]["installed"])

    def test_no_workers_and_failed_probe_reports_remediation(self) -> None:
        probes = [
            {"id": "status", "ok": True, "json": {"workers": []}, "stdout": "", "stderr": ""},
            {"id": "check", "ok": False, "json": None, "stdout": "", "stderr": "no workers available"},
        ]

        posture = classify_rch_posture(True, probes)

        self.assertEqual(posture["state"], "remediation")
        self.assertEqual(posture["worker_state"], "no-workers")
        self.assertEqual(posture["failed_probe_ids"], ["check"])
        self.assertTrue(posture["fail_open_expected"])

    def test_collect_rch_report_runs_only_safe_probe_commands(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[1] == "status":
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps({"workers": [{"id": "portfolio-devbox", "state": "ready"}]}),
                    stderr="",
                )
            if args[1] == "hook":
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"installed": False}), stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True}), stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("runtime_manager.rch_report.shutil.which", return_value="/usr/local/bin/rch"):
                with mock.patch("runtime_manager.rch_report.subprocess.run", side_effect=fake_run):
                    payload = collect_rch_report(Path(tmpdir))

        self.assertEqual(payload["posture"]["state"], "worker-ready")
        self.assertIn(["/usr/local/bin/rch", "--robot-triage", "--json"], calls)
        self.assertIn(["/usr/local/bin/rch", "status", "--workers", "--jobs", "--json"], calls)
        self.assertIn(["/usr/local/bin/rch", "check", "--json"], calls)
        self.assertIn(["/usr/local/bin/rch", "hook", "status", "--json"], calls)
        self.assertFalse(payload["global_hook_install"]["allowed"])

    def test_collect_rch_report_honors_env_binary_path(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[1] == "status":
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"workers": []}), stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True}), stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "rch"
            fake_bin.touch()
            with mock.patch.dict(os.environ, {"SKILLBOX_RCH_BIN": str(fake_bin)}, clear=False):
                with mock.patch("runtime_manager.rch_report.subprocess.run", side_effect=fake_run):
                    payload = collect_rch_report(Path(tmpdir))

        self.assertTrue(payload["binary"]["present"])
        self.assertEqual(payload["binary"]["path"], str(fake_bin))
        self.assertEqual(payload["binary"]["source"], "env")
        self.assertTrue(calls)
        self.assertTrue(all(call[0] == str(fake_bin) for call in calls))

    def test_rch_report_cli_json_no_probes(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                ".env-manager/manage.py",
                "rch-report",
                "--format",
                "json",
                "--no-probes",
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
        self.assertFalse(payload["global_hook_install"]["allowed"])
        self.assertIn("rch hook status --json", payload["safe_probe_commands"])

    def test_rch_text_mentions_hook_policy(self) -> None:
        lines = rch_report_text_lines(
            {
                "binary": {"present": False, "path": None},
                "posture": {"state": "not-configured", "healthy_workers": 0, "total_workers": 0},
                "global_hook_install": {"allowed": False},
                "next_actions": ["Do not run `rch hook install` without explicit approval."],
            }
        )

        self.assertTrue(any("hook install allowed: False" in line for line in lines))
        self.assertIn("Do not run `rch hook install`", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
