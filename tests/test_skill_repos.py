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
    _persist_skill_repo_lockfile,
    _resolve_skill_repo_entry_source,
    _resolve_skill_dirs,
    clone_dir_name,
    directory_tree_sha256,
    file_sha256,
    filtered_copy_skill,
    load_json_file,
    load_skill_repos_config,
    sync_skill_repo_sets,
    write_json_file,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _write_skill(source_root: Path, skill_name: str, body: str = "# Skill\n") -> Path:
    skill_dir = source_root / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


def _skill_repo_set_model(config_path: Path, lock_path: Path, clone_root: Path, install_root: Path) -> dict[str, object]:
    return {
        "skills": [
            {"kind": "inline-skill", "sync": {"mode": "clone-and-install"}},
            {
                "kind": "skill-repo-set",
                "sync": {"mode": "manual"},
                "skill_repos_config_host_path": str(config_path),
                "lock_path_host_path": str(lock_path),
                "clone_root_host_path": str(clone_root),
                "install_targets": [{"id": "codex", "host_path": str(install_root)}],
            },
            {
                "kind": "skill-repo-set",
                "sync": {"mode": "clone-and-install"},
                "skill_repos_config_host_path": str(config_path),
                "lock_path_host_path": str(lock_path),
                "clone_root_host_path": str(clone_root),
                "install_targets": [{"id": "codex", "host_path": str(install_root)}],
            },
        ]
    }


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
    def test_existing_clone_returns_dirty_without_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_root = Path(tmpdir)
            clone_path = clone_root / "owner-skills"
            clone_path.mkdir()
            commands: list[list[str]] = []

            def fake_run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                del cwd
                commands.append(args)
                if args[:2] == ["git", "status"]:
                    return _completed(stdout=" M SKILL.md\n")
                self.fail(f"unexpected command after dirty status: {args}")

            with mock.patch("runtime_manager.shared.run_command", side_effect=fake_run_command):
                action, resolved_path, commit = _clone_or_fetch_repo("owner/skills", "main", clone_root, dry_run=False)

            self.assertEqual(action, "SKILL_REPO_DIRTY")
            self.assertEqual(resolved_path, clone_path)
            self.assertIsNone(commit)
            self.assertEqual(commands, [["git", "status", "--porcelain"]])

    def test_existing_clone_raises_when_fetch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_root = Path(tmpdir)
            clone_path = clone_root / "owner-skills"
            clone_path.mkdir()

            def fake_run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                del cwd
                if args[:2] == ["git", "status"]:
                    return _completed()
                if args[:2] == ["git", "fetch"]:
                    return _completed(1, stderr="network down")
                self.fail(f"unexpected command after fetch failure: {args}")

            with mock.patch("runtime_manager.shared.run_command", side_effect=fake_run_command):
                with self.assertRaises(RuntimeError) as ctx:
                    _clone_or_fetch_repo("owner/skills", "main", clone_root, dry_run=False)

            self.assertIn("git fetch failed", str(ctx.exception))

    def test_clone_dry_run_returns_target_paths_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_root = Path(tmpdir)
            existing = clone_root / "owner-skills"
            existing.mkdir()

            fetched = _clone_or_fetch_repo("owner/skills", "main", clone_root, dry_run=True)
            shutil.rmtree(existing)
            cloned = _clone_or_fetch_repo("owner/skills", "main", clone_root, dry_run=True)

            self.assertEqual(fetched, ("fetched", existing, None))
            self.assertEqual(cloned, ("cloned", existing, None))

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

    def test_new_clone_falls_back_to_ssh_and_reports_clone_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_root = Path(tmpdir)
            commands: list[list[str]] = []

            def fake_run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                del cwd
                commands.append(args)
                if args == ["git", "clone", "https://github.com/owner/skills.git", str(clone_root / "owner-skills")]:
                    return _completed(1, stderr="https denied")
                if args == ["git", "clone", "git@github.com:owner/skills.git", str(clone_root / "owner-skills")]:
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
            self.assertEqual(commands[0][2], "https://github.com/owner/skills.git")
            self.assertEqual(commands[1][2], "git@github.com:owner/skills.git")

        with tempfile.TemporaryDirectory() as tmpdir:
            clone_root = Path(tmpdir)

            def fail_clone(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                del cwd
                if args[:2] == ["git", "clone"]:
                    return _completed(1, stderr="clone denied")
                self.fail(f"unexpected command after clone failure: {args}")

            with mock.patch("runtime_manager.shared.run_command", side_effect=fail_clone):
                with self.assertRaises(RuntimeError) as ctx:
                    _clone_or_fetch_repo("owner/skills", "main", clone_root, dry_run=False)
            self.assertIn("failed to clone", str(ctx.exception))


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

    def test_refuses_symlinked_skill_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            target = root / "target"
            secret = root / "outside-secret.txt"
            source.mkdir()
            (source / "SKILL.md").write_text("# Test\n", encoding="utf-8")
            secret.write_text("do not copy\n", encoding="utf-8")
            (source / "references").mkdir()
            (source / "references" / "secret.txt").symlink_to(secret)

            with self.assertRaises(RuntimeError) as ctx:
                filtered_copy_skill(source, target)

            self.assertIn("symlinked file", str(ctx.exception))
            self.assertFalse((target / "references" / "secret.txt").exists())

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


class TestResolveSkillRepoEntrySource(unittest.TestCase):
    def test_resolves_local_relative_paths_and_rejects_missing_required_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "workspace" / "skill-repos.yaml"
            config_path.parent.mkdir()
            source_root = config_path.parent / "local-skills"
            source_root.mkdir()
            actions: list[str] = []

            resolved = _resolve_skill_repo_entry_source(
                {"path": "./local-skills"},
                config_path,
                root / "clones",
                {"install_targets": []},
                False,
                actions,
            )

            self.assertEqual(resolved, (source_root.resolve(), "local-skills", None, None))

            missing_entry = {"path": "./missing-skills"}
            dry_run_result = _resolve_skill_repo_entry_source(
                missing_entry,
                config_path,
                root / "clones",
                {"install_targets": []},
                True,
                actions,
            )
            self.assertIsNone(dry_run_result)
            self.assertTrue(any(action.startswith("skip-local-path:") for action in actions))

            with self.assertRaises(RuntimeError) as ctx:
                _resolve_skill_repo_entry_source(
                    missing_entry,
                    config_path,
                    root / "clones",
                    {"install_targets": []},
                    False,
                    actions,
                )
            self.assertIn("local path does not exist", str(ctx.exception))

    def test_resolves_repo_sources_and_dry_run_install_plan_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "skill-repos.yaml"
            clone_root = root / "clones"
            install_root = root / "installed"
            actions: list[str] = []

            dry_run_result = _resolve_skill_repo_entry_source(
                {"repo": "owner/skills", "ref": "main", "pick": ["ask-cascade", "describe"]},
                config_path,
                clone_root,
                {"install_targets": [{"id": "codex", "host_path": str(install_root)}]},
                True,
                actions,
            )
            self.assertIsNone(dry_run_result)
            self.assertIn("skill-repo-cloned: owner/skills", actions)
            self.assertIn(f"install-skill: ask-cascade -> {install_root / 'ask-cascade'}", actions)
            self.assertIn(f"install-skill: describe -> {install_root / 'describe'}", actions)

            repo_root = clone_root / "owner-skills"
            repo_root.mkdir(parents=True)
            with mock.patch(
                "runtime_manager.shared._clone_or_fetch_repo",
                return_value=("fetched", repo_root, "abc123"),
            ):
                fetched = _resolve_skill_repo_entry_source(
                    {"repo": "owner/skills", "ref": "main"},
                    config_path,
                    clone_root,
                    {"install_targets": []},
                    False,
                    actions,
                )
            self.assertEqual(fetched, (repo_root, "skills", "owner/skills", "abc123"))

            with mock.patch(
                "runtime_manager.shared._clone_or_fetch_repo",
                return_value=("SKILL_REPO_DIRTY", repo_root, None),
            ):
                dirty = _resolve_skill_repo_entry_source(
                    {"repo": "owner/skills", "ref": "main"},
                    config_path,
                    clone_root,
                    {"install_targets": []},
                    False,
                    actions,
                )
            self.assertIsNone(dirty)
            self.assertTrue(any(action.startswith("SKILL_REPO_DIRTY: owner/skills") for action in actions))


class TestSyncSkillRepoSets(unittest.TestCase):
    def test_sync_installs_local_skill_repos_and_preserves_unchanged_lock_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "source-skills"
            _write_skill(source_root, "ask-cascade", "# Ask Cascade\n")
            _write_skill(source_root, "describe", "# Describe\n")
            config_path = root / "skill-repos.yaml"
            lock_path = root / "skill-repos.lock.json"
            clone_root = root / "clones"
            install_root = root / "installed"
            config_path.write_text(
                "version: 2\n"
                "distributors:\n"
                "  - id: acme-skills\n"
                "    url: https://skills.example.test/api/v1\n"
                "    client_id: client-42\n"
                "    auth:\n"
                "      method: api-key\n"
                "      key_env: ACME_DISTRIBUTOR_KEY\n"
                "    verification:\n"
                "      public_key: test-public-key\n"
                "skill_repos:\n"
                "  - distributor: acme-skills\n"
                "  - path: ./source-skills\n"
                "    pick: [ask-cascade, describe]\n",
                encoding="utf-8",
            )
            model = _skill_repo_set_model(config_path, lock_path, clone_root, install_root)

            with mock.patch.dict(os.environ, {"ACME_DISTRIBUTOR_KEY": "test-token"}):
                first_actions = sync_skill_repo_sets(model, dry_run=False)
                first_lock = load_json_file(lock_path)
                second_actions = sync_skill_repo_sets(model, dry_run=False)
                second_lock = load_json_file(lock_path)

            self.assertTrue((install_root / "ask-cascade" / "SKILL.md").is_file())
            self.assertTrue((install_root / "describe" / "SKILL.md").is_file())
            self.assertEqual([skill["name"] for skill in first_lock["skills"]], ["ask-cascade", "describe"])
            self.assertEqual(first_lock["synced_at"], second_lock["synced_at"])
            self.assertIn(f"write-lockfile: {lock_path}", first_actions)
            self.assertIn(f"lockfile-unchanged: {lock_path}", second_actions)

    def test_sync_mirrors_agent_skill_targets_to_host_home_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "source-skills"
            _write_skill(source_root, "ntm", "# NTM\n")
            config_path = root / "skill-repos.yaml"
            lock_path = root / "skill-repos.lock.json"
            clone_root = root / "clones"
            install_root = root / "state" / "home" / ".codex" / "skills"
            host_home = root / "host-home"
            config_path.write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - path: ./source-skills\n"
                "    pick: [ntm]\n",
                encoding="utf-8",
            )
            model = _skill_repo_set_model(config_path, lock_path, clone_root, install_root)

            with mock.patch.dict(os.environ, {"SKILLBOX_HOST_HOME_ROOT": str(host_home)}):
                actions = sync_skill_repo_sets(model, dry_run=False)

            installed = install_root / "ntm"
            mirrored = host_home / ".codex" / "skills" / "ntm"
            self.assertTrue((installed / "SKILL.md").is_file())
            self.assertTrue(mirrored.is_symlink())
            self.assertEqual(mirrored.resolve(), installed.resolve())
            self.assertIn(f"mirror-host-skill: ntm -> {mirrored}", actions)

    def test_sync_dry_run_records_repo_install_plan_and_defers_lockfile_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "skill-repos.yaml"
            lock_path = root / "skill-repos.lock.json"
            clone_root = root / "clones"
            install_root = root / "installed"
            config_path.write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - repo: owner/skills\n"
                "    ref: main\n"
                "    pick: [ask-cascade]\n",
                encoding="utf-8",
            )
            model = _skill_repo_set_model(config_path, lock_path, clone_root, install_root)

            actions = sync_skill_repo_sets(model, dry_run=True)

            self.assertIn("skill-repo-cloned: owner/skills", actions)
            self.assertIn(f"install-skill: ask-cascade -> {install_root / 'ask-cascade'}", actions)
            self.assertIn(f"write-lockfile: {lock_path}", actions)
            self.assertFalse(lock_path.exists())
            self.assertFalse(install_root.exists())

    def test_persist_lockfile_recovers_from_invalid_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "skill-repos.yaml"
            lock_path = root / "skill-repos.lock.json"
            config_path.write_text("version: 2\nskill_repos: []\n", encoding="utf-8")
            lock_path.write_text("{bad json\n", encoding="utf-8")
            actions: list[str] = []

            _persist_skill_repo_lockfile(
                lock_path,
                config_path,
                [{"name": "ask-cascade", "resolved_commit": "abc", "install_tree_sha": "def"}],
                actions,
            )

            lock = load_json_file(lock_path)
            self.assertEqual(lock["version"], SKILL_REPOS_LOCKFILE_VERSION)
            self.assertEqual(lock["skills"][0]["name"], "ask-cascade")
            self.assertIn(f"write-lockfile: {lock_path}", actions)


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
