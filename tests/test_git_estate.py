"""Tests for runtime_manager.git_estate -- the ``sbp git`` presentation layer.

Hermetic throughout: fixture repos are real ``git init`` repos inside a
TemporaryDirectory with pinned git config; the registry ignore fixture is a
temp config root (pointed at via ``SKILLBOX_CONFIG_ROOT``) carrying a
JSON-bodied ``registry/repos.yaml`` (JSON is valid YAML) and a stand-in
``scripts/registry_doctor.py`` exposing the same three functions git_estate
loads from the real skillbox-config helper. Wrapper-level alias tests
subprocess the real ``scripts/sbp`` with ``--root`` pointed at the temp
estate so the suite never scans the operator's ~/repos.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import git_estate  # noqa: E402
from runtime_manager.git_inventory import GitRepoRecord  # noqa: E402

SBP = ROOT / "scripts" / "sbp"

# Faithful stand-in for skillbox-config/scripts/registry_doctor.py: same three
# entry points, same path/pattern rule semantics, JSON body instead of PyYAML.
_REGISTRY_DOCTOR_STANDIN = textwrap.dedent(
    '''
    import fnmatch
    import json
    import os
    from pathlib import Path


    def normalize_path(value):
        expanded = os.path.expandvars(os.path.expanduser(str(value)))
        return os.path.abspath(os.path.normpath(expanded))


    def load_registry(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))


    def normalize_registry(payload, root_overrides):
        repos = []
        for item in payload.get("repos") or []:
            item = dict(item)
            item["path"] = normalize_path(item["path"])
            repos.append(item)
        ignore = []
        for item in payload.get("ignore") or []:
            item = dict(item)
            if "path" in item:
                item["path"] = normalize_path(item["path"])
            if "pattern" in item:
                item["pattern"] = normalize_path(item["pattern"])
            ignore.append(item)
        return {
            "roots": [],
            "max_depth": None,
            "prune_dir_names": set(),
            "repos": repos,
            "ignore": ignore,
        }


    def _is_same_or_child(path, parent):
        try:
            Path(path).relative_to(parent)
            return True
        except ValueError:
            return False


    def matching_ignore(path, ignore_rules):
        for rule in ignore_rules:
            if rule.get("path") and _is_same_or_child(path, rule["path"]):
                return rule
            if rule.get("pattern") and fnmatch.fnmatch(path, rule["pattern"]):
                return rule
        return None
    '''
)


def _record(path: str = "/repo", **overrides) -> GitRepoRecord:
    defaults = dict(
        path=path,
        classes=frozenset({"clean-current"}),
        primary_class="clean-current",
        branch="main",
        upstream="origin/main",
    )
    defaults.update(overrides)
    return GitRepoRecord(**defaults)


class RiskSortTests(unittest.TestCase):
    def _band_fixture(self) -> list[GitRepoRecord]:
        return [
            _record("/r/blocked", classes=frozenset({"blocked"}), primary_class="blocked", upstream=None, error="boom"),
            _record("/r/midop", classes=frozenset({"mid-op", "dirty"}), primary_class="mid-op", mid_op="merge", staged=1),
            _record("/r/diverged", classes=frozenset({"ahead", "behind", "diverged-clean"}), primary_class="diverged-clean", ahead=1, behind=1),
            _record("/r/behind", classes=frozenset({"behind"}), primary_class="behind-clean", behind=2),
            _record("/r/dirty-behind", classes=frozenset({"dirty", "behind"}), primary_class="dirty", behind=1, unstaged=1),
            _record("/r/dirty", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1),
            _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=3),
            _record("/r/noremote", classes=frozenset({"no-remote"}), primary_class="no-remote", upstream=None),
            _record("/r/stash", classes=frozenset({"stash"}), primary_class="clean-current", stash_count=2),
            _record("/r/clean", classes=frozenset({"clean-current"})),
        ]

    def test_risk_sort_band_order(self) -> None:
        records = self._band_fixture()
        shuffled = list(reversed(records))
        ordered = git_estate.risk_sorted(shuffled)
        bands = [git_estate.RISK_BAND_NAMES[git_estate.risk_band(r)] for r in ordered]
        self.assertEqual(
            bands,
            [
                "blocked",
                "mid-op",
                "diverged",
                "behind-clean",
                "dirty-behind",
                "dirty",
                "ahead",
                "no-remote",
                "stash-only",
                "clean",
            ],
        )
        self.assertEqual(
            [r.path for r in ordered],
            [
                "/r/blocked",
                "/r/midop",
                "/r/diverged",
                "/r/behind",
                "/r/dirty-behind",
                "/r/dirty",
                "/r/ahead",
                "/r/noremote",
                "/r/stash",
                "/r/clean",
            ],
        )

    def test_within_band_sorts_by_path(self) -> None:
        b = _record("/r/b", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        a = _record("/r/a", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        self.assertEqual([r.path for r in git_estate.risk_sorted([b, a])], ["/r/a", "/r/b"])

    def test_unregistered_outranks_registered_within_a_band(self) -> None:
        a = _record("/r/a", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        b = _record("/r/b", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        registration = {"/r/a": "registered", "/r/b": "unregistered"}
        self.assertEqual(
            [r.path for r in git_estate.risk_sorted([a, b], registration)],
            ["/r/b", "/r/a"],
        )

    def test_registration_tiebreak_never_crosses_bands(self) -> None:
        # An unregistered clean repo must NOT outrank a registered dirty one.
        clean = _record("/r/clean")
        dirty = _record("/r/dirty", classes=frozenset({"dirty"}), primary_class="dirty", unstaged=1)
        registration = {"/r/clean": "unregistered", "/r/dirty": "registered"}
        self.assertEqual(
            [r.path for r in git_estate.risk_sorted([clean, dirty], registration)],
            ["/r/dirty", "/r/clean"],
        )

    def test_unknown_registration_ties_with_registered_by_path(self) -> None:
        a = _record("/r/a", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        b = _record("/r/b", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        registration = {"/r/a": "unknown", "/r/b": "registered"}
        self.assertEqual(
            [r.path for r in git_estate.risk_sorted([b, a], registration)],
            ["/r/a", "/r/b"],
        )


class FixCommandTests(unittest.TestCase):
    def test_ahead_gets_push(self) -> None:
        record = _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=2)
        self.assertEqual(git_estate.fix_commands(record), ["git -C /r/ahead push"])

    def test_behind_gets_ff_only_pull(self) -> None:
        record = _record("/r/behind", classes=frozenset({"behind"}), primary_class="behind-clean", behind=2)
        self.assertEqual(
            git_estate.fix_commands(record),
            ["git -C /r/behind pull --ff-only  # or /reconcile"],
        )

    def test_diverged_gets_reconcile_handoff_never_push_pull(self) -> None:
        record = _record(
            "/r/div",
            classes=frozenset({"ahead", "behind", "diverged-clean"}),
            primary_class="diverged-clean",
            ahead=1,
            behind=1,
        )
        fixes = git_estate.fix_commands(record)
        self.assertEqual(fixes, ["sbp doctor / reconcile skill — do not hand-merge"])
        self.assertFalse(any("push" in fix or "pull" in fix for fix in fixes))

    def test_mid_op_names_the_kind(self) -> None:
        record = _record(
            "/r/mid", classes=frozenset({"mid-op"}), primary_class="mid-op", mid_op="rebase"
        )
        self.assertEqual(
            git_estate.fix_commands(record),
            ["git -C /r/mid status  # finish or abort the rebase"],
        )

    def test_dirty_stash_and_no_remote(self) -> None:
        record = _record(
            "/r/mix",
            classes=frozenset({"dirty", "stash", "no-remote"}),
            primary_class="dirty",
            upstream=None,
            unstaged=1,
            stash_count=1,
        )
        self.assertEqual(
            git_estate.fix_commands(record),
            [
                "git -C /r/mix add -p && git -C /r/mix commit",
                "git -C /r/mix stash list  # git-stash-janitor pass",
                "add a remote or register intent",
            ],
        )

    def test_blocked_carries_error_and_nothing_else(self) -> None:
        record = _record(
            "/r/blk", classes=frozenset({"blocked"}), primary_class="blocked", upstream=None, error="probe died"
        )
        self.assertEqual(git_estate.fix_commands(record), ["inspect: probe died"])

    def test_dirty_and_ahead_carry_both_fixes(self) -> None:
        record = _record(
            "/r/da", classes=frozenset({"dirty", "ahead"}), primary_class="dirty", ahead=1, staged=1
        )
        fixes = git_estate.fix_commands(record)
        self.assertIn("git -C /r/da push", fixes)
        self.assertIn("git -C /r/da add -p && git -C /r/da commit", fixes)

    def test_clean_has_no_fix(self) -> None:
        self.assertEqual(git_estate.fix_commands(_record("/r/clean")), [])

    def test_unpushed_branches_get_branch_listing_fix(self) -> None:
        record = _record("/r/up", unpushed_branches=(("feat", 2), ("wip", 1)))
        self.assertEqual(
            git_estate.fix_commands(record),
            ["git -C /r/up branch -vv  # 2 unpushed branches: feat(+2), wip(+1)"],
        )

    def test_single_unpushed_branch_fix_is_singular(self) -> None:
        record = _record("/r/up", unpushed_branches=(("parked", 3),))
        self.assertEqual(
            git_estate.fix_commands(record),
            ["git -C /r/up branch -vv  # 1 unpushed branch: parked(+3)"],
        )

    def test_unregistered_gets_registry_handoff_after_work_fixes(self) -> None:
        record = _record("/r/dirty", classes=frozenset({"dirty"}), primary_class="dirty", unstaged=1)
        fixes = git_estate.fix_commands(record, "unregistered", "/cfg/registry/repos.yaml")
        self.assertEqual(
            fixes,
            [
                "git -C /r/dirty add -p && git -C /r/dirty commit",
                "register in /cfg/registry/repos.yaml or add an ignore rule there",
            ],
        )

    def test_registered_row_gets_no_registry_handoff(self) -> None:
        record = _record("/r/dirty", classes=frozenset({"dirty"}), primary_class="dirty", unstaged=1)
        fixes = git_estate.fix_commands(record, "registered", "/cfg/registry/repos.yaml")
        self.assertFalse(any("register in" in fix for fix in fixes))

    def test_blocked_stays_inspect_only_even_when_unregistered(self) -> None:
        record = _record(
            "/r/blk", classes=frozenset({"blocked"}), primary_class="blocked", upstream=None, error="probe died"
        )
        self.assertEqual(
            git_estate.fix_commands(record, "unregistered", "/cfg/registry/repos.yaml"),
            ["inspect: probe died"],
        )


class OnlyFilterTests(unittest.TestCase):
    def test_unknown_token_raises_with_vocabulary(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            git_estate.parse_only(["bogus"])
        message = str(ctx.exception)
        for token in git_estate.FILTER_CLASSES + git_estate.REGISTRATION_FILTER_CLASSES:
            self.assertIn(token, message)

    def test_registration_tokens_parse_separately_from_classes(self) -> None:
        active, registration = git_estate.parse_only(["unregistered,stale-registered"])
        self.assertEqual(active, ())
        self.assertEqual(registration, ("unregistered", "stale-registered"))

    def test_classes_and_registration_tokens_split(self) -> None:
        active, registration = git_estate.parse_only(["dirty,unregistered"])
        self.assertEqual(active, ("dirty",))
        self.assertEqual(registration, ("unregistered",))

    def test_comma_and_repeat_forms_merge(self) -> None:
        active, reserved = git_estate.parse_only(["dirty,stash", "dirty", "mid-op"])
        self.assertEqual(active, ("dirty", "stash", "mid-op"))
        self.assertEqual(reserved, ())

    def test_behind_matches_behind_clean_and_diverged_clean(self) -> None:
        behind = _record("/r/behind", classes=frozenset({"behind"}), primary_class="behind-clean", behind=1)
        diverged = _record(
            "/r/div",
            classes=frozenset({"ahead", "behind", "diverged-clean"}),
            primary_class="diverged-clean",
            ahead=1,
            behind=1,
        )
        ahead = _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=1)
        kept = git_estate._apply_only([behind, diverged, ahead], ("behind",))
        self.assertEqual([r.path for r in kept], ["/r/behind", "/r/div"])

    def test_ahead_matches_ahead_clean_and_diverged_clean(self) -> None:
        behind = _record("/r/behind", classes=frozenset({"behind"}), primary_class="behind-clean", behind=1)
        diverged = _record(
            "/r/div",
            classes=frozenset({"ahead", "behind", "diverged-clean"}),
            primary_class="diverged-clean",
            ahead=1,
            behind=1,
        )
        ahead = _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=1)
        kept = git_estate._apply_only([behind, diverged, ahead], ("ahead",))
        self.assertEqual([r.path for r in kept], ["/r/div", "/r/ahead"])

    def test_stash_means_count_at_least_one(self) -> None:
        stashed = _record("/r/stash", classes=frozenset({"stash"}), primary_class="clean-current", stash_count=1)
        clean = _record("/r/clean")
        kept = git_estate._apply_only([stashed, clean], ("stash",))
        self.assertEqual([r.path for r in kept], ["/r/stash"])

    def test_multi_class_filter_is_a_union(self) -> None:
        dirty = _record("/r/dirty", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        midop = _record("/r/mid", classes=frozenset({"mid-op"}), primary_class="mid-op", mid_op="merge")
        clean = _record("/r/clean")
        kept = git_estate._apply_only([dirty, midop, clean], ("dirty", "mid-op"))
        self.assertEqual({r.path for r in kept}, {"/r/dirty", "/r/mid"})

    def test_unregistered_filters_rows_by_registration_state(self) -> None:
        reg = _record("/r/reg")
        unreg = _record("/r/unreg")
        registration = {"/r/reg": "registered", "/r/unreg": "unregistered"}
        kept = git_estate._apply_only([reg, unreg], ("unregistered",), registration)
        self.assertEqual([r.path for r in kept], ["/r/unreg"])

    def test_registration_and_class_tokens_compose_as_a_union(self) -> None:
        dirty = _record("/r/dirty", classes=frozenset({"dirty"}), primary_class="dirty", unstaged=1)
        unreg = _record("/r/unreg")
        clean = _record("/r/clean")
        registration = {
            "/r/dirty": "registered",
            "/r/unreg": "unregistered",
            "/r/clean": "registered",
        }
        kept = git_estate._apply_only(
            [dirty, unreg, clean], ("dirty", "unregistered"), registration
        )
        self.assertEqual({r.path for r in kept}, {"/r/dirty", "/r/unreg"})

    def test_stale_registered_matches_no_scanned_row(self) -> None:
        reg = _record("/r/reg")
        unreg = _record("/r/unreg")
        registration = {"/r/reg": "registered", "/r/unreg": "unregistered"}
        kept = git_estate._apply_only([reg, unreg], ("stale-registered",), registration)
        self.assertEqual(kept, [])

    def test_unknown_state_never_matches_registration_tokens(self) -> None:
        # Degrade: no registration map means every row is "unknown".
        row = _record("/r/any")
        self.assertEqual(git_estate._apply_only([row], ("unregistered",)), [])


class GitEstateFixtureCase(unittest.TestCase):
    """Temp estate + temp config root, hermetic git configuration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="git-estate-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.estate = self.tmp / "estate"
        self.estate.mkdir()
        self.origins = self.tmp / "origins"
        self.origins.mkdir()
        self.config_root = self.tmp / "config"

        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n"
            "\temail = fixture@example.invalid\n"
            "\tname = Git Estate Fixture\n"
            "[init]\n"
            "\tdefaultBranch = main\n"
            "[commit]\n"
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
                "SKILLBOX_CONFIG_ROOT": str(self.config_root),
                # Wrapper runs write-through the scan cache; keep it out of the
                # real state root so tests never seed the live home view.
                "SKILLBOX_STATE_ROOT": str(self.tmp / "state"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def make_repo(self, name: str, *, parent: Path | None = None) -> Path:
        repo = (parent or self.estate) / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "main")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "tracked.txt")
        self.git(repo, "commit", "-q", "-m", "base")
        return repo

    def make_ahead_clone(self, name: str) -> Path:
        origin = self.make_repo(f"{name}-origin", parent=self.origins)
        clone = self.estate / name
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        (clone / "local.txt").write_text("local\n", encoding="utf-8")
        self.git(clone, "add", "local.txt")
        self.git(clone, "commit", "-q", "-m", "local work")
        return clone

    def make_clean_clone(self, name: str) -> Path:
        origin = self.make_repo(f"{name}-origin", parent=self.origins)
        clone = self.estate / name
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        return clone

    def write_config_fixture(
        self,
        ignore: list[dict] | None = None,
        repos: list[dict] | None = None,
    ) -> None:
        scripts = self.config_root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "registry_doctor.py").write_text(_REGISTRY_DOCTOR_STANDIN, encoding="utf-8")
        registry = self.config_root / "registry"
        registry.mkdir(parents=True, exist_ok=True)
        # JSON is valid YAML: keeps the fixture hermetic (no PyYAML needed).
        (registry / "repos.yaml").write_text(
            json.dumps({"repos": repos or [], "ignore": ignore or []}), encoding="utf-8"
        )

    @property
    def registry_yaml(self) -> str:
        return str(self.config_root / "registry" / "repos.yaml")


