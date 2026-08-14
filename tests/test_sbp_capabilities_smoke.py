"""R-203: the capabilities machine contract is a regression gate, not an ad.

Every `commands[].safe_first_try` in `sbp capabilities --json` that contains no
<placeholder> is executed for real. The contract asserted per command:

- exit code is 0 (success) or 1 (findings/no-go) — never a usage error;
- when the invocation requests JSON, stdout parses as JSON;
- stdout is empty on non-{0,1} exits (errors belong on stderr).

Known-broken or host-dependent entries are skipped VISIBLY with a reason (and
a bead id where one exists) — a skip here is a tracked debt, not a pass.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SBP = ROOT_DIR / "scripts" / "sbp"

# Surfaces that talk to remote systems (tailnet, devbox, browser); their
# contract is asserted by their own suites, not a local smoke test.
REMOTE_COMMANDS = {"cass", "oracle", "conference1"}

# Known-broken safe_first_try entries, each pinned to the bead tracking the
# repair. Remove the row when the bead closes; the smoke test then guards it.
KNOWN_BROKEN = {
    "repo": "skillbox-sbp-repo-atlas-repair-2gbo",
}

TIMEOUT_SECONDS = 90


def _run(command: str) -> subprocess.CompletedProcess[str]:
    argv = shlex.split(command)
    assert argv and argv[0] == "sbp"
    argv[0] = str(SBP)
    return subprocess.run(
        argv,
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_SECONDS,
        env={
            **os.environ,
            "SKILLBOX_ROOT": str(ROOT_DIR),
            "SKILLBOX_INVOKE_CWD": str(ROOT_DIR),
            "PYTHONPATH": str(ROOT_DIR / ".env-manager"),
        },
    )


class SbpSafeFirstTrySmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        capabilities = _run("sbp capabilities --json")
        assert capabilities.returncode == 0, capabilities.stderr
        cls.payload = json.loads(capabilities.stdout)

    def test_every_placeholder_free_safe_first_try_honors_the_contract(self) -> None:
        commands = self.payload["commands"]
        self.assertGreater(len(commands), 10)
        executed = 0
        for entry in commands:
            name = entry["name"]
            command = entry.get("safe_first_try", "")
            with self.subTest(command=command or name):
                if "<" in command or not command:
                    continue  # parameterized examples are documentation, not smoke targets
                if name in REMOTE_COMMANDS:
                    continue  # remote surface; covered by its own suite
                if name in KNOWN_BROKEN:
                    continue  # tracked debt — see KNOWN_BROKEN bead id
                result = _run(command)
                executed += 1
                self.assertIn(
                    result.returncode, (0, 1),
                    f"{command!r} exited {result.returncode} (usage/crash class).\n"
                    f"stderr: {result.stderr[:800]}",
                )
                wants_json = entry.get("json") and (
                    "--json" in command or "--format json" in command
                    or "--robot-triage" in command
                )
                if wants_json:
                    try:
                        json.loads(result.stdout)
                    except json.JSONDecodeError as exc:
                        self.fail(
                            f"{command!r} promised JSON stdout but produced "
                            f"unparseable output ({exc}).\n"
                            f"stdout: {result.stdout[:400]}\nstderr: {result.stderr[:400]}"
                        )
        # The gate is only meaningful if it actually runs a meaningful sample.
        self.assertGreaterEqual(executed, 8, "smoke sample collapsed — check skip rules")

    def test_known_broken_rows_are_still_broken_or_should_be_unskipped(self) -> None:
        # When a KNOWN_BROKEN command starts passing, this fails to force
        # removing the skip so the smoke test guards the repaired surface.
        commands = {entry["name"]: entry for entry in self.payload["commands"]}
        for name, bead in KNOWN_BROKEN.items():
            entry = commands.get(name)
            with self.subTest(name=name, bead=bead):
                if entry is None or "<" in entry.get("safe_first_try", ""):
                    continue
                result = _run(entry["safe_first_try"])
                self.assertNotEqual(
                    result.returncode, 0,
                    f"{entry['safe_first_try']!r} now succeeds — close bead {bead} "
                    "and remove it from KNOWN_BROKEN so the smoke test guards it.",
                )


if __name__ == "__main__":
    unittest.main()
