"""Regression tests for committed .skillbox/ override policy tracking.

These assert rules in the repo's tracked .gitignore. `git check-ignore` needs a
repository to run in, and the self-test gate executes from a `git archive`
extract (no .git), so the tests build a throwaway repo containing the same
.gitignore instead of assuming the source tree is one.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


class OverrideGitignoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="gitignore-probe-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.repo = Path(tmp)
        subprocess.run(
            ["git", "init", "--quiet", str(self.repo)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.copyfile(ROOT_DIR / ".gitignore", self.repo / ".gitignore")

    def _check_ignore(self, path: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "check-ignore", path],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_repo_override_file_is_not_ignored(self) -> None:
        result = self._check_ignore(".skillbox/skill-overrides.yaml")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_runtime_state_remains_ignored(self) -> None:
        result = self._check_ignore(".skillbox-state/example")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), ".skillbox-state/example")


if __name__ == "__main__":
    unittest.main()
