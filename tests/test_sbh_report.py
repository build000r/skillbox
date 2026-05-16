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

from runtime_manager.sbh_report import (  # noqa: E402
    classify_sbh_posture,
    collect_sbh_report,
    protected_path_veto,
    sbh_report_text_lines,
)


class SbhReportTests(unittest.TestCase):
    def test_missing_sbh_reports_not_configured_and_blocks_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"SKILLBOX_SBH_BIN": ""}, clear=False):
                with mock.patch("runtime_manager.sbh_report.shutil.which", return_value=None):
                    payload = collect_sbh_report(Path(tmpdir))

        self.assertEqual(payload["mode"], "read_only")
        self.assertFalse(payload["mutates"])
        self.assertFalse(payload["binary"]["present"])
        self.assertEqual(payload["posture"]["state"], "not-configured")
        self.assertEqual(payload["posture"]["daemon_state"], "missing-daemon")
        self.assertFalse(payload["policy"]["auto_delete_allowed"])
        self.assertFalse(payload["policy"]["ballast_mutation_allowed"])
        self.assertFalse(payload["policy"]["release_asset_promotion_allowed"])
        self.assertEqual(payload["policy"]["linux_x86_64_canary_pin"], "v0.4.22")
        self.assertEqual(payload["release_caveats"][0]["version"], "v0.4.23")
        self.assertEqual(payload["release_caveats"][0]["status"], "known_bad")
        self.assertIn("sbh clean", "\n".join(payload["blocked_mutation_commands"]))
        self.assertIn("Do not promote SBH v0.4.23", "\n".join(payload["next_actions"]))

    def test_observer_ready_fixture_classifies_running_daemon(self) -> None:
        probes = [
            {"id": "doctor_pal", "ok": True, "json": {"checks": [{"status": "pass"}]}, "stdout": "", "stderr": ""},
            {"id": "status", "ok": True, "json": {"daemon": {"running": True}}, "stdout": "", "stderr": ""},
            {"id": "stats", "ok": True, "json": {"events": 2}, "stdout": "", "stderr": ""},
            {"id": "blame", "ok": True, "json": {"agents": []}, "stdout": "", "stderr": ""},
        ]

        posture = classify_sbh_posture(True, probes)

        self.assertEqual(posture["state"], "observer-ready")
        self.assertEqual(posture["daemon_state"], "running")
        self.assertEqual(posture["observer_state"], "observing")
        self.assertTrue(posture["activity_seen"])

    def test_failed_doctor_and_missing_daemon_reports_remediation(self) -> None:
        probes = [
            {"id": "doctor_pal", "ok": False, "json": None, "stdout": "", "stderr": "FAIL launchd service"},
            {"id": "status", "ok": False, "json": None, "stdout": "", "stderr": "not running"},
        ]

        posture = classify_sbh_posture(True, probes)

        self.assertEqual(posture["state"], "remediation")
        self.assertEqual(posture["daemon_state"], "not-running")
        self.assertIn("doctor_pal", posture["failed_probe_ids"])

    def test_text_status_not_running_is_not_misread_as_running(self) -> None:
        # Regression: 'running' substring used to match inside 'not running' first.
        probes = [
            {"id": "status", "ok": True, "json": None, "stdout": "sbh is not running", "stderr": ""},
        ]

        posture = classify_sbh_posture(True, probes)

        self.assertEqual(posture["daemon_state"], "not-running")
        self.assertEqual(posture["state"], "remediation")

    def test_text_broken_does_not_match_ok_substring_as_healthy(self) -> None:
        # Regression: 'ok' is a 2-char substring that lit up inside 'broken'.
        probes = [
            {"id": "status", "ok": True, "json": None, "stdout": "", "stderr": "service is broken"},
        ]

        posture = classify_sbh_posture(True, probes)

        self.assertEqual(posture["daemon_state"], "not-running")
        self.assertEqual(posture["state"], "remediation")

    def test_doctor_no_failures_text_is_not_a_failure(self) -> None:
        # Regression: substring 'fail' matched inside 'no failures detected'.
        probes = [
            {"id": "doctor_pal", "ok": True, "json": {}, "stdout": "no failures detected", "stderr": ""},
            {"id": "status", "ok": True, "json": {"daemon_running": True}, "stdout": "", "stderr": ""},
        ]

        posture = classify_sbh_posture(True, probes)

        self.assertEqual(posture["state"], "observer-ready")

    def test_collect_sbh_report_runs_only_safe_probe_commands(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[1] == "status":
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"running": True}), stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True}), stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "sbh"
            fake_bin.touch()
            with mock.patch.dict(os.environ, {"SKILLBOX_SBH_BIN": ""}, clear=False):
                with mock.patch("runtime_manager.sbh_report.subprocess.run", side_effect=fake_run):
                    payload = collect_sbh_report(Path(tmpdir), binary=str(fake_bin), decision_id="decision-1")

        self.assertEqual(payload["posture"]["state"], "observer-ready")
        self.assertIn([str(fake_bin), "doctor", "--pal"], calls)
        self.assertIn([str(fake_bin), "status", "--json"], calls)
        self.assertIn([str(fake_bin), "stats", "--window", "24h"], calls)
        self.assertIn([str(fake_bin), "blame", "--json"], calls)
        self.assertIn([str(fake_bin), "explain", "--id", "decision-1"], calls)
        self.assertFalse(payload["policy"]["clean_allowed"])

    def test_protected_path_veto_blocks_codex_state_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            payload = collect_sbh_report(Path(tmpdir), home=home, run_probes=False)
            veto = protected_path_veto(home / ".codex" / "sessions", payload["protected_paths"])

        self.assertFalse(veto["allowed"])
        self.assertEqual(veto["matched"], "codex-state")

    def test_sbh_report_cli_json_no_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    ".env-manager/manage.py",
                    "sbh-report",
                    "--format",
                    "json",
                    "--no-probes",
                    "--home",
                    str(home),
                ],
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR), "SKILLBOX_SBH_BIN": ""},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "read_only")
        self.assertFalse(payload["policy"]["auto_delete_allowed"])
        self.assertFalse(payload["policy"]["release_asset_promotion_allowed"])
        self.assertIn("sbh doctor --pal", payload["safe_probe_commands"])
        self.assertEqual(payload["release_caveats"][0]["affected_asset"], "sbh-v0.4.23-x86_64-unknown-linux-gnu.tar.xz")

    def test_sbh_text_mentions_observe_first_policy(self) -> None:
        lines = sbh_report_text_lines(
            {
                "binary": {"present": False, "path": None},
                "posture": {"state": "not-configured", "daemon_state": "missing-daemon"},
                "policy": {
                    "auto_delete_allowed": False,
                    "ballast_mutation_allowed": False,
                    "release_asset_promotion_allowed": False,
                },
                "protected_paths": [{"id": "codex-state", "display_path": "~/.codex", "policy": "hard_veto"}],
                "release_caveats": [
                    {
                        "version": "v0.4.23",
                        "affected_asset": "sbh-v0.4.23-x86_64-unknown-linux-gnu.tar.xz",
                        "status": "known_bad",
                        "observed_file_type": "Mach-O 64-bit executable arm64",
                    }
                ],
                "next_actions": ["Do not run `sbh clean` without explicit approval."],
            }
        )

        self.assertTrue(any("auto delete allowed: False" in line for line in lines))
        self.assertTrue(any("ballast mutation allowed: False" in line for line in lines))
        self.assertTrue(any("release asset promotion allowed: False" in line for line in lines))
        self.assertIn("known_bad", "\n".join(lines))
        self.assertIn("Do not run `sbh clean`", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
