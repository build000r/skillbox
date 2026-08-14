from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SBP = ROOT_DIR / "scripts" / "sbp"


def _run_sbp(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SBP), *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "SKILLBOX_ROOT": str(ROOT_DIR),
            "SKILLBOX_INVOKE_CWD": str(ROOT_DIR),
            "PYTHONPATH": str(ROOT_DIR / ".env-manager"),
        },
    )


class SbpHelpHumanTests(unittest.TestCase):
    def test_plain_help_advertises_human_mode(self) -> None:
        result = _run_sbp("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("help --human", result.stdout)

    def test_human_help_renders_grouped_atlas(self) -> None:
        result = _run_sbp("help", "--human")
        self.assertEqual(result.returncode, 0, result.stderr)
        for header in ("operator console", "START HERE", "RUNTIME", "SKILLS",
                       "ESTATE & GIT", "AGENTS & AUTOMATION", "MCP CONFIG PARITY"):
            self.assertIn(header, result.stdout)
        # Piped stdout is not a TTY: no ANSI escapes, no live NOW panel.
        self.assertNotIn("\x1b[", result.stdout)
        self.assertNotIn(" NOW ", result.stdout)

    def test_human_help_covers_every_plain_help_command_family(self) -> None:
        plain = _run_sbp("help").stdout
        human = _run_sbp("help", "--human").stdout
        families = [
            "capabilities", "robot-docs", "robot-triage", "status", "logs",
            "launch", "recalibrate", "candidates", "mcp", "doctor", "registry",
            "repo", "git", "cass", "oracle", "evidence", "cron", "send-later",
            "safe", "conference1", "beads", "mmdx", "hire", "skills", "skill",
            "overlay",
        ]
        for family in families:
            self.assertIn(family, plain)
            self.assertIn(family, human)

    def test_human_help_filter_narrows_and_reports_no_match(self) -> None:
        filtered = _run_sbp("help", "--human", "overlay")
        self.assertEqual(filtered.returncode, 0, filtered.stderr)
        self.assertIn("OVERLAYS", filtered.stdout)
        self.assertNotIn("send-later", filtered.stdout)

        missed = _run_sbp("help", "--human", "zzz-not-a-command")
        self.assertEqual(missed.returncode, 0, missed.stderr)
        self.assertIn("no commands match", missed.stdout)

    def test_capabilities_declares_help_command(self) -> None:
        result = _run_sbp("capabilities", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        commands = {command["name"]: command for command in payload["commands"]}
        self.assertIn("help", commands)
        self.assertIn("--human", commands["help"]["notes"])


if __name__ == "__main__":
    unittest.main()
