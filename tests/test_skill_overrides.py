"""Tests for repo-local .skillbox/skill-overrides.yaml inputs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.errors import OVERRIDE_PARSE_ERROR  # noqa: E402
from runtime_manager.policy_eval import _repo_override_policy  # noqa: E402
from tests.fixture_fleet import build_fixture_fleet  # noqa: E402


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


class RepoSkillOverridePolicyTests(unittest.TestCase):
    def test_reads_fixture_repo_override_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fleet = build_fixture_fleet(tmpdir)

            policy = _repo_override_policy(fleet.repo("healthy"))

        self.assertTrue(policy["ok"], policy["errors"])
        self.assertEqual(policy["pin_on"], ["needs-beads"])
        self.assertEqual(policy["pin_off"], ["tiny-marketing"])
        self.assertEqual(policy["opt_out_global"], ["project-status-mmdx"])
        self.assertEqual(policy["overlays"]["enable"], ["marketing"])
        self.assertEqual(policy["overlays"]["disable"], ["swarm"])
        self.assertEqual(policy["defaults"], ["tiny-ui"])
        self.assertEqual(policy["reason"], "fixture override")
        self.assertTrue(policy["_policy_path"].endswith(".skillbox/skill-overrides.yaml"))

    def test_subdir_invocation_resolves_to_git_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\npin_on: [wiki]\n",
                encoding="utf-8",
            )
            subdir = repo / "src" / "pkg"
            subdir.mkdir(parents=True)

            policy = _repo_override_policy(subdir)

        self.assertEqual(policy["pin_on"], ["wiki"])
        self.assertEqual(policy["_repo_root"], str(repo))

    def test_unknown_top_level_key_is_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\nsurprise: true\n",
                encoding="utf-8",
            )

            policy = _repo_override_policy(repo)

        self.assertFalse(policy["ok"])
        self.assertEqual(policy["pin_on"], [])
        self.assertEqual(policy["errors"][0]["code"], OVERRIDE_PARSE_ERROR)
        self.assertEqual(policy["errors"][0]["key"], "surprise")
        self.assertIn("surprise", policy["errors"][0]["message"])

    def test_wrong_overlay_shape_is_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\noverlays: []\n",
                encoding="utf-8",
            )

            policy = _repo_override_policy(repo)

        self.assertFalse(policy["ok"])
        self.assertEqual(policy["errors"][0]["key"], "overlays")
        self.assertIn("mapping", policy["errors"][0]["message"])

    def test_malformed_yaml_fails_safe_with_parse_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\npin_on: [unterminated\n",
                encoding="utf-8",
            )

            policy = _repo_override_policy(repo)

        self.assertFalse(policy["ok"])
        self.assertEqual(policy["pin_on"], [])
        self.assertEqual(policy["overlays"], {"enable": [], "disable": []})
        self.assertEqual(policy["errors"][0]["code"], OVERRIDE_PARSE_ERROR)
        self.assertIn("Failed to parse", policy["errors"][0]["message"])

    def test_missing_override_file_is_empty_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))

            policy = _repo_override_policy(repo)

        self.assertTrue(policy["ok"], policy["errors"])
        self.assertEqual(policy["pin_on"], [])
        self.assertEqual(policy["pin_off"], [])
        self.assertEqual(policy["defaults"], [])


if __name__ == "__main__":
    unittest.main()
