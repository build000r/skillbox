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
import time
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
        # A single stash reports the same timestamp as newest and oldest.
        self.assertIsNotNone(record.stash_newest)
        self.assertEqual(record.stash_newest, record.stash_oldest)

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


class EnrichmentProbeTests(GitFixtureCase):
    """Stash ages + unpushed-branch signals: additive, class-vocabulary-free."""

    def stash_with_date(self, repo: Path, content: str, date: str) -> None:
        """One stash entry whose committer date is pinned via the env (stash
        timestamps come from the stash commit's committer date)."""
        (repo / "tracked.txt").write_text(content, encoding="utf-8")
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": date}):
            self.git(repo, "stash", "push", "-q", "-m", content.strip())

    def test_stash_ages_from_pinned_committer_dates(self) -> None:
        repo = self.make_repo("stash-aged")
        self.stash_with_date(repo, "oldest\n", "2026-01-01T00:00:00+00:00")
        self.stash_with_date(repo, "newest\n", "2026-02-05T12:30:00+00:00")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(record.stash_count, 2)
        self.assertEqual(record.stash_newest, "2026-02-05T12:30:00+00:00")
        self.assertEqual(record.stash_oldest, "2026-01-01T00:00:00+00:00")
        # Enrichment never touches the class vocabulary.
        self.assertLessEqual(record.classes, git_inventory.ALL_CLASSES)

    def test_stash_ages_truthful_when_backdated_out_of_order(self) -> None:
        # stash@{0} is the newest ENTRY, but a backdated committer date must
        # not be reported as "newest": max/min over timestamps, not list order.
        repo = self.make_repo("stash-backdated")
        self.stash_with_date(repo, "first\n", "2026-03-01T00:00:00+00:00")
        self.stash_with_date(repo, "second-backdated\n", "2026-01-01T00:00:00+00:00")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(record.stash_newest, "2026-03-01T00:00:00+00:00")
        self.assertEqual(record.stash_oldest, "2026-01-01T00:00:00+00:00")

    def test_no_stash_yields_null_ages(self) -> None:
        repo = self.make_repo("stashless")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(record.stash_count, 0)
        self.assertIsNone(record.stash_newest)
        self.assertIsNone(record.stash_oldest)

    def test_unpushed_branch_without_upstream(self) -> None:
        _, clone = self.make_clone_pair("parked")
        self.git(clone, "checkout", "-q", "-b", "parked-work")
        (clone / "parked.txt").write_text("parked\n", encoding="utf-8")
        self.git(clone, "add", "parked.txt")
        self.git(clone, "commit", "-q", "-m", "parked work")
        self.git(clone, "checkout", "-q", "main")
        record = git_inventory.probe_repo(clone)
        self.assertEqual(record.unpushed_branches, (("parked-work", 1),))
        self.assertIsNone(record.branch_scan_note)
        # HEAD itself is clean and current: the silent-loss class does not
        # bleed into classes/primary (schema additive, no vocabulary change).
        self.assertEqual(record.classes, frozenset({"clean-current"}))
        self.assertEqual(record.primary_class, "clean-current")

    def test_unpushed_branch_with_upstream_uses_track_not_rev_list(self) -> None:
        _, clone = self.make_clone_pair("tracked")
        self.git(clone, "checkout", "-q", "-b", "feat", "--track", "origin/main")
        for i in range(2):
            (clone / f"feat-{i}.txt").write_text(f"{i}\n", encoding="utf-8")
            self.git(clone, "add", f"feat-{i}.txt")
            self.git(clone, "commit", "-q", "-m", f"feat {i}")
        self.git(clone, "checkout", "-q", "main")

        calls: list[list[str]] = []
        real_run_git = git_inventory._run_git

        def spy(path: str, args, timeout_s: float):
            calls.append(list(args))
            return real_run_git(path, args, timeout_s)

        with mock.patch.object(git_inventory, "_run_git", side_effect=spy):
            record = git_inventory.probe_repo(clone)
        self.assertEqual(record.unpushed_branches, (("feat", 2),))
        # %(upstream:track) supplied the count: no rev-list subprocess spawned.
        self.assertFalse(
            any("rev-list" in args for args in calls),
            f"track path must not rev-list: {calls}",
        )

    def test_gone_upstream_falls_back_to_rev_list(self) -> None:
        origin = self.make_repo("gone-origin")
        self.git(origin, "checkout", "-q", "-b", "feat")
        (origin / "feat.txt").write_text("feat\n", encoding="utf-8")
        self.git(origin, "add", "feat.txt")
        self.git(origin, "commit", "-q", "-m", "feat on origin")
        self.git(origin, "checkout", "-q", "main")
        clone = self.tmp / "gone-clone"
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        self.git(clone, "checkout", "-q", "feat")  # auto-tracks origin/feat
        (clone / "local.txt").write_text("local\n", encoding="utf-8")
        self.git(clone, "add", "local.txt")
        self.git(clone, "commit", "-q", "-m", "local feat work")
        self.git(clone, "checkout", "-q", "main")
        # The upstream ref disappears: %(upstream:track) reports [gone], so
        # the batched rev-list must carry the branch instead of the track path.
        self.git(clone, "update-ref", "-d", "refs/remotes/origin/feat")
        record = git_inventory.probe_repo(clone)
        # origin/main still holds the base commit; the origin-side feat commit
        # and the local one are absent from every remaining remote ref.
        self.assertEqual(record.unpushed_branches, (("feat", 2),))

    def test_fully_pushed_branch_is_not_flagged(self) -> None:
        _, clone = self.make_clone_pair("pushed")
        # Same tip as origin/main: reachable from a remote, nothing unpushed.
        self.git(clone, "branch", "twin")
        record = git_inventory.probe_repo(clone)
        self.assertEqual(record.unpushed_branches, ())
        self.assertIsNone(record.branch_scan_note)

    def test_head_branch_is_never_listed(self) -> None:
        _, clone = self.make_clone_pair("headwork")
        (clone / "local.txt").write_text("local\n", encoding="utf-8")
        self.git(clone, "add", "local.txt")
        self.git(clone, "commit", "-q", "-m", "local work")
        record = git_inventory.probe_repo(clone)
        # HEAD's unpushed work is the existing `ahead` signal, not a branch row.
        self.assertEqual((record.ahead, record.behind), (1, 0))
        self.assertEqual(record.unpushed_branches, ())

    def test_multiple_unpushed_branches_sorted_by_name(self) -> None:
        _, clone = self.make_clone_pair("multi")
        for name in ("zeta", "alpha"):
            self.git(clone, "checkout", "-q", "-b", name)
            (clone / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
            self.git(clone, "add", f"{name}.txt")
            self.git(clone, "commit", "-q", "-m", name)
            self.git(clone, "checkout", "-q", "main")
        record = git_inventory.probe_repo(clone)
        self.assertEqual(record.unpushed_branches, (("alpha", 1), ("zeta", 1)))

    def test_branch_scan_skipped_past_limit(self) -> None:
        repo = self.make_repo("many-branches")
        # main + BRANCH_SCAN_LIMIT extras = one past the limit. Plain refs at
        # HEAD (no commits) keep the fixture fast.
        for i in range(git_inventory.BRANCH_SCAN_LIMIT):
            self.git(repo, "branch", f"b-{i:03d}")
        record = git_inventory.probe_repo(repo)
        self.assertEqual(record.unpushed_branches, ())
        self.assertEqual(
            record.branch_scan_note,
            f"branch scan skipped: {git_inventory.BRANCH_SCAN_LIMIT + 1} local branches",
        )

    def test_bare_repo_skips_branch_scan(self) -> None:
        bare = self.tmp / "bare-branches.git"
        bare.mkdir()
        self.git(bare, "init", "-q", "--bare", "-b", "main")
        record = git_inventory.probe_repo(bare)
        self.assertTrue(record.bare)
        self.assertEqual(record.unpushed_branches, ())
        self.assertIsNone(record.branch_scan_note)

    def test_track_ahead_parser(self) -> None:
        cases = {
            "": 0,
            "[ahead 2]": 2,
            "[behind 3]": 0,
            "[ahead 4, behind 1]": 4,
            "[gone]": 0,
            "[garbage nonsense]": 0,
        }
        for track, expected in cases.items():
            self.assertEqual(
                git_inventory._parse_track_ahead(track), expected, repr(track)
            )


class StoreIdentityTests(GitFixtureCase):
    """``git_dir`` / ``common_dir``: the shared-ref-store identity that lets
    git_estate attribute stashes to one row per physical store."""

    def test_plain_repo_owns_its_own_store(self) -> None:
        repo = self.make_repo("solo")
        record = git_inventory.probe_repo(repo)
        expected = str((repo / ".git").resolve())
        self.assertEqual(record.git_dir, expected)
        self.assertEqual(record.common_dir, expected)
        # A repo that owns its store is its own primary.
        self.assertEqual(record.git_dir, record.common_dir)

    def test_linked_worktree_names_the_main_store(self) -> None:
        repo = self.make_repo("wt-store-main")
        worktree = self.tmp / "wt-store-linked"
        self.git(repo, "worktree", "add", "-q", str(worktree), "-b", "linked")
        main = git_inventory.probe_repo(repo)
        linked = git_inventory.probe_repo(worktree)
        # Same physical store...
        self.assertEqual(linked.common_dir, main.common_dir)
        # ...but the worktree has its own per-worktree git dir, so only the
        # main checkout satisfies the primary rule (git_dir == common_dir).
        self.assertNotEqual(linked.git_dir, linked.common_dir)
        self.assertEqual(main.git_dir, main.common_dir)
        self.assertIn("worktrees", str(linked.git_dir))

    def test_symlink_alias_resolves_to_the_same_store_key(self) -> None:
        repo = self.make_repo("alias-target")
        alias = self.tmp / "alias-view"
        alias.symlink_to(repo)
        record = git_inventory.probe_repo(repo)
        aliased = git_inventory.probe_repo(alias)
        # The alias reports its own path but must NOT look like a second
        # store: symlink resolution is what collapses the two into one key.
        self.assertEqual(aliased.path, str(alias))
        self.assertEqual(aliased.common_dir, record.common_dir)
        self.assertEqual(aliased.git_dir, record.git_dir)

    def test_bare_repo_reports_its_store(self) -> None:
        bare = self.tmp / "bare-store.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        record = git_inventory.probe_repo(bare)
        self.assertTrue(record.bare)
        self.assertEqual(record.common_dir, str(bare.resolve()))
        self.assertEqual(record.git_dir, record.common_dir)

    def test_blocked_probe_has_no_store_key(self) -> None:
        # An unknown key must never group with another unknown key.
        not_a_repo = self.tmp / "not-a-repo"
        not_a_repo.mkdir()
        record = git_inventory.probe_repo(not_a_repo)
        self.assertEqual(record.primary_class, "blocked")
        self.assertIsNone(record.git_dir)
        self.assertIsNone(record.common_dir)

    def test_store_identity_costs_no_extra_subprocess(self) -> None:
        repo = self.make_repo("store-batch")
        calls: list[list[str]] = []
        real_run_git = git_inventory._run_git

        def spy(path: str, args, timeout_s: float):
            calls.append(list(args))
            return real_run_git(path, args, timeout_s)

        with mock.patch.object(git_inventory, "_run_git", side_effect=spy):
            record = git_inventory.probe_repo(repo)
        self.assertIsNotNone(record.common_dir)
        rev_parse = [args for args in calls if args and args[0] == "rev-parse"]
        # --git-common-dir rides the ONE identity batch: the consolidated path
        # spawns exactly one rev-parse, exactly as it did before the field.
        self.assertEqual(1, len(rev_parse), f"rev-parse calls: {rev_parse}")
        self.assertIn("--git-common-dir", rev_parse[0])
        self.assertIn("--absolute-git-dir", rev_parse[0])

    def test_old_git_without_the_query_degrades_to_no_key(self) -> None:
        # `git rev-parse` echoes an option it does not know back verbatim and
        # still exits 0. That must read as "unknown store", never as a key
        # every ancient-git repo would share.
        self.assertIsNone(git_inventory._resolve_git_path("/repo", "--git-common-dir"))
        self.assertIsNone(git_inventory._resolve_git_path("/repo", ""))
        self.assertIsNone(git_inventory._resolve_git_path("/repo", "   "))

    def test_relative_answer_is_joined_onto_the_repo(self) -> None:
        # --git-common-dir answers ".git" from a main worktree.
        repo = self.make_repo("relative-answer")
        self.assertEqual(
            str((repo / ".git").resolve()),
            git_inventory._resolve_git_path(str(repo), ".git"),
        )


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
        # A parked branch so the unpushed_branches projection is non-trivial.
        self.git(repo, "branch", "parked")
        record = git_inventory.probe_repo(repo)
        payload = record.to_dict()
        self.assertEqual(
            list(payload),
            [
                "path", "classes", "primary_class", "branch", "upstream",
                "ahead", "behind", "stash_count", "stash_newest",
                "stash_oldest", "staged", "unstaged", "untracked", "mid_op",
                "unpushed_branches", "branch_scan_note", "bare", "git_dir",
                "common_dir", "error",
            ],
        )
        self.assertEqual(payload["classes"], sorted(payload["classes"]))
        # (name, ahead) pairs project as JSON-friendly {name, ahead} objects.
        self.assertEqual(payload["unpushed_branches"], [{"name": "parked", "ahead": 1}])
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

    def test_scan_accepts_workers_and_stays_correct(self) -> None:
        root = self.tmp / "workersroot"
        root.mkdir()
        repo = self.make_repo("workersroot/solo")
        records = git_inventory.scan([root], depth=2, workers=1)
        self.assertEqual([r.path for r in records], [str(repo)])
        self.assertEqual(records[0].primary_class, "no-remote")
        with self.assertRaises(ValueError):
            git_inventory.scan([root], depth=2, workers=0)

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


class ConcurrentScanTests(GitFixtureCase):
    """Parallel-probe contract: determinism, deadlines, ScanResult surface."""

    def _build_estate(self, count: int) -> tuple[Path, list[Path]]:
        root = self.tmp / "estate"
        root.mkdir()
        repos = [self.make_repo(f"estate/repo-{i:02d}") for i in range(count)]
        return root, repos

    def test_deterministic_ordering_under_concurrency(self) -> None:
        root, repos = self._build_estate(10)
        expected = sorted(str(r) for r in repos)

        # Skew completion order: make the lexicographically-first repos the
        # slowest so completion order inverts path order.
        real_run_git = git_inventory._run_git

        def skewed(path: str, args, timeout_s: float):
            if path.endswith(("repo-00", "repo-01", "repo-02")):
                time.sleep(0.05)
            return real_run_git(path, args, timeout_s)

        with mock.patch.object(git_inventory, "_run_git", side_effect=skewed):
            records = git_inventory.scan([root], depth=2, workers=8)
        self.assertEqual([r.path for r in records], expected)
        self.assertTrue(all(r.error is None for r in records))

        # Same order again on a second, differently-parallel run.
        rerun = git_inventory.scan([root], depth=2, workers=3)
        self.assertEqual([r.path for r in rerun], expected)

    def test_wedged_repo_becomes_blocked_not_hang(self) -> None:
        root, repos = self._build_estate(4)
        wedged = str(repos[1])
        real_run_git = git_inventory._run_git

        def wedge(path: str, args, timeout_s: float):
            if path == wedged:
                time.sleep(1.0)  # every call outlasts the whole repo deadline
            return real_run_git(path, args, timeout_s)

        # The deadline is generous enough for the healthy repos (a fixture
        # probe is ~0.1s) but is exhausted by the wedged repo's first call.
        start = time.monotonic()
        with mock.patch.object(git_inventory, "_run_git", side_effect=wedge):
            records = git_inventory.scan(
                [root], depth=2, workers=4, deadline_s=0.75
            )
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 5.0, "wedged repo must not stall the scan")

        by_path = {r.path: r for r in records}
        self.assertEqual([r.path for r in records], sorted(by_path))
        self.assertEqual(by_path[wedged].primary_class, "blocked")
        self.assertIn("deadline", by_path[wedged].error or "")
        for path, record in by_path.items():
            if path != wedged:
                self.assertIsNone(record.error, f"{path} should probe cleanly")

    def test_deadline_exhausted_between_calls(self) -> None:
        repo = self.make_repo("deadline-between")
        real_run_git = git_inventory._run_git

        def slow(path: str, args, timeout_s: float):
            time.sleep(0.06)  # deadline is spent before the next call starts
            return real_run_git(path, args, timeout_s)

        with mock.patch.object(git_inventory, "_run_git", side_effect=slow):
            record = git_inventory.probe_repo(repo, deadline_s=0.05)
        self.assertEqual(record.primary_class, "blocked")
        self.assertIn("deadline", record.error or "")

    def test_probe_repo_rejects_nonpositive_deadline(self) -> None:
        repo = self.make_repo("bad-deadline")
        with self.assertRaises(ValueError):
            git_inventory.probe_repo(repo, deadline_s=0)
        # None disables the overall deadline entirely.
        record = git_inventory.probe_repo(repo, deadline_s=None)
        self.assertIsNone(record.error)

    def test_scan_estate_result_surface(self) -> None:
        root, repos = self._build_estate(3)
        result = git_inventory.scan_estate([root], depth=2, workers=4)

        self.assertIsInstance(result, git_inventory.ScanResult)
        self.assertGreaterEqual(result.elapsed_seconds, 0.0)
        self.assertEqual(result.repo_count, len(result.records))
        self.assertEqual(result.repo_count, len(repos))
        self.assertEqual(result.roots, [str(root)])
        self.assertEqual(result.depth, 2)
        self.assertEqual(result.workers, 4)
        self.assertEqual(
            [r.path for r in result.records], sorted(str(r) for r in repos)
        )

        # scan() is the same scan minus the metadata wrapper.
        self.assertEqual(
            [r.path for r in git_inventory.scan([root], depth=2, workers=4)],
            [r.path for r in result.records],
        )

        payload = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(payload["repo_count"], result.repo_count)
        self.assertEqual(payload["depth"], 2)
        self.assertEqual(len(payload["records"]), result.repo_count)
        self.assertIsInstance(payload["elapsed_seconds"], float)

    def test_scan_estate_empty_estate(self) -> None:
        root = self.tmp / "empty-estate"
        root.mkdir()
        result = git_inventory.scan_estate([root], depth=2)
        self.assertEqual(result.records, [])
        self.assertEqual(result.repo_count, 0)
        self.assertGreaterEqual(result.elapsed_seconds, 0.0)

    def test_default_workers_is_sane(self) -> None:
        workers = git_inventory.DEFAULT_WORKERS
        self.assertGreaterEqual(workers, 8)
        self.assertLessEqual(workers, 32)

    def test_git_env_is_fresh_per_call(self) -> None:
        """Workers must never share a mutable env dict across probes."""
        first = git_inventory._git_env()
        second = git_inventory._git_env()
        self.assertIsNot(first, second)
        self.assertEqual(first, second)
        first["GIT_OPTIONAL_LOCKS"] = "tampered"
        self.assertEqual(git_inventory._git_env()["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(git_inventory.READ_ONLY_GIT_ENV["GIT_OPTIONAL_LOCKS"], "0")


if __name__ == "__main__":
    unittest.main()
