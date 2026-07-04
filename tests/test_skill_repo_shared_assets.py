from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER))

from runtime_manager import shared, validation  # noqa: E402


class SkillRepoSharedAssetTests(unittest.TestCase):
    def _write_source(
        self,
        source_root: Path,
        skill_name: str,
        *,
        shared_helper_text: str = "print('shared')\n",
    ) -> None:
        skill_dir = source_root / skill_name
        shared_dir = source_root / shared.SHARED_SKILL_ASSET_DIR / "scripts"
        skill_dir.mkdir(parents=True)
        shared_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill_name}\n", encoding="utf-8")
        (shared_dir / "helper.py").write_text(shared_helper_text, encoding="utf-8")

    def test_sync_installs_shared_assets_without_locking_them_as_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "source"
            install_root = root / "installed"
            lock_path = root / "skill-repos.lock.json"
            config_path = root / "skill-repos.yaml"

            self._write_source(source_root, "domain-planner")
            config_path.write_text(
                f"version: {shared.SKILL_REPOS_CONFIG_VERSION}\n"
                "skill_repos:\n"
                "  - path: ./source\n"
                "    pick: [domain-planner]\n",
                encoding="utf-8",
            )

            skillset = {
                "id": "repo-set",
                "kind": "skill-repo-set",
                "sync": {"mode": "clone-and-install"},
                "skill_repos_config_host_path": str(config_path),
                "lock_path_host_path": str(lock_path),
                "clone_root_host_path": str(root / "clones"),
                "install_targets": [{"id": "codex", "host_path": str(install_root)}],
            }

            actions = shared.sync_skill_repo_sets({"env": {}, "skills": [skillset]}, dry_run=False)

            self.assertIn(
                f"install-shared-skill-asset: _shared -> {install_root / '_shared'}",
                actions,
            )
            self.assertTrue((install_root / "domain-planner" / "SKILL.md").is_file())
            self.assertTrue((install_root / "_shared" / "scripts" / "helper.py").is_file())

            lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual([entry["name"] for entry in lock_payload["skills"]], ["domain-planner"])

            validation_result = validation._validate_skillset_locks_and_installs(  # noqa: SLF001
                skillset,
                {"repo-set": {"domain-planner"}},
                {"domain-planner": "repo-set"},
                {"domain-planner"},
            )
            self.assertEqual(validation_result, ([], [], [], [], [], []))

            shutil.rmtree(install_root / "_shared")
            missing_shared = validation._validate_skillset_locks_and_installs(  # noqa: SLF001
                skillset,
                {"repo-set": {"domain-planner"}},
                {"domain-planner": "repo-set"},
                {"domain-planner"},
            )
            self.assertEqual(missing_shared[4], ["repo-set: SHARED_ASSET_NOT_INSTALLED: _shared missing in codex"])

    def test_shared_asset_dry_run_reports_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "source"
            install_root = root / "installed"
            lock_path = root / "skill-repos.lock.json"
            config_path = root / "skill-repos.yaml"

            self._write_source(source_root, "domain-planner")
            config_path.write_text(
                f"version: {shared.SKILL_REPOS_CONFIG_VERSION}\n"
                "skill_repos:\n"
                "  - path: ./source\n"
                "    pick: [domain-planner]\n",
                encoding="utf-8",
            )
            skillset = {
                "id": "repo-set",
                "kind": "skill-repo-set",
                "sync": {"mode": "clone-and-install"},
                "skill_repos_config_host_path": str(config_path),
                "lock_path_host_path": str(lock_path),
                "clone_root_host_path": str(root / "clones"),
                "install_targets": [{"id": "codex", "host_path": str(install_root)}],
            }

            actions = shared.sync_skill_repo_sets({"env": {}, "skills": [skillset]}, dry_run=True)

            self.assertIn(
                f"install-shared-skill-asset: _shared -> {install_root / '_shared'}",
                actions,
            )
            self.assertFalse((install_root / "_shared").exists())
            self.assertFalse(lock_path.exists())

    def test_conflicting_shared_asset_sources_fail_before_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first_source = root / "first"
            second_source = root / "second"
            install_root = root / "installed"
            config_path = root / "skill-repos.yaml"

            self._write_source(first_source, "alpha", shared_helper_text="print('first')\n")
            self._write_source(second_source, "beta", shared_helper_text="print('second')\n")
            config_path.write_text(
                f"version: {shared.SKILL_REPOS_CONFIG_VERSION}\n"
                "skill_repos:\n"
                "  - path: ./first\n"
                "    pick: [alpha]\n"
                "  - path: ./second\n"
                "    pick: [beta]\n",
                encoding="utf-8",
            )
            skillset = {
                "id": "repo-set",
                "kind": "skill-repo-set",
                "sync": {"mode": "clone-and-install"},
                "skill_repos_config_host_path": str(config_path),
                "lock_path_host_path": str(root / "skill-repos.lock.json"),
                "clone_root_host_path": str(root / "clones"),
                "install_targets": [{"id": "codex", "host_path": str(install_root)}],
            }

            with self.assertRaisesRegex(RuntimeError, "SKILL_SHARED_ASSET_CONFLICT"):
                shared.sync_skill_repo_sets({"env": {}, "skills": [skillset]}, dry_run=True)

            self.assertFalse((install_root / "_shared").exists())


if __name__ == "__main__":
    unittest.main()
