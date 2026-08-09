"""Tests for runtime_manager.git_inventory -- sbp's read-only estate-git-scan.

Fixture repos are real ``git init`` repos built inside a TemporaryDirectory,
with git configuration pinned via ``GIT_CONFIG_NOSYSTEM`` /
``GIT_CONFIG_GLOBAL`` so the suite passes on a clean machine regardless of the
operator's global git config. Deep fixture coverage of every class lands in a
separate tests bead; this file covers the contract's minimum surface.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import git_inventory  # noqa: E402


class GitFixtureCase(unittest.TestCase):
    """Base case: temp dir + hermetic git configuration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="git-inventory-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n"
            "\temail = fixture@example.invalid\n"
            "\tname = Git Inventory Fixture\n"
            "[init]\n"
            "\tdefaultBranch = main\n"
            "[commit]\n"
            "\tgpgsign = false\n"
            "[tag]\n"
            "\tgpgsign = false\n",
            encoding="utf-8",
        )
        patcher = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def make_repo(self, name: str, *, commit: bool = True) -> Path:
        repo = self.tmp / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "main")
        if commit:
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "tracked.txt")
            self.git(repo, "commit", "-q", "-m", "base")
        return repo

    def make_clone_pair(self, name: str) -> tuple[Path, Path]:
        """(origin, clone) -- clone via file:// so upstream exists, no network."""
        origin = self.make_repo(f"{name}-origin")
        clone = self.tmp / f"{name}-clone"
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        return origin, clone


