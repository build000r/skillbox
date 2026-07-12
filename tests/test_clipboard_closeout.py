"""Tests for the clipboard closeout gate verdict policy and CLI surface.

These tests cover the mocked/unit side of the closeout gate: the JSON report
helper that owns the PASS/FAIL/SKIP policy, and the closeout script's CLI
surface. They deliberately do NOT run live terminal paths; that is what
``scripts/clipboard-closeout.sh --live`` is for.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import clipboard_closeout_report as report  # noqa: E402

CLOSEOUT = ROOT_DIR / "scripts" / "clipboard-closeout.sh"
REPORT_PY = ROOT_DIR / "scripts" / "lib" / "clipboard_closeout_report.py"


def _payload(mode: str) -> dict:
    return report.init_report(mode, "20260711T000000Z", "testhost", "/tmp/art")


class VerdictPolicyTests(unittest.TestCase):
    def test_smoke_mode_allows_core_skips(self) -> None:
        payload = _payload("smoke")
        report.record_gate(payload, name="unit_tests", status="PASS", core=True, kind="unit")
        report.record_gate(
            payload,
            name="ssh_osc52_d3",
            status="SKIP",
            core=True,
            reason="live terminal path; smoke mode",
        )
        payload, rc = report.finalize_report(payload)
        self.assertEqual(payload["overall"], "PASS")
        self.assertEqual(rc, 0)
        self.assertTrue(payload["skips_allowed"])
        self.assertIn("non-live", payload["skip_note"])
        self.assertEqual(payload["skipped"][0]["name"], "ssh_osc52_d3")

    def test_live_mode_fails_closed_on_skipped_core_path(self) -> None:
        payload = _payload("live")
        report.record_gate(payload, name="unit_tests", status="PASS", core=True, kind="unit")
        report.record_gate(
            payload,
            name="ssh_osc52_d3",
            status="SKIP",
            core=True,
            reason="d3 unreachable",
        )
        payload, rc = report.finalize_report(payload)
        self.assertEqual(payload["overall"], "FAIL")
        self.assertEqual(rc, 1)
        self.assertIn("ssh_osc52_d3", payload["blocking"])
        self.assertFalse(payload["skips_allowed"])

    def test_live_mode_fails_closed_on_skipped_current_host_migration(self) -> None:
        payload = _payload("live")
        report.record_gate(
            payload,
            name="current_host_migration",
            status="SKIP",
            core=True,
            reason="not checked",
        )
        payload, rc = report.finalize_report(payload)
        self.assertEqual(payload["overall"], "FAIL")
        self.assertIn("current_host_migration", payload["blocking"])
        self.assertEqual(rc, 1)

    def test_live_mode_allows_non_core_skip(self) -> None:
        payload = _payload("live")
        report.record_gate(payload, name="unit_tests", status="PASS", core=True, kind="unit")
        report.record_gate(
            payload,
            name="optional_extra",
            status="SKIP",
            core=False,
            reason="optional",
        )
        payload, rc = report.finalize_report(payload)
        self.assertEqual(payload["overall"], "PASS")
        self.assertEqual(rc, 0)

    def test_any_fail_blocks_both_modes(self) -> None:
        for mode in ("smoke", "live"):
            payload = _payload(mode)
            report.record_gate(payload, name="static_checks", status="FAIL", core=True, kind="static")
            payload, rc = report.finalize_report(payload)
            self.assertEqual(payload["overall"], "FAIL", msg=mode)
            self.assertEqual(rc, 1, msg=mode)
            self.assertIn("static_checks", payload["failed"], msg=mode)

    def test_invalid_mode_and_status_rejected(self) -> None:
        with self.assertRaises(ValueError):
            report.init_report("rollout", "s", "h", "a")
        payload = _payload("live")
        with self.assertRaises(ValueError):
            report.record_gate(payload, name="x", status="MAYBE", core=True)

    def test_gate_metadata_recorded(self) -> None:
        payload = _payload("live")
        report.record_gate(
            payload,
            name="conference_direct_wsl",
            status="PASS",
            core=True,
            exit_code=0,
            target_host="worker@conference1-wsl",
            transport="ssh",
            log="/tmp/art/conference_direct_wsl.log",
        )
        gate = payload["gates"][0]
        self.assertEqual(gate["target_host"], "worker@conference1-wsl")
        self.assertEqual(gate["transport"], "ssh")
        self.assertEqual(gate["log"], "/tmp/art/conference_direct_wsl.log")


class ReportCliTests(unittest.TestCase):
    def test_init_record_finalize_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "closeout.json"
            base = [sys.executable, str(REPORT_PY)]
            subprocess.run(
                base + ["init", "--out", str(out), "--mode", "live", "--stamp", "s", "--host", "h"],
                check=True,
            )
            subprocess.run(
                base + ["record", "--out", str(out), "--name", "unit_tests", "--status", "PASS",
                        "--core", "--kind", "unit", "--exit-code", "0"],
                check=True,
            )
            subprocess.run(
                base + ["record", "--out", str(out), "--name", "direct_ghostty_osc52",
                        "--status", "SKIP", "--core", "--reason", "operator Mac unreachable"],
                check=True,
            )
            proc = subprocess.run(base + ["finalize", "--out", str(out)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
            self.assertIn("overall: FAIL", proc.stdout)
            self.assertIn("direct_ghostty_osc52", proc.stdout)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["overall"], "FAIL")
            self.assertEqual(payload["blocking"], ["direct_ghostty_osc52"])


class CloseoutScriptTests(unittest.TestCase):
    def test_help_documents_both_modes(self) -> None:
        proc = subprocess.run(
            ["bash", str(CLOSEOUT), "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("--live", proc.stdout)
        self.assertIn("smoke", proc.stdout)
        self.assertIn("SKIP", proc.stdout)

    def test_unknown_arg_rejected(self) -> None:
        proc = subprocess.run(
            ["bash", str(CLOSEOUT), "--bogus"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 2)

    def test_script_shell_syntax(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(CLOSEOUT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)


if __name__ == "__main__":
    unittest.main()