class RelativeAgeTests(unittest.TestCase):
    NOW = "2026-08-09T12:00:00+00:00"

    def test_day_and_hour_floors(self) -> None:
        self.assertEqual(
            git_estate._relative_age("2026-08-06T11:00:00+00:00", self.NOW), "3d"
        )
        self.assertEqual(
            git_estate._relative_age("2026-08-09T07:00:00+00:00", self.NOW), "5h"
        )
        self.assertEqual(
            git_estate._relative_age("2026-08-09T11:30:00+00:00", self.NOW), "<1h"
        )

    def test_future_timestamp_clamps_instead_of_negative(self) -> None:
        self.assertEqual(
            git_estate._relative_age("2026-08-10T12:00:00+00:00", self.NOW), "<1h"
        )

    def test_degrades_to_none_on_any_parse_problem(self) -> None:
        self.assertIsNone(git_estate._relative_age(None, self.NOW))
        self.assertIsNone(git_estate._relative_age("2026-08-06T11:00:00+00:00", None))
        self.assertIsNone(git_estate._relative_age("garbage", self.NOW))
        # naive/aware mix raises TypeError inside; must degrade, not crash.
        self.assertIsNone(
            git_estate._relative_age("2026-08-06T11:00:00", self.NOW)
        )


class BandPlacementFixtureTests(GitEstateFixtureCase):
    """Real-git dirty+behind / dirty+diverged fixtures asserting band
    placement (closes the tests-bead audit gap: these combinations were
    previously covered only by synthetic records in RiskSortTests)."""

    def make_behind_clone(self, name: str) -> Path:
        """Clone stepped one commit behind its origin, no fetch involved."""
        origin = self.make_repo(f"{name}-origin", parent=self.origins)
        (origin / "second.txt").write_text("two\n", encoding="utf-8")
        self.git(origin, "add", "second.txt")
        self.git(origin, "commit", "-q", "-m", "second")
        clone = self.estate / name
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        self.git(clone, "reset", "-q", "--hard", "HEAD~1")
        return clone

    def test_dirty_behind_band_from_real_repo(self) -> None:
        clone = self.make_behind_clone("a-dirty-behind")
        (clone / "tracked.txt").write_text("dirt\n", encoding="utf-8")
        self.write_config_fixture()
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        row = next(r for r in report["repos"] if r["path"] == str(clone))
        self.assertEqual(row["risk_band"], "dirty-behind")
        self.assertEqual((row["ahead"], row["behind"]), (0, 1))
        self.assertIn("dirty", row["classes"])
        self.assertIn("behind", row["classes"])

    def test_dirty_diverged_band_from_real_repo(self) -> None:
        clone = self.make_behind_clone("a-dirty-diverged")
        (clone / "local.txt").write_text("local\n", encoding="utf-8")
        self.git(clone, "add", "local.txt")
        self.git(clone, "commit", "-q", "-m", "local work")
        (clone / "tracked.txt").write_text("dirt\n", encoding="utf-8")
        self.write_config_fixture()
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        row = next(r for r in report["repos"] if r["path"] == str(clone))
        # ahead+behind wins the band whatever the tree state; the dirty tree
        # keeps diverged-clean OUT of the class set.
        self.assertEqual(row["risk_band"], "diverged")
        self.assertEqual((row["ahead"], row["behind"]), (1, 1))
        self.assertIn("dirty", row["classes"])
        self.assertNotIn("diverged-clean", row["classes"])
        # Diverged rows keep the reconcile handoff, never push/pull.
        self.assertIn("sbp doctor / reconcile skill — do not hand-merge", row["fix"])


