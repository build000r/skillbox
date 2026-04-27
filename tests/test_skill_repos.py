"""Tests for repo-based skill sync, drift detection, and filtered copy."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
MANAGER = ENV_MANAGER_DIR / "manage.py"

if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from runtime_manager.shared import (
    DEFAULT_SKILLIGNORE_PATTERNS,
    SKILL_REPOS_CONFIG_VERSION,
    SKILL_REPOS_LOCKFILE_VERSION,
    _load_skillignore,
    _matches_skillignore,
    _clone_or_fetch_repo,
    _resolve_skill_dirs,
    clone_dir_name,
    directory_tree_sha256,
    file_sha256,
    filtered_copy_skill,
    load_json_file,
    load_skill_repos_config,
    write_json_file,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestSkillReposConfig(unittest.TestCase):
    """Tests for skill_repos config parsing and validation."""

    def test_valid_config_with_repo_and_path_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "skill-repos.yaml"
            config_path.write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - repo: build000r/skills\n"
                "    ref: main\n"
                "    pick: [ask-cascade, describe]\n"
                "  - path: ./skills\n"
                "    pick: [dev-sanity]\n",
                encoding="utf-8",
            )
            config = load_skill_repos_config(config_path)
            self.assertEqual(config["version"], 2)
            self.assertEqual(len(config["skill_repos"]), 2)
            self.assertEqual(config["skill_repos"][0]["repo"], "build000r/skills")
            self.assertEqual(config["skill_repos"][1]["path"], "./skills")

    def test_missing_config_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            load_skill_repos_config(Path("/nonexistent/skill-repos.yaml"))
        self.assertIn("SKILL_CONFIG_INVALID", str(ctx.exception))

    def test_wrong_version_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "skill-repos.yaml"
            config_path.write_text("version: 99\nskill_repos: []\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                load_skill_repos_config(config_path)
            self.assertIn("SKILL_CONFIG_INVALID", str(ctx.exception))

    def test_repo_entry_without_ref_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "skill-repos.yaml"
            config_path.write_text(
                "version: 2\nskill_repos:\n  - repo: foo/bar\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as ctx:
                load_skill_repos_config(config_path)
            self.assertIn("requires a 'ref'", str(ctx.exception))

    def test_entry_with_both_repo_and_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "skill-repos.yaml"
            config_path.write_text(
                "version: 2\nskill_repos:\n  - repo: foo/bar\n    path: ./x\n    ref: main\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as ctx:
                load_skill_repos_config(config_path)
            self.assertIn("exactly one", str(ctx.exception))

    def test_empty_skill_repos_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "skill-repos.yaml"
            config_path.write_text("version: 2\nskill_repos: []\n", encoding="utf-8")
            config = load_skill_repos_config(config_path)
            self.assertEqual(config["skill_repos"], [])


class TestCloneDirName(unittest.TestCase):
    def test_owner_repo(self) -> None:
        self.assertEqual(clone_dir_name("build000r/skills"), "build000r-skills")

    def test_nested_slashes(self) -> None:
        self.assertEqual(clone_dir_name("org/sub/repo"), "org-sub-repo")


class TestCloneOrFetchRepo(unittest.TestCase):
    def test_existing_clone_raises_when_ref_checkout_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_root = Path(tmpdir)
            clone_path = clone_root / "owner-skills"
            clone_path.mkdir()

            def fake_run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                if args[:2] == ["git", "status"]:
                    return _completed()
                if args[:2] == ["git", "fetch"]:
                    return _completed()
                if args[:2] == ["git", "checkout"]:
                    return _completed(1, stderr="unknown revision")
                self.fail(f"unexpected command after checkout failure: {args}")

            with mock.patch("runtime_manager.shared.run_command", side_effect=fake_run_command):
                with self.assertRaises(RuntimeError) as ctx:
                    _clone_or_fetch_repo("owner/skills", "missing-ref", clone_root, dry_run=False)

            self.assertIn("git checkout failed", str(ctx.exception))

    def test_existing_clone_raises_when_pull_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_root = Path(tmpdir)
            clone_path = clone_root / "owner-skills"
            clone_path.mkdir()

            def fake_run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                if args[:2] == ["git", "status"]:
                    return _completed()
                if args[:2] == ["git", "fetch"]:
                    return _completed()
                if args[:2] == ["git", "checkout"]:
                    return _completed()
                if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                    return _completed(stdout="main\n")
                if args[:3] == ["git", "pull", "--ff-only"]:
                    return _completed(1, stderr="not possible to fast-forward")
                self.fail(f"unexpected command after pull failure: {args}")

            with mock.patch("runtime_manager.shared.run_command", side_effect=fake_run_command):
                with self.assertRaises(RuntimeError) as ctx:
                    _clone_or_fetch_repo("owner/skills", "main", clone_root, dry_run=False)

            self.assertIn("git pull --ff-only failed", str(ctx.exception))

    def test_existing_clone_does_not_pull_detached_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_root = Path(tmpdir)
            clone_path = clone_root / "owner-skills"
            clone_path.mkdir()
            commands: list[list[str]] = []

            def fake_run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                commands.append(args)
                if args[:2] == ["git", "status"]:
                    return _completed()
                if args[:2] == ["git", "fetch"]:
                    return _completed()
                if args[:2] == ["git", "checkout"]:
                    return _completed()
                if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                    return _completed(stdout="HEAD\n")
                if args == ["git", "rev-parse", "HEAD"]:
                    return _completed(stdout="abc123\n")
                self.fail(f"unexpected command: {args}")

            with mock.patch("runtime_manager.shared.run_command", side_effect=fake_run_command):
                action, clone_path, commit = _clone_or_fetch_repo("owner/skills", "v1.2.3", clone_root, dry_run=False)

            self.assertEqual(action, "fetched")
            self.assertEqual(clone_path, clone_root / "owner-skills")
            self.assertEqual(commit, "abc123")
            self.assertNotIn(["git", "pull", "--ff-only"], commands)

    def test_new_clone_checks_out_declared_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_root = Path(tmpdir)
            commands: list[list[str]] = []

            def fake_run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                commands.append(args)
                if args[:2] == ["git", "clone"]:
                    (clone_root / "owner-skills").mkdir()
                    return _completed()
                if args[:2] == ["git", "checkout"]:
                    return _completed()
                if args[:2] == ["git", "rev-parse"]:
                    return _completed(stdout="abc123\n")
                self.fail(f"unexpected command: {args}")

            with mock.patch("runtime_manager.shared.run_command", side_effect=fake_run_command):
                action, clone_path, commit = _clone_or_fetch_repo("owner/skills", "main", clone_root, dry_run=False)

            self.assertEqual(action, "cloned")
            self.assertEqual(clone_path, clone_root / "owner-skills")
            self.assertEqual(commit, "abc123")
            self.assertIn(["git", "checkout", "main"], commands)


class TestSkillignore(unittest.TestCase):
    """Tests for .skillignore loading and pattern matching."""

    def test_default_patterns_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            patterns = _load_skillignore(Path(tmpdir))
            self.assertEqual(patterns, DEFAULT_SKILLIGNORE_PATTERNS)

    def test_custom_patterns_extend_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir)
            (skill_dir / ".skillignore").write_text("*.log\ntemp/\n", encoding="utf-8")
            patterns = _load_skillignore(skill_dir)
            self.assertIn("*.log", patterns)
            self.assertIn("temp/", patterns)
            for default in DEFAULT_SKILLIGNORE_PATTERNS:
                self.assertIn(default, patterns)

    def test_matches_git_dir(self) -> None:
        self.assertTrue(_matches_skillignore(".git/config", DEFAULT_SKILLIGNORE_PATTERNS))

    def test_matches_pycache_dir(self) -> None:
        self.assertTrue(_matches_skillignore("__pycache__/module.pyc", DEFAULT_SKILLIGNORE_PATTERNS))

    def test_matches_pyc_file(self) -> None:
        self.assertTrue(_matches_skillignore("module.pyc", DEFAULT_SKILLIGNORE_PATTERNS))

    def test_matches_modes_dir(self) -> None:
        self.assertTrue(_matches_skillignore("modes/draft.md", DEFAULT_SKILLIGNORE_PATTERNS))

    def test_matches_briefs_dir(self) -> None:
        self.assertTrue(_matches_skillignore("briefs/brief.md", DEFAULT_SKILLIGNORE_PATTERNS))

    def test_matches_ds_store(self) -> None:
        self.assertTrue(_matches_skillignore(".DS_Store", DEFAULT_SKILLIGNORE_PATTERNS))

    def test_no_match_for_skill_md(self) -> None:
        self.assertFalse(_matches_skillignore("SKILL.md", DEFAULT_SKILLIGNORE_PATTERNS))

    def test_no_match_for_references(self) -> None:
        self.assertFalse(_matches_skillignore("references/guide.md", DEFAULT_SKILLIGNORE_PATTERNS))


class TestFilteredCopy(unittest.TestCase):
    """Tests for filtered_copy_skill."""

    def test_copies_skill_files_excluding_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            target = Path(tmpdir) / "target"

            # Create skill structure
            source.mkdir()
            (source / "SKILL.md").write_text("# Test Skill\n")
            (source / "references").mkdir()
            (source / "references" / "guide.md").write_text("guide\n")
            (source / "__pycache__").mkdir()
            (source / "__pycache__" / "mod.pyc").write_text("bytecode\n")
            (source / "modes").mkdir()
            (source / "modes" / "draft.md").write_text("draft\n")
            (source / ".DS_Store").write_text("junk\n")

            tree_sha = filtered_copy_skill(source, target)

            self.assertTrue((target / "SKILL.md").is_file())
            self.assertTrue((target / "references" / "guide.md").is_file())
            self.assertFalse((target / "__pycache__").exists())
            self.assertFalse((target / "modes").exists())
            self.assertFalse((target / ".DS_Store").exists())
            self.assertIsNotNone(tree_sha)
            self.assertEqual(len(tree_sha), 64)

    def test_respects_custom_skillignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            target = Path(tmpdir) / "target"

            source.mkdir()
            (source / "SKILL.md").write_text("# Test\n")
            (source / "secret.key").write_text("secret\n")
            (source / ".skillignore").write_text("*.key\n", encoding="utf-8")

            filtered_copy_skill(source, target)

            self.assertTrue((target / "SKILL.md").is_file())
            self.assertFalse((target / "secret.key").exists())
            self.assertFalse((target / ".skillignore").exists())

    def test_idempotent_tree_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            target1 = Path(tmpdir) / "target1"
            target2 = Path(tmpdir) / "target2"

            source.mkdir()
            (source / "SKILL.md").write_text("# Same\n")

            sha1 = filtered_copy_skill(source, target1)
            sha2 = filtered_copy_skill(source, target2)
            self.assertEqual(sha1, sha2)

    def test_refuses_same_source_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "skill"
            source.mkdir()
            (source / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

            with self.assertRaises(RuntimeError) as ctx:
                filtered_copy_skill(source, source)

            self.assertIn("overlapping source and target", str(ctx.exception))
            self.assertTrue((source / "SKILL.md").is_file())

    def test_refuses_target_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "skill"
            target = source / "installed"
            source.mkdir()
            (source / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

            with self.assertRaises(RuntimeError) as ctx:
                filtered_copy_skill(source, target)

            self.assertIn("overlapping source and target", str(ctx.exception))
            self.assertFalse(target.exists())

    def test_refuses_source_inside_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "install"
            source = target / "skill"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

            with self.assertRaises(RuntimeError) as ctx:
                filtered_copy_skill(source, target)

            self.assertIn("overlapping source and target", str(ctx.exception))
            self.assertTrue((source / "SKILL.md").is_file())


class TestResolveSkillDirs(unittest.TestCase):
    """Tests for _resolve_skill_dirs."""

    def test_pick_list_resolves_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "ask-cascade").mkdir()
            (root / "ask-cascade" / "SKILL.md").write_text("# Skill\n")
            (root / "describe").mkdir()
            (root / "describe" / "SKILL.md").write_text("# Skill\n")

            entry = {"pick": ["ask-cascade", "describe"]}
            result = _resolve_skill_dirs(entry, root, "skills")
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0][0], "ask-cascade")
            self.assertEqual(result[1][0], "describe")

    def test_mono_skill_repo_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "SKILL.md").write_text("# Mono Skill\n")

            entry = {"repo": "owner/mono-skill"}
            result = _resolve_skill_dirs(entry, root, "mono-skill")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0][0], "mono-skill")

    def test_no_pick_no_skill_md_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entry = {"repo": "owner/multi"}
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_skill_dirs(entry, root, "multi")
            self.assertIn("SKILL_CONFIG_INVALID", str(ctx.exception))

    def test_pick_list_with_missing_skill_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entry = {"pick": ["nonexistent"]}
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_skill_dirs(entry, root, "skills")
            self.assertIn("SKILL_NOT_FOUND_IN_REPO", str(ctx.exception))


class TestLockFileFormat(unittest.TestCase):
    """Tests for the lock file format produced by sync."""

    def test_lock_file_from_real_sync(self) -> None:
        lock_path = ROOT_DIR / "workspace" / "skill-repos.lock.json"
        if not lock_path.is_file():
            self.skipTest("Lock file not present — run sync first")

        lock = load_json_file(lock_path)
        self.assertEqual(lock["version"], SKILL_REPOS_LOCKFILE_VERSION)
        self.assertIn("config_sha", lock)
        self.assertIn("synced_at", lock)
        self.assertIsInstance(lock["skills"], list)

        for skill in lock["skills"]:
            self.assertIn("name", skill)
            self.assertIn("install_tree_sha", skill)
            self.assertEqual(len(skill["install_tree_sha"]), 64)


if __name__ == "__main__":
    unittest.main()
