"""Regression tests for committed .skillbox/ override policy tracking."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def _check_ignore(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "check-ignore", path],
        cwd=ROOT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class OverrideGitignoreTests(unittest.TestCase):
    def test_repo_override_file_is_not_ignored(self) -> None:
        result = _check_ignore(".skillbox/skill-overrides.yaml")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_runtime_state_remains_ignored(self) -> None:
        result = _check_ignore(".skillbox-state/example")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), ".skillbox-state/example")


if __name__ == "__main__":
    unittest.main()