class BuildReportTests(GitEstateFixtureCase):
    def test_envelope_shape_order_ignore_rules_and_fixes(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        ahead = self.make_ahead_clone("b-ahead")
        clean = self.make_clean_clone("c-clean")
        ignored = self.make_repo("z-ignored")
        (ignored / "junk.txt").write_text("junk\n", encoding="utf-8")
        gone = self.estate / "gone-checkout"  # registered, never created on disk
        self.write_config_fixture(
            ignore=[{"path": str(ignored), "reason": "fixture"}],
            repos=[
                {"id": "b-ahead", "path": str(ahead)},
                {"id": "c-clean", "path": str(clean)},
                {"id": "gone", "path": str(gone)},
            ],
        )

        report = git_estate.build_report(
            roots=[str(self.estate)], depth=2, cwd=str(dirty)
        )

        self.assertEqual(report["schema"], "sbp-git/v1")
        for key in (
            "generated_at",
            "roots",
            "ignored_count",
            "repos",
            "summary",
            "elapsed_seconds",
            "repo_count",
        ):
            self.assertIn(key, report)
        self.assertIsInstance(report["elapsed_seconds"], float)
        self.assertEqual(report["roots"], [str(self.estate)])
        self.assertEqual(report["ignored_count"], 1)
        self.assertTrue(report["registry_applied"])
        self.assertEqual(report["notes"], [])

        paths = [row["path"] for row in report["repos"]]
        self.assertEqual(paths, [str(dirty), str(ahead), str(clean)])
        self.assertNotIn(str(ignored), paths)
        self.assertEqual(report["repo_count"], 3)
        self.assertEqual(
            report["summary"],
            {"ahead-clean": 1, "clean-current": 1, "dirty": 1},
        )

        rows = {row["path"]: row for row in report["repos"]}
        self.assertEqual(rows[str(dirty)]["risk_band"], "dirty")
        # Registration joins from the same registry parse as the ignore rules.
        self.assertEqual(rows[str(dirty)]["registration"], "unregistered")
        self.assertEqual(rows[str(ahead)]["registration"], "registered")
        self.assertEqual(rows[str(clean)]["registration"], "registered")
        # The local-only fixture repo has no upstream, so the dirty row also
        # carries the no-remote handoff; unregistered adds the registry
        # handoff (exact file path) after the work-securing fixes.
        self.assertEqual(
            rows[str(dirty)]["fix"],
            [
                f"git -C {dirty} add -p && git -C {dirty} commit",
                "add a remote or register intent",
                f"register in {self.registry_yaml} or add an ignore rule there",
            ],
        )
        self.assertEqual(rows[str(ahead)]["fix"], [f"git -C {ahead} push"])
        self.assertEqual(rows[str(clean)]["fix"], [])

        # Estate-level registration summary + the stale-registered section.
        self.assertEqual(
            report["registration_summary"],
            {"registered": 2, "unregistered": 1, "unknown": 0, "stale_registered": 1},
        )
        self.assertEqual(
            report["stale_registered"],
            [
                {
                    "path": str(gone),
                    "id": "gone",
                    "registration": "stale-registered",
                    "fix": [f"remove or repoint the registry entry in {self.registry_yaml}"],
                }
            ],
        )

        # cwd detail probes the enclosing repo root, even from a subdirectory,
        # and carries its registration state.
        sub = dirty / "nested"
        sub.mkdir()
        nested = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=str(sub))
        self.assertEqual(nested["cwd_repo"]["path"], str(dirty))
        self.assertEqual(nested["cwd_repo"]["risk_band"], "dirty")
        self.assertEqual(nested["cwd_repo"]["registration"], "unregistered")

        # Deterministic: a second scan yields the same row order.
        again = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=str(dirty))
        self.assertEqual([row["path"] for row in again["repos"]], paths)

    def test_ignore_matched_repo_is_not_counted_unregistered(self) -> None:
        ignored = self.make_repo("z-ignored")
        registered = self.make_repo("a-registered")
        self.write_config_fixture(
            ignore=[{"path": str(ignored), "reason": "fixture"}],
            repos=[{"id": "a", "path": str(registered)}],
        )
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        self.assertEqual(report["ignored_count"], 1)
        self.assertEqual(
            report["registration_summary"],
            {"registered": 1, "unregistered": 0, "unknown": 0, "stale_registered": 0},
        )

    def test_unregistered_outranks_registered_within_a_band(self) -> None:
        registered = self.make_repo("a-dirty")  # path-sorts FIRST
        (registered / "loose.txt").write_text("loose\n", encoding="utf-8")
        unregistered = self.make_repo("b-dirty")
        (unregistered / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture(repos=[{"id": "a", "path": str(registered)}])

        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        self.assertEqual(
            [row["path"] for row in report["repos"]],
            [str(unregistered), str(registered)],
        )
        self.assertEqual(
            [row["registration"] for row in report["repos"]],
            ["unregistered", "registered"],
        )

    def test_registry_absent_degrades_loudly_and_unfiltered(self) -> None:
        repo = self.make_repo("solo")
        # No config fixture written: SKILLBOX_CONFIG_ROOT points at nothing.
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        self.assertFalse(report["registry_applied"])
        self.assertEqual(report["ignored_count"], 0)
        self.assertEqual([row["path"] for row in report["repos"]], [str(repo)])
        self.assertTrue(
            any(
                note.startswith("registry unavailable:") and note.endswith("showing unfiltered")
                for note in report["notes"]
            ),
            report["notes"],
        )
        # Registration degrades to "unknown" everywhere, never a crash.
        self.assertEqual(report["repos"][0]["registration"], "unknown")
        self.assertEqual(
            report["registration_summary"],
            {"registered": 0, "unregistered": 0, "unknown": 1, "stale_registered": 0},
        )
        self.assertEqual(report["stale_registered"], [])

    def test_registry_absent_registration_filter_notes_and_empties(self) -> None:
        self.make_repo("solo")
        report = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["unregistered"]
        )
        self.assertEqual(report["repos"], [])
        self.assertEqual(report["repo_count"], 0)
        self.assertTrue(
            any("registration unknown" in note for note in report["notes"]),
            report["notes"],
        )

    def test_only_filter_composes_classes_and_registration(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        clean = self.make_clean_clone("b-clean")
        gone = self.estate / "gone-checkout"
        self.write_config_fixture(
            repos=[
                {"id": "a-dirty", "path": str(dirty)},
                {"id": "gone", "path": str(gone)},
            ],
        )

        only_dirty = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["dirty"]
        )
        self.assertEqual([row["path"] for row in only_dirty["repos"]], [str(dirty)])
        self.assertEqual(only_dirty["filters"], ["dirty"])

        # `unregistered` is a REAL row filter now: the unregistered clean
        # clone matches, the registered dirty repo does not.
        only_unreg = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["unregistered"]
        )
        self.assertEqual([row["path"] for row in only_unreg["repos"]], [str(clean)])
        self.assertEqual(only_unreg["repos"][0]["registration"], "unregistered")
        self.assertEqual(only_unreg["notes"], [])

        # `stale-registered` filters rows to nothing (stale entries are not
        # scanned rows) while the envelope still carries the stale section.
        only_stale = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["stale-registered"]
        )
        self.assertEqual(only_stale["repos"], [])
        self.assertEqual(
            [entry["path"] for entry in only_stale["stale_registered"]], [str(gone)]
        )

        # Union with git classes, matching every other --only token.
        mixed = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["dirty", "unregistered"]
        )
        self.assertEqual(
            {row["path"] for row in mixed["repos"]}, {str(dirty), str(clean)}
        )
        self.assertEqual(mixed["filters"], ["dirty", "unregistered"])

        with self.assertRaises(ValueError):
            git_estate.build_report(roots=[str(self.estate)], depth=2, only=["bogus"])

    def test_cwd_outside_any_repo_yields_null_detail(self) -> None:
        self.write_config_fixture()
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=str(outside))
        self.assertIsNone(report["cwd_repo"])