class ProbeClassificationTests(GitFixtureCase):
    def test_clean_current_with_upstream(self) -> None:
        _, clone = self.make_clone_pair("clean")
        record = git_inventory.probe_repo(clone)
        self.assertEqual(record.classes, frozenset({"clean-current"}))
        self.assertEqual(record.primary_class, "clean-current")
        self.assertEqual(record.branch, "main")
        self.assertEqual(record.upstream, "origin/main")
        self.assertEqual((record.ahead, record.behind), (0, 0))
        self.assertEqual(
            (record.staged, record.unstaged, record.untracked), (0, 0, 0)
        )
        self.assertIsNone(record.mid_op)
        self.assertIsNone(record.error)
        self.assertFalse(record.bare)

    def test_dirty_staged_only(self) -> None:
        repo = self.make_repo("staged")
        (repo / "new.txt").write_text("staged\n", encoding="utf-8")
        self.git(repo, "add", "new.txt")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(
            (record.staged, record.unstaged, record.untracked), (1, 0, 0)
        )
        self.assertIn("dirty", record.classes)
        self.assertEqual(record.primary_class, "dirty")

    def test_dirty_unstaged_only(self) -> None:
        repo = self.make_repo("unstaged")
        (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(
            (record.staged, record.unstaged, record.untracked), (0, 1, 0)
        )
        self.assertIn("dirty", record.classes)
        self.assertEqual(record.primary_class, "dirty")

    def test_dirty_untracked_only(self) -> None:
        repo = self.make_repo("untracked")
        (repo / "loose.txt").write_text("loose\n", encoding="utf-8")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(
            (record.staged, record.unstaged, record.untracked), (0, 0, 1)
        )
        self.assertIn("dirty", record.classes)
        self.assertEqual(record.primary_class, "dirty")

    def test_stash_class_from_first_stash(self) -> None:
        repo = self.make_repo("stash")
        (repo / "tracked.txt").write_text("stash me\n", encoding="utf-8")
        self.git(repo, "stash", "-q")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(record.stash_count, 1)
        self.assertIn("stash", record.classes)
        self.assertNotIn("dirty", record.classes)
        # Below the >=5 threshold the primary falls through (here: no-remote).
        self.assertEqual(record.primary_class, "no-remote")

    def test_stash_heavy_primary_at_threshold(self) -> None:
        repo = self.make_repo("stash-heavy")
        for i in range(git_inventory.STASH_HEAVY_THRESHOLD):
            (repo / "tracked.txt").write_text(f"stash {i}\n", encoding="utf-8")
            self.git(repo, "stash", "-q")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(record.stash_count, git_inventory.STASH_HEAVY_THRESHOLD)
        self.assertEqual(record.primary_class, "stash-heavy")
        self.assertIn("stash", record.classes)

    def test_ahead_of_upstream(self) -> None:
        _, clone = self.make_clone_pair("ahead")
        (clone / "local.txt").write_text("local\n", encoding="utf-8")
        self.git(clone, "add", "local.txt")
        self.git(clone, "commit", "-q", "-m", "local work")
        record = git_inventory.probe_repo(clone)
        self.assertEqual((record.ahead, record.behind), (1, 0))
        self.assertEqual(record.classes, frozenset({"ahead"}))
        self.assertEqual(record.primary_class, "ahead-clean")

    def test_behind_upstream(self) -> None:
        # Step the clone's HEAD back one commit: origin/main stays at the
        # clone-time tip, so the repo is strictly behind without any fetch.
        origin = self.make_repo("behind-origin")
        (origin / "second.txt").write_text("two\n", encoding="utf-8")
        self.git(origin, "add", "second.txt")
        self.git(origin, "commit", "-q", "-m", "second")
        clone = self.tmp / "behind-clone"
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        self.git(clone, "reset", "-q", "--hard", "HEAD~1")
        record = git_inventory.probe_repo(clone)
        self.assertEqual((record.ahead, record.behind), (0, 1))
        self.assertEqual(record.classes, frozenset({"behind"}))
        self.assertEqual(record.primary_class, "behind-clean")

    def test_diverged_clean(self) -> None:
        origin = self.make_repo("diverged-origin")
        (origin / "second.txt").write_text("two\n", encoding="utf-8")
        self.git(origin, "add", "second.txt")
        self.git(origin, "commit", "-q", "-m", "second")
        clone = self.tmp / "diverged-clone"
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        self.git(clone, "reset", "-q", "--hard", "HEAD~1")
        (clone / "local.txt").write_text("local\n", encoding="utf-8")
        self.git(clone, "add", "local.txt")
        self.git(clone, "commit", "-q", "-m", "local")
        record = git_inventory.probe_repo(clone)
        self.assertEqual((record.ahead, record.behind), (1, 1))
        self.assertEqual(record.classes, frozenset({"ahead", "behind", "diverged-clean"}))
        self.assertEqual(record.primary_class, "diverged-clean")

    def test_detached_head(self) -> None:
        repo = self.make_repo("detached")
        self.git(repo, "checkout", "-q", "--detach")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(record.branch, git_inventory.BRANCH_DETACHED)
        self.assertEqual(record.primary_class, "no-remote")
        self.assertIsNone(record.error)

    def test_no_remote(self) -> None:
        repo = self.make_repo("noremote")
        record = git_inventory.probe_repo(repo)
        self.assertIsNone(record.upstream)
        self.assertEqual(record.classes, frozenset({"no-remote"}))
        self.assertEqual(record.primary_class, "no-remote")

    def test_mid_op_merge_conflict(self) -> None:
        repo = self.make_repo("midop")
        self.git(repo, "checkout", "-q", "-b", "feature")
        (repo / "tracked.txt").write_text("feature\n", encoding="utf-8")
        self.git(repo, "commit", "-q", "-am", "feature change")
        self.git(repo, "checkout", "-q", "main")
        (repo / "tracked.txt").write_text("mainline\n", encoding="utf-8")
        self.git(repo, "commit", "-q", "-am", "main change")
        merge = self.git(repo, "merge", "feature", check=False)
        self.assertNotEqual(merge.returncode, 0, "merge should conflict")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(record.mid_op, "merge")
        self.assertIn("mid-op", record.classes)
        self.assertIn("dirty", record.classes)
        self.assertEqual(record.primary_class, "mid-op")

    def test_blocked_invalid_gitfile(self) -> None:
        repo = self.tmp / "broken"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /nonexistent/elsewhere\n", encoding="utf-8")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(record.classes, frozenset({"blocked"}))
        self.assertEqual(record.primary_class, "blocked")
        self.assertTrue(record.error)

    def test_blocked_on_timeout(self) -> None:
        repo = self.make_repo("timeout")
        with mock.patch.object(
            git_inventory,
            "_run_git",
            side_effect=subprocess.TimeoutExpired(cmd=["git", "status"], timeout=5.0),
        ):
            record = git_inventory.probe_repo(repo)
        self.assertEqual(record.primary_class, "blocked")
        self.assertIn("timed out", record.error or "")

    def test_bare_repo(self) -> None:
        bare = self.tmp / "bare.git"
        bare.mkdir()
        self.git(bare, "init", "-q", "--bare", "-b", "main")
        record = git_inventory.probe_repo(bare)
        self.assertTrue(record.bare)
        self.assertIsNone(record.error)
        self.assertNotEqual(record.primary_class, "blocked")
        self.assertEqual(record.branch, "main")
        self.assertEqual(
            (record.staged, record.unstaged, record.untracked), (0, 0, 0)
        )
        self.assertEqual(record.classes, frozenset({"no-remote"}))

    def test_linked_worktree_probes_cleanly(self) -> None:
        repo = self.make_repo("wt-main")
        worktree = self.tmp / "wt-linked"
        self.git(repo, "worktree", "add", "-q", str(worktree), "-b", "linked")
        record = git_inventory.probe_repo(worktree)
        self.assertIsNone(record.error)
        self.assertEqual(record.branch, "linked")
        self.assertIsNone(record.mid_op)
        self.assertEqual(record.primary_class, "no-remote")


class ReadOnlyContractTests(GitFixtureCase):
    def test_probe_never_fetches_and_sets_readonly_env(self) -> None:
        repo = self.make_repo("readonly")
        calls: list[list[str]] = []
        real_run_git = git_inventory._run_git

        def spy(path: str, args, timeout_s: float):
            calls.append(list(args))
            return real_run_git(path, args, timeout_s)

        with mock.patch.object(git_inventory, "_run_git", side_effect=spy):
            git_inventory.probe_repo(repo)
        self.assertTrue(calls)
        for args in calls:
            self.assertNotIn("fetch", args)
            self.assertNotIn("push", args)

        env = git_inventory._git_env()
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_git_env_strips_ambient_repo_overrides(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_DIR": "/somewhere/.git"}):
            env = git_inventory._git_env()
        self.assertNotIn("GIT_DIR", env)


class RecordSerializationTests(GitFixtureCase):
    def test_to_dict_is_json_safe_deterministic_and_sorted(self) -> None:
        repo = self.make_repo("serialize")
        (repo / "loose.txt").write_text("loose\n", encoding="utf-8")
        record = git_inventory.probe_repo(repo)
        payload = record.to_dict()
        self.assertEqual(
            list(payload),
            [
                "path", "classes", "primary_class", "branch", "upstream",
                "ahead", "behind", "stash_count", "staged", "unstaged",
                "untracked", "mid_op", "bare", "error",
            ],
        )
        self.assertEqual(payload["classes"], sorted(payload["classes"]))
        round_trip = json.loads(json.dumps(payload))
        self.assertEqual(round_trip, payload)


class DiscoveryTests(GitFixtureCase):
    @staticmethod
    def _fake_repo(path: Path, *, gitfile: bool = False) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if gitfile:
            (path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        else:
            (path / ".git").mkdir()

    def test_discovery_depth_prune_gitfile_and_nesting(self) -> None:
        root = self.tmp / "estate"
        self._fake_repo(root / "repoA")  # .git at depth 2
        self._fake_repo(root / "group" / "repoB")  # .git at depth 3
        self._fake_repo(root / "a" / "b" / "repoDeep")  # .git at depth 4
        self._fake_repo(root / "node_modules" / "repoPruned")  # pruned
        self._fake_repo(root / ".venv" / "repoVenv")  # pruned
        self._fake_repo(root / "worktreeish", gitfile=True)  # .git file
        # Nested inside repoA's work tree: must be skipped (submodule rule).
        self._fake_repo(root / "repoA" / "vendor" / "inner")

        found = git_inventory.discover_repos([root], depth=3)
        self.assertEqual(
            found,
            sorted(
                str(p)
                for p in (
                    root / "repoA",
                    root / "group" / "repoB",
                    root / "worktreeish",
                )
            ),
        )

        deeper = git_inventory.discover_repos([root], depth=4)
        self.assertIn(str(root / "a" / "b" / "repoDeep"), deeper)

    def test_root_itself_is_included_and_still_descended(self) -> None:
        root = self.tmp / "rootrepo"
        self._fake_repo(root)
        self._fake_repo(root / "child")
        found = git_inventory.discover_repos([root], depth=3)
        self.assertEqual(found, sorted([str(root), str(root / "child")]))

    def test_missing_root_and_bad_depth(self) -> None:
        self.assertEqual(
            git_inventory.discover_repos([self.tmp / "does-not-exist"]), []
        )
        with self.assertRaises(ValueError):
            git_inventory.discover_repos([self.tmp], depth=0)

    def test_scan_end_to_end(self) -> None:
        root = self.tmp / "scanroot"
        root.mkdir()
        repo = self.make_repo("scanroot/real")
        (repo / "loose.txt").write_text("loose\n", encoding="utf-8")
        broken = root / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")

        records = git_inventory.scan([root], depth=2)
        by_path = {r.path: r for r in records}
        self.assertEqual(set(by_path), {str(repo), str(broken)})
        self.assertEqual(by_path[str(repo)].primary_class, "dirty")
        self.assertEqual(by_path[str(broken)].primary_class, "blocked")
        self.assertEqual(
            git_inventory.primary_class_counts(records),
            {"blocked": 1, "dirty": 1},
        )


if __name__ == "__main__":
    unittest.main()
