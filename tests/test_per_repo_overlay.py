from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import skill_visibility as sv  # noqa: E402


def _make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    return repo


def _write_override(repo: Path, text: str) -> None:
    policy_dir = repo / ".skillbox"
    policy_dir.mkdir()
    (policy_dir / "skill-overrides.yaml").write_text(text, encoding="utf-8")


class PerRepoOverlayTests(unittest.TestCase):
    def _env(self, state_path: Path) -> dict[str, str]:
        return {
            "SKILLBOX_OVERLAY_STATE": str(state_path),
            "SKILLBOX_OVERLAYS": "",
            "SKILLBOX_CLI_OVERLAYS": "",
        }

    def test_repo_enable_is_active_only_for_that_cwd_and_explains_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root, "repo")
            other = _make_repo(root, "other")
            state_path = root / "global-overlays"
            _write_override(repo, "version: 1\noverlays:\n  enable: [hardening]\n")
            model = {
                "active_clients": ["personal"],
                "clients": [
                    {
                        "id": "personal",
                        "context": {
                            "skill_scope": {
                                "rules": [
                                    {
                                        "id": "hardening-rule",
                                        "skills": ["audit-hardening"],
                                        "overlay": "hardening",
                                        "paths": [str(repo)],
                                    }
                                ]
                            }
                        },
                    }
                ],
                "skills": [],
            }

            with mock.patch.dict(os.environ, self._env(state_path), clear=False):
                self.assertEqual(sv.active_overlays(), set())
                self.assertEqual(sv.active_overlays(other), set())
                self.assertEqual(sv.active_overlays(repo), {"hardening"})
                self.assertEqual(
                    [
                        rule["id"]
                        for rule in sv.collect_skill_visibility(
                            model,
                            cwd=str(repo),
                            include_global=False,
                            include_project=False,
                        )["matched_scope_rules"]
                    ],
                    ["hardening-rule"],
                )
                self.assertEqual(
                    sv.collect_skill_visibility(
                        model,
                        cwd=str(other),
                        include_global=False,
                        include_project=False,
                    )["matched_scope_rules"],
                    [],
                )
                records = sv.active_overlay_records(repo)

            hardening = next(record for record in records if record["name"] == "hardening")
            self.assertTrue(hardening["enabled"])
            self.assertEqual(hardening["why"], "repo-file")

    def test_repo_disable_beats_global_on_but_not_other_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root, "repo")
            other = _make_repo(root, "other")
            state_path = root / "global-overlays"
            state_path.write_text("hardening\n", encoding="utf-8")
            _write_override(repo, "version: 1\noverlays:\n  disable: [hardening]\n")

            with mock.patch.dict(os.environ, self._env(state_path), clear=False):
                self.assertEqual(sv.active_overlays(), {"hardening"})
                self.assertEqual(sv.active_overlays(other), {"hardening"})
                self.assertEqual(sv.active_overlays(repo), set())
                records = sv.active_overlay_records(repo)

            hardening = next(record for record in records if record["name"] == "hardening")
            self.assertFalse(hardening["enabled"])
            self.assertEqual(hardening["why"], "repo-file")

    def test_env_overlay_stays_top_and_explains_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root, "repo")
            state_path = root / "global-overlays"
            _write_override(repo, "version: 1\noverlays:\n  disable: [hardening]\n")
            env = self._env(state_path)
            env["SKILLBOX_OVERLAYS"] = "hardening"

            with mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual(sv.active_overlays(repo), {"hardening"})
                records = sv.active_overlay_records(repo)

            hardening = next(record for record in records if record["name"] == "hardening")
            self.assertTrue(hardening["enabled"])
            self.assertEqual(hardening["why"], "env")


if __name__ == "__main__":
    unittest.main()