class TextRenderingTests(GitEstateFixtureCase):
    def test_clean_rows_fold_detail_first_and_footer(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        clean = self.make_clean_clone("b-clean")
        self.write_config_fixture()

        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=str(dirty))
        lines = git_estate.report_text_lines(report, color=False)
        text = "\n".join(lines)

        # cwd detail block comes before the estate rollup.
        self.assertLess(text.index("cwd repo:"), text.index("estate:"))
        self.assertIn(f"cwd repo: {dirty}", text)
        # Clean repos are one count line, not rows.
        self.assertNotIn(str(clean), text)
        self.assertIn("1 clean-current repos", text)
        self.assertIn("0 ignored by registry rules", text)
        self.assertIn("issues:", text)
        self.assertIn("  - dirty: 1", text)
        self.assertIn("next_actions:", text)
        self.assertIn(f"  - git -C {dirty} add -p && git -C {dirty} commit", text)
        # Plain output when not a tty.
        self.assertNotIn("\033[", text)

    def test_color_only_when_requested(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture()
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        colored = "\n".join(git_estate.report_text_lines(report, color=True))
        self.assertIn("\033[", colored)

    def test_registry_unavailable_note_is_rendered(self) -> None:
        self.make_repo("solo")
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("registry unavailable:", text)
        self.assertNotIn("ignored by registry rules", text)
        # No registration summary line without a registry to join against.
        self.assertNotIn("registration:", text)

    def test_registration_summary_line_and_unregistered_marker(self) -> None:
        registered = self.make_repo("a-dirty")
        (registered / "loose.txt").write_text("loose\n", encoding="utf-8")
        unregistered = self.make_repo("b-dirty")
        (unregistered / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture(repos=[{"id": "a", "path": str(registered)}])

        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        text = "\n".join(git_estate.report_text_lines(report, color=False))
        self.assertIn("registration: 1 registered, 1 unregistered, 0 stale-registered", text)
        self.assertIn(f"{unregistered}  [unregistered]", text)
        self.assertNotIn(f"{registered}  [unregistered]", text)
        self.assertIn(
            f"register in {self.registry_yaml} or add an ignore rule there", text
        )

    def test_stale_section_renders_by_default_and_under_its_own_filter(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        gone = self.estate / "gone-checkout"
        self.write_config_fixture(
            repos=[{"id": "a", "path": str(dirty)}, {"id": "gone", "path": str(gone)}]
        )

        default = git_estate.build_report(roots=[str(self.estate)], depth=2)
        default_text = "\n".join(git_estate.report_text_lines(default))
        self.assertIn("stale-registered: 1 registry entries with no repo on disk", default_text)
        self.assertIn(
            f"  - {gone}  -> remove or repoint the registry entry in {self.registry_yaml}",
            default_text,
        )

        asked = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["stale-registered"]
        )
        asked_text = "\n".join(git_estate.report_text_lines(asked))
        self.assertIn("stale-registered: 1 registry entries", asked_text)

        # An unrelated --only view keeps its focus: no stale section.
        narrowed = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["dirty"]
        )
        narrowed_text = "\n".join(git_estate.report_text_lines(narrowed))
        self.assertNotIn("stale-registered: 1", narrowed_text)

    def test_cwd_detail_shows_unregistered_state(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture()
        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=str(dirty))
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("registration: unregistered (not in registry, not ignore-matched)", text)

    def stash_with_age(self, repo: Path, content: str, age: timedelta) -> None:
        """One stash entry backdated by ``age`` (committer date pinned via
        GIT_COMMITTER_DATE; stash timestamps come from the stash commit)."""
        (repo / "tracked.txt").write_text(content, encoding="utf-8")
        date = (datetime.now(timezone.utc) - age).isoformat()
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": date}):
            self.git(repo, "stash", "push", "-q", "-m", content.strip())

    def add_parked_branch(self, repo: Path, name: str = "parked-work") -> None:
        self.git(repo, "checkout", "-q", "-b", name)
        (repo / f"{name}.txt").write_text("parked\n", encoding="utf-8")
        self.git(repo, "add", f"{name}.txt")
        self.git(repo, "commit", "-q", "-m", "parked work")
        self.git(repo, "checkout", "-q", "main")

    def test_clean_row_with_unpushed_branch_stays_visible(self) -> None:
        clone = self.make_clean_clone("a-parked")
        self.add_parked_branch(clone)
        self.write_config_fixture(repos=[{"id": "a-parked", "path": str(clone)}])
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)

        row = next(r for r in report["repos"] if r["path"] == str(clone))
        self.assertEqual(row["risk_band"], "clean")  # HEAD is clean-current
        self.assertEqual(
            row["unpushed_branches"], [{"name": "parked-work", "ahead": 1}]
        )
        self.assertIsNone(row["branch_scan_note"])

        text = "\n".join(git_estate.report_text_lines(report, color=False))
        # The silent-loss class must not fold away with the clean rows.
        self.assertIn(f"{clone}  [+1 unpushed branch]", text)
        self.assertNotIn("rows folded", text)
        # It joins next_actions without inventing an issue band.
        self.assertNotIn("issues:", text)
        self.assertIn("next_actions:", text)
        self.assertIn(
            f"git -C {clone} branch -vv  # 1 unpushed branch: parked-work(+1)", text
        )

    def test_stash_only_row_carries_age_marker(self) -> None:
        clone = self.make_clean_clone("a-stash-aged")
        self.stash_with_age(clone, "old stash\n", timedelta(days=40, hours=2))
        self.stash_with_age(clone, "new stash\n", timedelta(days=3, hours=2))
        self.write_config_fixture(repos=[{"id": "a-stash-aged", "path": str(clone)}])
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        text = "\n".join(git_estate.report_text_lines(report, color=False))
        self.assertIn(f"{clone}  [stash newest 3d, oldest 40d]", text)

    def test_cwd_detail_shows_stash_ages_and_unpushed_branches(self) -> None:
        repo = self.make_repo("a-mixed")
        self.stash_with_age(repo, "old stash\n", timedelta(days=40, hours=2))
        self.stash_with_age(repo, "new stash\n", timedelta(days=3, hours=2))
        self.add_parked_branch(repo, "parked-work")
        self.write_config_fixture()
        report = git_estate.build_report(
            roots=[str(self.estate)], depth=2, cwd=str(repo)
        )
        text = "\n".join(git_estate.report_text_lines(report, color=False))
        self.assertIn("  stash: 2 (newest 3d, oldest 40d)", text)
        # No remote at all: every parked commit is absent from any remote,
        # so the count covers the branch's whole history (base + parked).
        self.assertIn("  unpushed branches: parked-work (+2)", text)

    def test_cwd_detail_renders_branch_scan_note(self) -> None:
        # Synthetic report: the note render needs no 51-branch fixture.
        record = _record(
            "/r/many", branch_scan_note="branch scan skipped: 73 local branches"
        )
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "roots": ["/r"],
            "repos": [],
            "cwd_repo": git_estate._row(record),
        }
        text = "\n".join(git_estate.report_text_lines(report, color=False))
        self.assertIn("  note: branch scan skipped: 73 local branches", text)

    def test_stash_age_absent_keeps_plain_count_line(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture()
        report = git_estate.build_report(
            roots=[str(self.estate)], depth=2, cwd=str(dirty)
        )
        text = "\n".join(git_estate.report_text_lines(report, color=False))
        self.assertIn("  stash: 0", text)
        self.assertNotIn("(newest", text)


class WrapperAliasTests(GitEstateFixtureCase):
    """`sbp git` / `sbp gs` / `sbp git status` through the real wrapper."""

    def run_sbp(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SBP), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "SKILLBOX_ROOT": str(ROOT),
                "SKILLBOX_INVOKE_CWD": str(self.estate),
                "SKILLBOX_CONFIG_ROOT": str(self.config_root),
            },
        )

    def scan_args(self) -> list[str]:
        return ["--root", str(self.estate), "--depth", "2"]

    def test_git_gs_and_git_status_are_the_same_command(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture()

        payloads = []
        for alias in (["git"], ["gs"], ["git", "status"]):
            result = self.run_sbp(*alias, "--json", *self.scan_args())
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema"], "sbp-git/v1")
            payloads.append(payload)
        self.assertEqual(
            [p["repo_count"] for p in payloads], [payloads[0]["repo_count"]] * 3
        )
        self.assertEqual(
            [row["path"] for row in payloads[0]["repos"]],
            [row["path"] for row in payloads[1]["repos"]],
        )

    def test_git_push_is_refused_never_proxied(self) -> None:
        self.write_config_fixture()
        result = self.run_sbp("git", "push")
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing to proxy", result.stderr)
        self.assertIn("Usage:", result.stderr)

    def test_unknown_only_class_exits_2_with_vocabulary(self) -> None:
        self.write_config_fixture()
        result = self.run_sbp("git", "--only", "bogus", *self.scan_args())
        self.assertEqual(result.returncode, 2)
        self.assertIn("valid classes:", result.stderr)
        self.assertIn("diverged-clean", result.stderr)

    def test_only_unregistered_yields_real_rows_through_wrapper(self) -> None:
        registered = self.make_repo("a-registered")
        unregistered = self.make_repo("b-unregistered")
        self.write_config_fixture(repos=[{"id": "a", "path": str(registered)}])

        result = self.run_sbp(
            "git", "--json", "--only", "unregistered", *self.scan_args()
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "sbp-git/v1")
        self.assertEqual(
            [row["path"] for row in payload["repos"]], [str(unregistered)]
        )
        self.assertEqual(payload["repos"][0]["registration"], "unregistered")
        self.assertEqual(
            payload["registration_summary"],
            {"registered": 1, "unregistered": 1, "unknown": 0, "stale_registered": 0},
        )

    def test_piped_output_is_plain_text(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture()
        result = self.run_sbp("gs", *self.scan_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\033[", result.stdout)
        self.assertIn("estate:", result.stdout)


if __name__ == "__main__":
    unittest.main()
