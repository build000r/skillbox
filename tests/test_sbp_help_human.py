from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SBP = ROOT_DIR / "scripts" / "sbp"


def _run_sbp(*args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SKILLBOX_ROOT": str(ROOT_DIR),
        "SKILLBOX_INVOKE_CWD": str(ROOT_DIR),
        "PYTHONPATH": str(ROOT_DIR / ".env-manager"),
        "NO_COLOR": "1",
    }
    # Piped help must stay plain. Operator FORCE_COLOR=0 is a non-empty string
    # and used to leak ANSI into capture_output tests.
    env.pop("FORCE_COLOR", None)
    env.pop("CLICOLOR_FORCE", None)
    return subprocess.run(
        [str(SBP), *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _load_atlas_module():
    import importlib.util
    import sys

    lib_path = ROOT_DIR / "scripts" / "lib" / "sbp_help_human.py"
    spec = importlib.util.spec_from_file_location("sbp_help_atlas", lib_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sbp_help_atlas"] = module  # dataclasses needs the module registered
    spec.loader.exec_module(module)
    return module


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

    def test_plain_help_is_rendered_from_the_atlas(self) -> None:
        # Single-source help: plain `sbp help` and `help --human` both render
        # lib/sbp_help_human.py's atlas(). Every atlas invocation must appear
        # verbatim in plain help — a missing row means the wiring regressed to
        # a hand-maintained copy.
        module = _load_atlas_module()

        plain = _run_sbp("help").stdout
        self.assertNotIn("\x1b[", plain)
        self.assertIn("Examples:", plain)
        for group in module.atlas("sbp"):
            for cmd in group.cmds:
                self.assertIn(cmd.invocation, plain, f"atlas row missing from plain help: {cmd.invocation}")

    def test_atlas_and_capabilities_agree_on_the_verb_set(self) -> None:
        # The atlas (help) and capabilities (machine contract) are the two
        # remaining inventories. Compare their top-level verb sets exactly,
        # with every deliberate difference named here — silent drift fails.
        module = _load_atlas_module()

        atlas_verbs = set()
        for group in module.atlas("sbp"):
            for cmd in group.cmds:
                tokens = cmd.invocation.split()
                if not tokens or tokens[0] != "sbp":
                    continue  # info rows like the bare `profiles` line
                if len(tokens) == 1:
                    continue  # bare `sbp` home view
                atlas_verbs.add(tokens[1])

        capabilities = json.loads(_run_sbp("capabilities", "--json").stdout)
        caps_verbs = set()
        for entry in capabilities["commands"]:
            name = entry["name"]
            if name.startswith("skill-"):
                name = "skill"  # skill-why/pull/... are skill subverbs
            if name == "bulk":
                name = "launch"  # documented alias
            caps_verbs.add(name)

        # Deliberate one-sided entries (update alongside a real surface change):
        atlas_only = {
            "m",     # marketing-overlay toggle shorthand; capability is `overlay`
            "sync",  # deprecated legacy shorthand for skill add; kept out of the machine contract
        }
        self.assertEqual(
            atlas_verbs - atlas_only, caps_verbs,
            "help atlas and capabilities drifted — add the verb to both (or to the allowlist above)",
        )

    def test_capabilities_declares_help_command(self) -> None:
        result = _run_sbp("capabilities", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        commands = {command["name"]: command for command in payload["commands"]}
        self.assertIn("help", commands)
        self.assertIn("--human", commands["help"]["notes"])


if __name__ == "__main__":
    unittest.main()
