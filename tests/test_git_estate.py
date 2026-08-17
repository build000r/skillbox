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

from tests import helpers  # noqa: E402
from runtime_manager import git_inventory  # noqa: E402
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


def _lane_row(path: str, **overrides) -> dict:
    """A minimal envelope ROW (lane planning is pure over rows, not records)."""
    row = {
        "path": path,
        "risk_band": "clean",
        "classes": ["clean-current"],
        "registration": "registered",
        "ahead": 0,
        "behind": 0,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "stash_count": 0,
        "ownership": git_estate.OWNERSHIP_OPERATOR,
        "push_policy": git_estate.PUSH_POLICY_PUSH,
    }
    row.update(overrides)
    return row


class LanePlanTests(unittest.TestCase):
    """The envelope hands over the division the coordinator used to hand-build.

    The 2026-08-15 brief partitioned 55 issue rows into 5 lanes with prose
    write scopes; every rule it applied was mechanical. These pin the rules.
    """

    # -- the assignment ladder --------------------------------------------

    def test_each_band_shape_lands_in_its_lane(self) -> None:
        cases = {
            git_estate.LANE_DIVERGED: _lane_row(
                "/r/d", risk_band="diverged", classes=["ahead", "behind", "diverged-clean"],
                ahead=2, behind=3,
            ),
            git_estate.LANE_DIRTY_BEHIND: _lane_row(
                "/r/db", risk_band="dirty-behind", classes=["dirty", "behind"],
                behind=3, unstaged=1,
            ),
            git_estate.LANE_CONVERGE: _lane_row(
                "/r/b", risk_band="behind-clean", classes=["behind"], behind=4
            ),
            git_estate.LANE_PUSH_AHEAD: _lane_row(
                "/r/a", risk_band="ahead", classes=["ahead"], ahead=2
            ),
            git_estate.LANE_SMALL_DIRTY: _lane_row(
                "/r/s", risk_band="dirty", classes=["dirty"], unstaged=2
            ),
            git_estate.LANE_UNREGISTERED_DIRTY: _lane_row(
                "/r/u", risk_band="dirty", classes=["dirty"], unstaged=1,
                registration="unregistered",
            ),
        }
        for kind, row in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(git_estate.lane_kind_for_row(row), kind)

    def test_a_clean_row_needs_no_lane(self) -> None:
        self.assertIsNone(git_estate.lane_kind_for_row(_lane_row("/r/clean")))

    def test_every_emitted_kind_is_in_the_declared_vocabulary(self) -> None:
        self.assertEqual(
            set(git_estate.EMITTED_LANE_KINDS) - set(git_estate.LANE_KINDS), set()
        )

    def test_doc_only_is_declared_but_never_emitted(self) -> None:
        # It needs file-level data the read-only glance does not probe.
        self.assertIn(git_estate.LANE_DOC_ONLY, git_estate.LANE_KINDS)
        self.assertNotIn(git_estate.LANE_DOC_ONLY, git_estate.EMITTED_LANE_KINDS)

    # -- the convergence contract (VISION CORRECTION) ----------------------

    def test_no_lane_kind_ends_in_a_side_ref(self) -> None:
        # Safety branches are the debris a reconcile eliminates, not an
        # outcome. There must be no lane whose end state is a new side ref.
        for kind in git_estate.LANE_KINDS:
            self.assertNotIn("safety", kind)
            self.assertNotIn("backup", kind)
            self.assertNotIn("snapshot", kind)

    def test_a_convergence_lane_exists_for_behind_rows(self) -> None:
        lanes = git_estate.build_lane_plan(
            [_lane_row("/r/b", risk_band="behind-clean", classes=["behind"], behind=4)]
        )
        self.assertEqual([lane["kind"] for lane in lanes], [git_estate.LANE_CONVERGE])
        self.assertIn("parity", lanes[0]["rationale"])

    # -- typed withholds ---------------------------------------------------

    def test_an_external_upstream_is_withheld_not_dispatched(self) -> None:
        row = _lane_row(
            "/r/ext", risk_band="ahead", classes=["ahead"], ahead=2,
            ownership=git_estate.OWNERSHIP_EXTERNAL,
            push_policy=git_estate.PUSH_POLICY_NO_PUSH,
            push_policy_reason="external upstream (tetsuo-ai)",
        )
        self.assertEqual(git_estate.lane_kind_for_row(row), git_estate.LANE_WITHHELD)
        lane = git_estate.build_lane_plan([row])[0]
        self.assertEqual(lane["withheld"][0]["path"], "/r/ext")
        self.assertIn("tetsuo-ai", lane["withheld"][0]["reason"])

    def test_a_declared_remoteless_repo_is_withheld_with_its_declaration(self) -> None:
        # Live: 5 repos are registry-declared "Deliberately remoteless".
        # Telling an agent to add a remote to one would be actively wrong.
        row = _lane_row(
            "/r/local", risk_band="no-remote", classes=["no-remote"],
            ownership=git_estate.OWNERSHIP_LOCAL,
            push_policy=git_estate.PUSH_POLICY_NO_PUSH,
        )
        lane = git_estate.build_lane_plan([row])[0]
        self.assertEqual(lane["kind"], git_estate.LANE_WITHHELD)
        self.assertIn("by declaration", lane["withheld"][0]["reason"])

    def test_a_dirty_remoteless_repo_is_still_dispatchable(self) -> None:
        # Committing secures the work; that is safe and valuable even with
        # nowhere to push. Only the nothing-else-to-do case is withheld.
        row = _lane_row(
            "/r/local", risk_band="dirty", classes=["no-remote", "dirty"],
            unstaged=2, ownership=git_estate.OWNERSHIP_LOCAL,
            push_policy=git_estate.PUSH_POLICY_NO_PUSH,
        )
        self.assertEqual(git_estate.lane_kind_for_row(row), git_estate.LANE_SMALL_DIRTY)

    def test_a_mid_op_row_is_a_judgment_block(self) -> None:
        row = _lane_row("/r/m", risk_band="mid-op", classes=["mid-op"], mid_op="merge")
        lane = git_estate.build_lane_plan([row])[0]
        self.assertEqual(lane["kind"], git_estate.LANE_WITHHELD)
        self.assertIn("merge in flight", lane["withheld"][0]["reason"])

    def test_a_withheld_lane_is_not_dispatchable_work(self) -> None:
        row = _lane_row("/r/m", risk_band="mid-op", classes=["mid-op"], mid_op="rebase")
        self.assertEqual(git_estate.build_lane_plan([row])[0]["suggested_concurrency"], 0)

    def test_a_blocked_probe_is_withheld_never_silently_dropped(self) -> None:
        row = _lane_row(
            "/r/x", risk_band="blocked", classes=["blocked"], error="permission denied"
        )
        lane = git_estate.build_lane_plan([row])[0]
        self.assertEqual(lane["kind"], git_estate.LANE_WITHHELD)
        self.assertIn("permission denied", lane["withheld"][0]["reason"])

    # -- the worktree hard rule -------------------------------------------

    def test_a_repo_and_its_worktrees_never_split_across_lanes(self) -> None:
        # The live incident: lanes partitioned by directory let L2 push L4's
        # branch through the shared git dir.
        parent = _lane_row("/r/main", risk_band="dirty", classes=["dirty"], unstaged=1)
        worktree = _lane_row(
            "/r/wt", risk_band="ahead", classes=["ahead"], ahead=3, worktree_of="/r/main"
        )
        lanes = git_estate.build_lane_plan([parent, worktree])
        self.assertEqual(len(lanes), 1, "family was split across lanes")
        self.assertEqual(
            sorted(lanes[0]["repos"]), ["/r/main", "/r/wt"]
        )

    def test_the_family_takes_its_most_urgent_members_lane(self) -> None:
        parent = _lane_row("/r/main", risk_band="dirty", classes=["dirty"], unstaged=1)
        worktree = _lane_row(
            "/r/wt", risk_band="diverged", classes=["ahead", "behind", "diverged-clean"],
            ahead=1, behind=1, worktree_of="/r/main",
        )
        lanes = git_estate.build_lane_plan([parent, worktree])
        self.assertEqual(lanes[0]["kind"], git_estate.LANE_DIVERGED)

    def test_write_scope_covers_clean_siblings_on_the_shared_store(self) -> None:
        # Writing through a shared git dir touches every worktree on it, so a
        # CLEAN sibling still belongs to the write scope even though it is not
        # itself a row to work.
        parent = _lane_row("/r/main", risk_band="dirty", classes=["dirty"], unstaged=1)
        clean_wt = _lane_row("/r/wt-clean", worktree_of="/r/main")
        lane = git_estate.build_lane_plan([parent, clean_wt])[0]
        self.assertEqual(lane["repos"], ["/r/main"])
        self.assertIn("/r/wt-clean", lane["write_scope"])
        self.assertEqual(sorted(lane["write_scope"]), ["/r/main", "/r/wt-clean"])

    # -- envelope contract -------------------------------------------------

    def test_absent_when_nothing_needs_a_lane(self) -> None:
        self.assertEqual(git_estate.build_lane_plan([]), [])
        self.assertEqual(git_estate.build_lane_plan([_lane_row("/r/clean")]), [])

    def test_lane_ids_are_sequential_in_emission_order(self) -> None:
        rows = [
            _lane_row("/r/a", risk_band="ahead", classes=["ahead"], ahead=1),
            _lane_row("/r/s", risk_band="dirty", classes=["dirty"], unstaged=1),
            _lane_row("/r/m", risk_band="mid-op", classes=["mid-op"], mid_op="merge"),
        ]
        lanes = git_estate.build_lane_plan(rows)
        self.assertEqual([lane["id"] for lane in lanes], ["L1", "L2", "L3"])
        # Ladder order, not input order: withheld leads.
        self.assertEqual(lanes[0]["kind"], git_estate.LANE_WITHHELD)

    def test_the_plan_is_deterministic_across_runs_and_input_order(self) -> None:
        rows = [
            _lane_row("/r/b", risk_band="behind-clean", classes=["behind"], behind=1),
            _lane_row("/r/a", risk_band="ahead", classes=["ahead"], ahead=1),
            _lane_row("/r/s", risk_band="dirty", classes=["dirty"], unstaged=1),
        ]
        first = git_estate.build_lane_plan(rows)
        self.assertEqual(first, git_estate.build_lane_plan(rows))
        self.assertEqual(first, git_estate.build_lane_plan(list(reversed(rows))))

    def test_every_lane_carries_the_full_contract(self) -> None:
        rows = [_lane_row("/r/a", risk_band="ahead", classes=["ahead"], ahead=1)]
        for lane in git_estate.build_lane_plan(rows):
            for key in (
                "id", "kind", "repos", "write_scope", "rationale", "suggested_concurrency"
            ):
                self.assertIn(key, lane)
            self.assertIn(lane["kind"], git_estate.EMITTED_LANE_KINDS)
            self.assertTrue(lane["rationale"])

    def test_concurrency_is_one_per_independent_family_and_capped(self) -> None:
        many = [
            _lane_row(f"/r/a{i}", risk_band="ahead", classes=["ahead"], ahead=1)
            for i in range(10)
        ]
        lane = git_estate.build_lane_plan(many)[0]
        self.assertEqual(lane["suggested_concurrency"], git_estate.MAX_LANE_CONCURRENCY)

    def test_worktree_families_count_once_toward_concurrency(self) -> None:
        rows = [
            _lane_row("/r/main", risk_band="ahead", classes=["ahead"], ahead=1),
            _lane_row("/r/wt", risk_band="ahead", classes=["ahead"], ahead=1, worktree_of="/r/main"),
        ]
        lane = git_estate.build_lane_plan(rows)[0]
        self.assertEqual(lane["suggested_concurrency"], 1)

    def test_every_issue_row_lands_in_exactly_one_lane(self) -> None:
        # The 2026-08-15 shape in miniature: no row may be dropped, and none
        # may appear twice.
        rows = [
            _lane_row("/r/m", risk_band="mid-op", classes=["mid-op"], mid_op="merge"),
            _lane_row("/r/d", risk_band="diverged", classes=["ahead", "behind", "diverged-clean"], ahead=1, behind=1),
            _lane_row("/r/db", risk_band="dirty-behind", classes=["dirty", "behind"], behind=2, unstaged=1),
            _lane_row("/r/b", risk_band="behind-clean", classes=["behind"], behind=1),
            _lane_row("/r/a", risk_band="ahead", classes=["ahead"], ahead=1),
            _lane_row("/r/u", risk_band="dirty", classes=["dirty"], unstaged=1, registration="unregistered"),
            _lane_row("/r/s", risk_band="dirty", classes=["dirty"], unstaged=1),
            _lane_row("/r/mech", unpushed_branches=[{"name": "f", "ahead": 1}]),
            _lane_row("/r/clean"),
        ]
        lanes = git_estate.build_lane_plan(rows)
        placed = [path for lane in lanes for path in lane["repos"]]
        self.assertEqual(len(placed), len(set(placed)), "a row landed in two lanes")
        expected = {r["path"] for r in rows if git_estate.lane_kind_for_row(r)}
        self.assertEqual(set(placed), expected)
        self.assertNotIn("/r/clean", placed)


class MisconfiguredUpstreamBandingTests(unittest.TestCase):
    """The top of the risk table must never be a config artifact."""

    def _mismatch(self, ahead_vs: int = 0, behind_vs: int = 0):
        return git_inventory.UpstreamMismatch(
            configured="origin/main",
            same_name="origin/codex/qbo",
            ahead_vs_same_name=ahead_vs,
            behind_vs_same_name=behind_vs,
        )

    def _cfo_record(self, **overrides) -> GitRepoRecord:
        defaults = dict(
            classes=frozenset({"clean-current"}),
            primary_class="clean-current",
            branch="codex/qbo",
            upstream="origin/main",
            ahead=3,
            behind=58,
            upstream_mismatch=self._mismatch(),
        )
        defaults.update(overrides)
        return _record("/r/cfo", **defaults)

    def test_the_false_diverged_row_no_longer_bands_diverged(self) -> None:
        record = self._cfo_record()
        band = git_estate.RISK_BAND_NAMES[git_estate.risk_band(record)]
        self.assertNotEqual(band, "diverged")
        self.assertEqual(band, "clean")

    def test_genuine_divergence_is_untouched(self) -> None:
        record = _record(
            "/r/real",
            classes=frozenset({"ahead", "behind", "diverged-clean"}),
            primary_class="diverged-clean",
            ahead=3,
            behind=58,
            upstream_mismatch=None,
        )
        self.assertEqual(
            git_estate.RISK_BAND_NAMES[git_estate.risk_band(record)], "diverged"
        )

    def test_the_configured_counts_survive_for_the_reader(self) -> None:
        # The A/B column keeps saying what the CONFIGURED upstream says: that
        # is a real fact about the config, and the marker is what tells the
        # reader those numbers are an artifact.
        row = git_estate._row(self._cfo_record())
        self.assertEqual((row["ahead"], row["behind"]), (3, 58))
        self.assertEqual(row["upstream_mismatch"]["configured"], "origin/main")
        self.assertEqual(row["upstream_mismatch"]["same_name"], "origin/codex/qbo")
        self.assertEqual(row["upstream_mismatch"]["ahead_vs_same_name"], 0)

    def test_the_fix_repairs_the_config_instead_of_reconciling(self) -> None:
        fixes = git_estate.fix_commands(self._cfo_record())
        self.assertEqual(
            fixes,
            [
                "git -C /r/cfo branch --set-upstream-to origin/codex/qbo"
                "  # upstream points at origin/main"
            ],
        )
        # Emphatically NOT the divergence handoff: there is no divergence.
        self.assertNotIn("sbp doctor / reconcile skill — do not hand-merge", fixes)

    def test_a_genuinely_diverged_row_still_gets_the_reconcile_handoff(self) -> None:
        record = _record(
            "/r/real",
            classes=frozenset({"ahead", "behind", "diverged-clean"}),
            primary_class="diverged-clean",
            ahead=3,
            behind=58,
        )
        self.assertIn(
            "sbp doctor / reconcile skill — do not hand-merge",
            git_estate.fix_commands(record),
        )

    def test_a_still_behind_row_gets_a_pull_not_a_reconcile(self) -> None:
        # Config is wrong AND the same-name ref really has moved on: repair
        # the upstream, then fast-forward. Still never a hand-merge.
        record = self._cfo_record(upstream_mismatch=self._mismatch(behind_vs=4))
        fixes = git_estate.fix_commands(record)
        self.assertIn("git -C /r/cfo branch --set-upstream-to origin/codex/qbo"
                      "  # upstream points at origin/main", fixes)
        self.assertIn("git -C /r/cfo pull --ff-only  # or /reconcile", fixes)

    def test_the_row_is_marked_in_the_tty(self) -> None:
        row = git_estate._row(self._cfo_record())
        lines = git_estate._table_lines([row], False)
        self.assertTrue(any("[upstream-misconfigured]" in line for line in lines))

    def test_no_push_badge_on_a_row_with_nothing_to_publish(self) -> None:
        # ahead=3 against the wrong ref, 0 against the right one. Badging this
        # with a push policy would be advice about work that does not exist.
        row = git_estate._row(self._cfo_record())
        row["push_policy"] = git_estate.PUSH_POLICY_ASK
        lines = git_estate._table_lines([row], False)
        self.assertFalse(any("[ask]" in line for line in lines))

    def test_the_row_stays_visible_despite_banding_clean(self) -> None:
        # Trading a false alarm for silence would be no improvement: the row
        # still carries a repair, so it earns footer next_actions.
        row = git_estate._row(self._cfo_record())
        self.assertTrue(git_estate._is_issue_row(row))

    def test_a_clean_row_without_a_mismatch_is_still_not_an_issue(self) -> None:
        self.assertFalse(git_estate._is_issue_row(git_estate._row(_record("/r/clean"))))

    def test_rows_without_a_mismatch_project_null(self) -> None:
        self.assertIsNone(git_estate._row(_record("/r/clean"))["upstream_mismatch"])


class SameNameRefTests(unittest.TestCase):
    """Which ref the branch SHOULD have been measured against."""

    def test_the_remote_comes_from_the_configured_upstream(self) -> None:
        # A fork tracking upstream/main is compared against upstream/<branch>,
        # not origin/<branch>: that is the ref that would explain its commits.
        self.assertEqual(
            git_inventory._same_name_ref("feature", "upstream/main"),
            "upstream/feature",
        )

    def test_a_branch_already_on_its_own_ref_is_skipped(self) -> None:
        self.assertIsNone(git_inventory._same_name_ref("main", "origin/main"))

    def test_slashed_branch_names_round_trip(self) -> None:
        self.assertEqual(
            git_inventory._same_name_ref("codex/qbo", "origin/main"),
            "origin/codex/qbo",
        )

    def test_unusable_inputs_yield_nothing(self) -> None:
        for branch, configured in (
            ("", "origin/main"),
            (git_inventory.BRANCH_DETACHED, "origin/main"),
            ("feature", "no-slash"),
            ("feature", ""),
        ):
            with self.subTest(branch=branch, configured=configured):
                self.assertIsNone(git_inventory._same_name_ref(branch, configured))


class EffectiveAheadBehindTests(unittest.TestCase):
    def test_without_a_mismatch_the_records_own_numbers_are_used(self) -> None:
        record = _record("/r/x", ahead=4, behind=2)
        self.assertEqual(git_inventory.effective_ahead_behind(record), (4, 2))

    def test_with_a_mismatch_the_same_name_numbers_are_used(self) -> None:
        record = _record(
            "/r/x",
            ahead=3,
            behind=58,
            upstream_mismatch=git_inventory.UpstreamMismatch(
                configured="origin/main",
                same_name="origin/f",
                ahead_vs_same_name=0,
                behind_vs_same_name=1,
            ),
        )
        self.assertEqual(git_inventory.effective_ahead_behind(record), (0, 1))


class WorktreeIdentityTests(unittest.TestCase):
    """Who is the primary checkout behind a linked worktree.

    Uses synthetic records: the identity rule is ``git_dir != common_dir``,
    which is a pair of strings, so real worktree plumbing proves nothing extra
    here. (The end-to-end proof over git's own worktree-add plumbing is the
    blocked item recorded in WG-era21_RESULT.md.)
    """

    def _wt(self, path: str, git_dir: str, common: str) -> GitRepoRecord:
        return _record(path, git_dir=git_dir, common_dir=common)

    def test_a_main_worktree_has_no_parent(self) -> None:
        main = self._wt("/r/main", "/r/main/.git", "/r/main/.git")
        self.assertIsNone(git_estate.worktree_primary(main))

    def test_a_linked_worktree_names_its_scanned_parent(self) -> None:
        main = self._wt("/r/main", "/r/main/.git", "/r/main/.git")
        linked = self._wt("/r/wt", "/r/main/.git/worktrees/wt", "/r/main/.git")
        parents = git_estate.worktree_primaries([main, linked])
        self.assertEqual(git_estate.worktree_primary(linked, parents), "/r/main")

    def test_a_linked_worktree_derives_its_parent_when_unscanned(self) -> None:
        # The parent lives outside the scan roots. Git puts a main worktree's
        # store at <primary>/.git, so the row still names it instead of
        # reporting nothing.
        linked = self._wt("/r/wt", "/r/main/.git/worktrees/wt", "/r/main/.git")
        self.assertEqual(git_estate.worktree_primary(linked, {}), "/r/main")

    def test_a_non_dot_git_store_is_not_guessed_at(self) -> None:
        # A bare repo serving worktrees: the store is not "<primary>/.git", so
        # there is no parent directory to name and the field stays absent.
        linked = self._wt("/r/wt", "/srv/bare.git/worktrees/wt", "/srv/bare.git")
        self.assertIsNone(git_estate.worktree_primary(linked, {}))

    def test_a_record_without_store_identity_is_never_grouped(self) -> None:
        # An old git (no --git-common-dir) or a blocked probe: unknown must not
        # read as "same store as every other unknown".
        blank = self._wt("/r/x", None, None)
        self.assertIsNone(git_estate.worktree_primary(blank, {}))

    def test_primaries_map_indexes_only_main_worktrees(self) -> None:
        main = self._wt("/r/main", "/r/main/.git", "/r/main/.git")
        linked = self._wt("/r/wt", "/r/main/.git/worktrees/wt", "/r/main/.git")
        self.assertEqual(
            git_estate.worktree_primaries([main, linked]),
            {"/r/main/.git": "/r/main"},
        )

    def test_rows_carry_worktree_of_only_when_linked(self) -> None:
        main = self._wt("/r/main", "/r/main/.git", "/r/main/.git")
        linked = self._wt("/r/wt", "/r/main/.git/worktrees/wt", "/r/main/.git")
        parents = git_estate.worktree_primaries([main, linked])
        main_row = git_estate._row(main, worktree_parents=parents)
        linked_row = git_estate._row(linked, worktree_parents=parents)
        self.assertNotIn("worktree_of", main_row)
        self.assertEqual(linked_row["worktree_of"], "/r/main")


class WorktreeBandingTests(unittest.TestCase):
    """A linked worktree must never overstate loss risk.

    ``no-remote`` reads as "this work exists nowhere else". For a worktree
    whose shared store has a remote that is false -- the 2026-08-15 run pushed
    four such "orphaned" branches trivially once they were reclassified.
    """

    def _classify(self, **kwargs):
        base = dict(
            mid_op=None,
            dirty=False,
            stash_count=0,
            upstream=None,
            ahead=0,
            behind=0,
        )
        base.update(kwargs)
        return git_inventory._classify(**base)

    def test_a_store_backed_worktree_is_never_no_remote(self) -> None:
        classes, primary = self._classify(linked_worktree=True, has_remote=True)
        self.assertNotIn("no-remote", classes)
        self.assertNotEqual(primary, "no-remote")
        self.assertIn("unpublished-branch", classes)

    def test_a_remoteless_worktree_still_bands_no_remote(self) -> None:
        # Here the scary reading is TRUE: the store has nowhere to push.
        classes, primary = self._classify(linked_worktree=True, has_remote=False)
        self.assertIn("no-remote", classes)
        self.assertEqual(primary, "no-remote")

    def test_a_main_worktree_banding_is_unchanged(self) -> None:
        # Scope: this bead reclassifies linked worktrees only. A main checkout
        # with remotes but no upstream keeps its existing band.
        classes, primary = self._classify(linked_worktree=False, has_remote=True)
        self.assertIn("no-remote", classes)
        self.assertEqual(primary, "no-remote")

    def test_a_store_backed_worktree_bands_from_its_own_checkout_state(self) -> None:
        classes, primary = self._classify(
            linked_worktree=True, has_remote=True, dirty=True
        )
        self.assertEqual(primary, "dirty")
        self.assertNotIn("no-remote", classes)

    def test_the_demoted_row_drops_out_of_the_no_remote_band(self) -> None:
        record = _record(
            "/r/wt",
            classes=frozenset({"unpublished-branch"}),
            primary_class="unpublished-branch",
            upstream=None,
            git_dir="/r/main/.git/worktrees/wt",
            common_dir="/r/main/.git",
        )
        band = git_estate.RISK_BAND_NAMES[git_estate.risk_band(record)]
        self.assertNotEqual(band, "no-remote")
        self.assertEqual(band, "clean")

    def test_an_upstream_backed_worktree_is_unaffected(self) -> None:
        classes, primary = self._classify(
            linked_worktree=True, has_remote=True, upstream="origin/feature", ahead=2
        )
        self.assertIn("ahead", classes)
        self.assertNotIn("unpublished-branch", classes)
        self.assertEqual(primary, "ahead-clean")

    def test_unpublished_branch_is_a_declared_class(self) -> None:
        # It has to be in every vocabulary, or consumers filtering on
        # ALL_CLASSES silently drop the rows this bead created.
        self.assertIn("unpublished-branch", git_inventory.ALL_CLASSES)
        self.assertIn("unpublished-branch", git_inventory.PRIMARY_CLASSES)
        self.assertIn("unpublished-branch", git_estate.FILTER_CLASSES)

    def test_the_reclassified_rows_are_askable_for_by_name(self) -> None:
        self.assertEqual(
            git_estate.parse_only(["unpublished-branch"]),
            (("unpublished-branch",), ()),
        )
        record = _record(
            "/r/wt",
            classes=frozenset({"unpublished-branch"}),
            primary_class="unpublished-branch",
            upstream=None,
        )
        self.assertTrue(git_estate._matches_only(record, "unpublished-branch"))
        self.assertFalse(git_estate._matches_only(record, "no-remote"))


class WorktreeStashDedupTests(unittest.TestCase):
    """One physical stash store is counted once, whatever the row count.

    The live run listed 26 stashes where 12 existed: linked worktrees share
    ONE stash store, and every row reported the same entries.
    """

    def _store(self, count: int) -> list[GitRepoRecord]:
        return [
            _record("/r/main", git_dir="/r/main/.git", common_dir="/r/main/.git", stash_count=count),
            _record("/r/wt-a", git_dir="/r/main/.git/worktrees/a", common_dir="/r/main/.git", stash_count=count),
            _record("/r/wt-b", git_dir="/r/main/.git/worktrees/b", common_dir="/r/main/.git", stash_count=count),
        ]

    def test_estate_total_counts_the_store_once(self) -> None:
        records = self._store(4)
        summary = git_estate.stash_summary(records)
        # Row math would say 12; the store holds 4.
        self.assertEqual(summary["row_total"], 12)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["shared_rows"], 2)
        self.assertEqual(summary["shared_stores"], 1)

    def test_the_main_worktree_owns_the_count(self) -> None:
        owners = git_estate.stash_store_owners(self._store(4))
        self.assertEqual(set(owners.values()), {"/r/main"})

    def test_sharer_rows_defer_instead_of_repeating_the_number(self) -> None:
        records = self._store(4)
        owners = git_estate.stash_store_owners(records)
        rows = [
            git_estate._row(r, stash_owner=owners.get(r.path)) for r in records
        ]
        by_path = {row["path"]: row for row in rows}
        # Every row names the store's primary...
        for path in ("/r/main", "/r/wt-a", "/r/wt-b"):
            self.assertEqual(by_path[path]["stash_store_primary"], "/r/main")
        # ...but only the primary is CREDITED with the entries, so the
        # attributed column sums to the store's real total instead of 3x it.
        self.assertEqual(by_path["/r/main"]["stash_attributed"], 4)
        self.assertEqual(by_path["/r/wt-a"]["stash_attributed"], 0)
        self.assertEqual(by_path["/r/wt-b"]["stash_attributed"], 0)
        self.assertEqual(sum(r["stash_attributed"] for r in rows), 4)
        # The honest per-checkout observation is never rewritten: a worktree
        # parked on a shared stash stays as visible as before.
        self.assertEqual(by_path["/r/wt-a"]["stash_count"], 4)

    def test_worktree_rows_carry_both_the_parent_and_the_store_primary(self) -> None:
        records = self._store(2)
        owners = git_estate.stash_store_owners(records)
        parents = git_estate.worktree_primaries(records)
        row = git_estate._row(
            records[1], stash_owner=owners.get(records[1].path), worktree_parents=parents
        )
        self.assertEqual(row["worktree_of"], "/r/main")
        self.assertEqual(row["stash_store_primary"], "/r/main")

    def test_an_unshared_store_is_untouched(self) -> None:
        solo = [_record("/r/solo", git_dir="/r/solo/.git", common_dir="/r/solo/.git", stash_count=3)]
        self.assertEqual(git_estate.stash_store_owners(solo), {})
        self.assertEqual(git_estate.stash_summary(solo)["total"], 3)


class RemoteOwnerParseTests(unittest.TestCase):
    """``parse_remote_owner`` over every URL spelling git actually returns."""

    def test_scp_style_ssh_url(self) -> None:
        self.assertEqual(
            git_estate.parse_remote_owner("git@github.com:choffmanebpm/pdsmvp.git"),
            ("github.com", "choffmanebpm"),
        )

    def test_https_url(self) -> None:
        self.assertEqual(
            git_estate.parse_remote_owner("https://github.com/tetsuo-ai/agenc-core.git"),
            ("github.com", "tetsuo-ai"),
        )

    def test_ssh_scheme_url_with_port(self) -> None:
        self.assertEqual(
            git_estate.parse_remote_owner("ssh://git@github.com:22/build000r/skillbox.git"),
            ("github.com", "build000r"),
        )

    def test_local_paths_have_no_owner(self) -> None:
        # The live run's third ownership class: a remote that is just a path.
        for url in ("/srv/mirrors/thing.git", "../sibling.git", "~/repos/x.git", "file:///tmp/x.git"):
            with self.subTest(url=url):
                self.assertEqual(git_estate.parse_remote_owner(url), (None, None))

    def test_empty_and_garbage_degrade_to_none(self) -> None:
        for url in ("", "   ", "not-a-url"):
            with self.subTest(url=url):
                self.assertEqual(git_estate.parse_remote_owner(url), (None, None))


class OwnershipDerivationTests(unittest.TestCase):
    """Who owns a row, and may a coordinator push to it.

    The bar from the bead: zero prose about who may push in a coordinator
    brief. Every case below therefore resolves to a value, a source, and a
    reason -- never to "it depends".
    """

    def _derive(self, remotes=(), entry=None, owner="build000r"):
        record = _record("/r/x", remotes=tuple(remotes))
        return git_estate.derive_ownership(
            record, registry_entry=entry, operator_owner=owner
        )

    # -- registry is the declared source ----------------------------------

    def test_registry_owned_is_operator_owned_and_pushable(self) -> None:
        result = self._derive(
            [("origin", "https://github.com/build000r/x.git")], {"ownership": "owned"}
        )
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_OPERATOR)
        self.assertEqual(result["ownership_source"], git_estate.OWNERSHIP_SOURCE_REGISTRY)
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_PUSH)

    def test_registry_owned_local_has_nowhere_to_push(self) -> None:
        result = self._derive([], {"ownership": "owned-local"})
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_LOCAL)
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_NO_PUSH)

    def test_registry_external_spellings_all_mean_no_push(self) -> None:
        # v6ac.6.4 will start writing these; accept them now so that bead is a
        # registry edit, not another change here.
        for spelling in ("external", "external-upstream", "upstream", "fork", "vendor"):
            with self.subTest(spelling=spelling):
                result = self._derive(
                    [("origin", "https://github.com/someone/x.git")],
                    {"ownership": spelling},
                )
                self.assertEqual(result["ownership"], git_estate.OWNERSHIP_EXTERNAL)
                self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_NO_PUSH)

    def test_an_unrecognized_registry_spelling_asks_rather_than_assumes(self) -> None:
        result = self._derive(
            [("origin", "https://github.com/build000r/x.git")],
            {"ownership": "something-new"},
        )
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_UNKNOWN)
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_ASK)

    def test_registered_without_a_remote_is_owned_local(self) -> None:
        result = self._derive([], {"id": "x"})
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_LOCAL)
        self.assertEqual(result["ownership_source"], git_estate.OWNERSHIP_SOURCE_REGISTRY)

    # -- the remote heuristic ---------------------------------------------

    def test_operator_account_remote_is_operator_owned(self) -> None:
        result = self._derive([("origin", "https://github.com/build000r/skillbox.git")])
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_OPERATOR)
        self.assertEqual(result["ownership_source"], git_estate.OWNERSHIP_SOURCE_HEURISTIC)
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_PUSH)

    def test_the_two_live_run_external_upstreams_are_no_push(self) -> None:
        # The exact repos the 2026-08-15 coordinator classified by hand, and
        # the exact URLs on disk.
        cases = {
            "https://github.com/tetsuo-ai/agenc-core.git": "tetsuo-ai",
            "git@github.com:choffmanebpm/pdsmvp.git": "choffmanebpm",
        }
        for url, owner in cases.items():
            with self.subTest(url=url):
                result = self._derive([("origin", url)])
                self.assertEqual(result["ownership"], git_estate.OWNERSHIP_EXTERNAL)
                self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_NO_PUSH)
                self.assertEqual(result["remote_owner"], owner)
                self.assertIn(owner, result["push_policy_reason"])

    def test_a_local_path_remote_is_ownership_unknown(self) -> None:
        result = self._derive([("origin", "/srv/mirrors/x.git")])
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_UNKNOWN)
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_ASK)

    def test_an_unrecognized_forge_host_asks(self) -> None:
        result = self._derive([("origin", "https://git.example.invalid/build000r/x.git")])
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_UNKNOWN)
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_ASK)

    def test_no_remote_and_no_registry_entry_is_unknown_with_no_source(self) -> None:
        result = self._derive([])
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_UNKNOWN)
        self.assertEqual(result["ownership_source"], git_estate.OWNERSHIP_SOURCE_NONE)
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_ASK)

    def test_origin_wins_over_other_remotes(self) -> None:
        result = self._derive(
            [
                ("origin", "https://github.com/build000r/x.git"),
                ("upstream", "https://github.com/tetsuo-ai/x.git"),
            ]
        )
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_OPERATOR)

    def test_the_operator_account_comes_from_the_registry_not_a_constant(self) -> None:
        # A different estate declares a different metadata.owner; the same URL
        # must then read as external.
        result = self._derive(
            [("origin", "https://github.com/build000r/x.git")], owner="someone-else"
        )
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_EXTERNAL)

    # -- conflict and forward compatibility --------------------------------

    def test_a_registry_owned_claim_over_an_external_remote_asks(self) -> None:
        # Never let a stale registry line authorize a push at somebody else's
        # upstream: the observable remote disagrees, so a human decides.
        result = self._derive(
            [("origin", "https://github.com/tetsuo-ai/x.git")], {"ownership": "owned"}
        )
        self.assertEqual(result["ownership"], git_estate.OWNERSHIP_OPERATOR)
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_ASK)
        self.assertIn("confirm", result["push_policy_reason"])

    def test_an_explicit_registry_push_policy_is_honoured(self) -> None:
        result = self._derive(
            [("origin", "https://github.com/build000r/x.git")],
            {"ownership": "owned", "push_policy": "scrub-gate"},
        )
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_SCRUB_GATE)

    def test_a_registry_scrub_gate_flag_is_honoured(self) -> None:
        result = self._derive(
            [("origin", "https://github.com/build000r/x.git")],
            {"ownership": "owned", "scrub_gate": True},
        )
        self.assertEqual(result["push_policy"], git_estate.PUSH_POLICY_SCRUB_GATE)


class PushPolicyFixGatingTests(unittest.TestCase):
    """`git push` advice is emitted for exactly one policy."""

    def _fixes(self, policy):
        record = _record(
            "/r/ahead",
            classes=frozenset({"ahead"}),
            primary_class="ahead-clean",
            ahead=3,
        )
        return git_estate.fix_commands(record, push_policy=policy)

    def test_only_the_push_policy_yields_a_push_command(self) -> None:
        self.assertEqual(self._fixes(git_estate.PUSH_POLICY_PUSH), ["git -C /r/ahead push"])

    def test_no_other_policy_ever_emits_a_push_command(self) -> None:
        for policy in (
            git_estate.PUSH_POLICY_NO_PUSH,
            git_estate.PUSH_POLICY_SCRUB_GATE,
            git_estate.PUSH_POLICY_ASK,
            None,
        ):
            with self.subTest(policy=policy):
                fixes = self._fixes(policy)
                joined = " ".join(fixes)
                self.assertNotIn("git -C /r/ahead push", joined)
                self.assertNotIn("git push", joined)
                # It still tells the coordinator how to SEE the commits.
                self.assertIn("log --oneline", joined)
                self.assertIn("3 unpublished commits", joined)

    def test_a_diverged_row_still_routes_to_reconcile_regardless_of_policy(self) -> None:
        record = _record(
            "/r/div",
            classes=frozenset({"ahead", "behind", "diverged-clean"}),
            primary_class="diverged-clean",
            ahead=1,
            behind=1,
        )
        fixes = git_estate.fix_commands(record, push_policy=git_estate.PUSH_POLICY_PUSH)
        self.assertIn("sbp doctor / reconcile skill — do not hand-merge", fixes)
        self.assertNotIn("git -C /r/div push", fixes)

    def test_singular_commit_wording(self) -> None:
        record = _record(
            "/r/one", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=1
        )
        fixes = git_estate.fix_commands(record, push_policy=git_estate.PUSH_POLICY_NO_PUSH)
        self.assertIn("1 unpublished commit;", fixes[0])


class PushPolicyMarkerTests(unittest.TestCase):
    """The tty gains a marker, never a column, and only where advice changed."""

    def _row(self, *, ahead: int, policy: str) -> dict:
        return {
            "path": "/r/x",
            "risk_band": "ahead" if ahead else "clean",
            "branch": "main",
            "staged": 0,
            "unstaged": 0,
            "untracked": 0,
            "stash_count": 0,
            "ahead": ahead,
            "behind": 0,
            "push_policy": policy,
        }

    def test_an_ahead_no_push_row_is_marked(self) -> None:
        lines = git_estate._table_lines([self._row(ahead=2, policy="no-push")], False)
        self.assertTrue(any("[no-push]" in line for line in lines))

    def test_a_pushable_row_is_not_marked(self) -> None:
        lines = git_estate._table_lines([self._row(ahead=2, policy="push")], False)
        self.assertFalse(any("[no-push]" in line or "[push]" in line for line in lines))

    def test_a_row_with_nothing_to_push_is_not_marked(self) -> None:
        # No advice would have said "push", so no badge: the table must not
        # sprout markers on repos nobody was about to publish.
        lines = git_estate._table_lines([self._row(ahead=0, policy="no-push")], False)
        self.assertFalse(any("[no-push]" in line for line in lines))

    def test_the_table_header_gains_no_column(self) -> None:
        header = git_estate._table_lines([self._row(ahead=2, policy="no-push")], False)[0]
        self.assertNotIn("OWNER", header.upper().replace("OWNERSHIP", ""))
        self.assertNotIn("POLICY", header.upper())


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
    def test_ahead_gets_push_when_the_remote_is_operator_owned(self) -> None:
        record = _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=2)
        self.assertEqual(
            git_estate.fix_commands(record, push_policy=git_estate.PUSH_POLICY_PUSH),
            ["git -C /r/ahead push"],
        )

    def test_an_omitted_push_policy_is_not_permission_to_push(self) -> None:
        # Fail-safe default: a caller that never derived a policy has not
        # established that pushing is allowed, so it does not get told to.
        record = _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=2)
        fixes = git_estate.fix_commands(record)
        self.assertNotIn("git -C /r/ahead push", fixes)
        self.assertIn("ownership unconfirmed", fixes[0])

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

    def test_junk_pile_earns_the_repo_janitor_handoff(self) -> None:
        record = _record(
            "/r/junk",
            classes=frozenset({"dirty"}),
            primary_class="dirty",
            untracked=6,
        )
        self.assertEqual(
            git_estate.fix_commands(record),
            [
                "git -C /r/junk add -p && git -C /r/junk commit",
                "git -C /r/junk status --short  # git-repo-janitor pass (6 untracked)",
            ],
        )

    def test_junk_handoff_stays_quiet_below_the_floor(self) -> None:
        record = _record(
            "/r/tidy",
            classes=frozenset({"dirty"}),
            primary_class="dirty",
            untracked=git_estate.JUNK_CANDIDATE_MIN - 1,
        )
        self.assertEqual(
            git_estate.fix_commands(record),
            ["git -C /r/tidy add -p && git -C /r/tidy commit"],
        )

    def test_junk_handoff_lands_between_commit_and_stash(self) -> None:
        record = _record(
            "/r/both",
            classes=frozenset({"dirty", "stash"}),
            primary_class="dirty",
            untracked=5,
            stash_count=2,
        )
        self.assertEqual(
            git_estate.fix_commands(record),
            [
                "git -C /r/both add -p && git -C /r/both commit",
                "git -C /r/both status --short  # git-repo-janitor pass (5 untracked)",
                "git -C /r/both stash list  # git-stash-janitor pass",
            ],
        )

    def test_blocked_row_skips_the_junk_handoff_entirely(self) -> None:
        record = _record(
            "/r/blkjunk",
            classes=frozenset({"blocked"}),
            primary_class="blocked",
            upstream=None,
            untracked=99,
            error="probe failed",
        )
        self.assertEqual(git_estate.fix_commands(record), ["inspect: probe failed"])

    def test_blocked_carries_error_and_nothing_else(self) -> None:
        record = _record(
            "/r/blk", classes=frozenset({"blocked"}), primary_class="blocked", upstream=None, error="probe died"
        )
        self.assertEqual(git_estate.fix_commands(record), ["inspect: probe died"])

    def test_dirty_and_ahead_carry_both_fixes(self) -> None:
        record = _record(
            "/r/da", classes=frozenset({"dirty", "ahead"}), primary_class="dirty", ahead=1, staged=1
        )
        fixes = git_estate.fix_commands(
            record, push_policy=git_estate.PUSH_POLICY_PUSH
        )
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
                # Hermetic joins: absent stores/scripts add NOTHING, so point
                # every external-state join at nonexistent paths -- otherwise
                # a real receipts store or reconcile-skill checkout on the
                # host leaks into fixture envelopes.
                **helpers.hermetic_join_env(self.tmp),
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
        # The fixture's remote is a local bare path, which is exactly the
        # "ownership-unknown" case the live run hit: no forge, no account to
        # compare, so the row asks instead of advising a push.
        self.assertEqual(rows[str(ahead)]["ownership"], git_estate.OWNERSHIP_UNKNOWN)
        self.assertEqual(rows[str(ahead)]["push_policy"], git_estate.PUSH_POLICY_ASK)
        self.assertEqual(
            rows[str(ahead)]["fix"],
            [
                f"git -C {ahead} log --oneline @{{u}}..HEAD  # 1 unpublished commit; "
                "ownership unconfirmed — establish intent before publishing"
            ],
        )
        self.assertNotIn(f"git -C {ahead} push", rows[str(ahead)]["fix"])
        self.assertEqual(rows[str(clean)]["fix"], [])

        # Every row carries the ownership join, on both the scanned rows and
        # cwd_repo, so a coordinator never has to re-derive it.
        for row in report["repos"]:
            self.assertIn(row["ownership"], git_estate.OWNERSHIP_VALUES)
            self.assertIn(row["push_policy"], git_estate.PUSH_POLICY_VALUES)
            self.assertIn(
                row["ownership_source"],
                (
                    git_estate.OWNERSHIP_SOURCE_REGISTRY,
                    git_estate.OWNERSHIP_SOURCE_HEURISTIC,
                    git_estate.OWNERSHIP_SOURCE_NONE,
                ),
            )
            self.assertTrue(row["push_policy_reason"])

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

    def test_located_stale_entry_is_not_advised_away(self) -> None:
        # A `located:` registry annotation means the checkout intentionally
        # lives on another box / in an Amp Orb: the remove-or-repoint advice
        # would be wrong (and for something like sand, dangerous), so the fix
        # flips to verify-there and the fields pass through additively.
        elsewhere = self.estate / "sand"
        unaccounted = self.estate / "gone-checkout"
        self.write_config_fixture(
            repos=[
                {
                    "id": "sand",
                    "path": str(elsewhere),
                    "located": "d3c",
                    "note": "important on d3c — do not remove; verify there first",
                },
                {"id": "gone", "path": str(unaccounted)},
            ]
        )

        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        by_id = {entry["id"]: entry for entry in report["stale_registered"]}
        self.assertEqual(by_id["sand"]["located"], "d3c")
        self.assertEqual(
            by_id["sand"]["note"],
            "important on d3c — do not remove; verify there first",
        )
        self.assertEqual(
            by_id["sand"]["fix"],
            [
                "lives on d3c — verify there before touching; "
                "do not remove or repoint from this machine"
            ],
        )
        # Unannotated entries keep the classic advice and gain no fields.
        self.assertNotIn("located", by_id["gone"])
        self.assertNotIn("note", by_id["gone"])
        self.assertEqual(
            by_id["gone"]["fix"],
            [f"remove or repoint the registry entry in {self.registry_yaml}"],
        )

        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn(
            "stale-registered: 2 registry entries with no repo on disk "
            "(1 located elsewhere, 1 unaccounted)",
            text,
        )
        self.assertIn(
            f"  - {elsewhere}  [located: d3c]  -> lives on d3c — verify there "
            "before touching; do not remove or repoint from this machine  "
            "(important on d3c — do not remove; verify there first)",
            text,
        )
        self.assertIn(
            f"  - {unaccounted}  -> remove or repoint the registry entry in "
            f"{self.registry_yaml}",
            text,
        )

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


class StashStoreOwnerTests(unittest.TestCase):
    """Attribution keys, owner choice and the estate total -- the correctness
    core, exercised on synthetic records so every edge is reachable."""

    @staticmethod
    def _store(path: str, store: str, *, linked: bool = False, stash: int = 0):
        git_dir = f"{store}/worktrees/{Path(path).name}" if linked else store
        return _record(
            path,
            git_dir=git_dir,
            common_dir=store,
            stash_count=stash,
            classes=frozenset({"stash"}) if stash else frozenset({"clean-current"}),
        )

    def test_unshared_stores_are_absent_from_the_map(self) -> None:
        records = [
            self._store("/r/a", "/r/a/.git", stash=3),
            self._store("/r/b", "/r/b/.git", stash=1),
        ]
        # Every ordinary repo owns its store: no entry, no marker, no change.
        self.assertEqual({}, git_estate.stash_store_owners(records))
        self.assertEqual(
            {
                "total": 4,
                "row_total": 4,
                "counted_rows": 2,
                "shared_rows": 0,
                "shared_stores": 0,
            },
            git_estate.stash_summary(records),
        )

    def test_main_worktree_owns_the_store_over_its_linked_worktrees(self) -> None:
        store = "/r/main/.git"
        records = [
            self._store("/r/wt-a", store, linked=True, stash=2),
            self._store("/r/main", store, stash=2),
            self._store("/r/wt-b", store, linked=True, stash=2),
        ]
        owners = git_estate.stash_store_owners(records)
        self.assertEqual({"/r/main", "/r/wt-a", "/r/wt-b"}, set(owners))
        self.assertEqual({"/r/main"}, set(owners.values()))
        # Row math says six; the store holds two.
        summary = git_estate.stash_summary(records)
        self.assertEqual(2, summary["total"])
        self.assertEqual(6, summary["row_total"])
        self.assertEqual(1, summary["counted_rows"])
        self.assertEqual(2, summary["shared_rows"])
        self.assertEqual(1, summary["shared_stores"])

    def test_missing_primary_falls_back_to_first_sorted_member(self) -> None:
        # The main worktree lives outside the scan roots (or a registry rule
        # ignored it): the estate must still count the store exactly once, and
        # pick the same row on every run.
        store = "/elsewhere/main/.git"
        records = [
            self._store("/r/wt-z", store, linked=True, stash=5),
            self._store("/r/wt-a", store, linked=True, stash=5),
        ]
        owners = git_estate.stash_store_owners(records)
        self.assertEqual({"/r/wt-a", "/r/wt-z"}, set(owners))
        self.assertEqual({"/r/wt-a"}, set(owners.values()))
        self.assertEqual(5, git_estate.stash_summary(records)["total"])
        # Stable regardless of input order.
        self.assertEqual(
            owners, git_estate.stash_store_owners(list(reversed(records)))
        )

    def test_symlink_alias_of_one_checkout_is_not_a_second_store(self) -> None:
        # Both rows resolve to the same store and both look like a main
        # worktree; the tiebreak keeps exactly one of them counted.
        store = "/r/real/.git"
        records = [
            self._store("/r/alias", store, stash=4),
            self._store("/r/real", store, stash=4),
        ]
        summary = git_estate.stash_summary(records)
        self.assertEqual(4, summary["total"])
        self.assertEqual(1, summary["counted_rows"])
        self.assertEqual(1, summary["shared_rows"])

    def test_unknown_store_keys_never_group_together(self) -> None:
        # Blocked probes (and a git too old for --git-common-dir) have no key.
        # Treating "unknown" as one shared store would silently drop counts.
        records = [
            _record("/r/blocked-a", common_dir=None, git_dir=None, stash_count=1),
            _record("/r/blocked-b", common_dir=None, git_dir=None, stash_count=2),
        ]
        self.assertEqual({}, git_estate.stash_store_owners(records))
        self.assertEqual(3, git_estate.stash_summary(records)["total"])

    def test_a_path_scanned_twice_does_not_share_a_store_with_itself(self) -> None:
        record = self._store("/r/solo", "/r/solo/.git", stash=2)
        self.assertEqual({}, git_estate.stash_store_owners([record, record]))
        self.assertEqual(2, git_estate.stash_summary([record, record])["total"])

    def test_renderer_tolerates_a_pre_attribution_envelope(self) -> None:
        # `sbp git --cached` replays envelopes written before this field
        # existed: no stash_summary, no stash_store_primary. Those must render
        # exactly as they always did rather than KeyError on a stale cache.
        legacy = {
            "generated_at": "2026-08-09T12:00:00+00:00",
            "roots": ["/r"],
            "repo_count": 1,
            "filters": [],
            "repos": [
                {
                    "path": "/r/solo",
                    "risk_band": "stash-only",
                    "branch": "main",
                    "ahead": 0,
                    "behind": 0,
                    "staged": 0,
                    "unstaged": 0,
                    "untracked": 0,
                    "stash_count": 3,
                    "fix": [],
                }
            ],
        }
        text = "\n".join(git_estate.report_text_lines(legacy, color=False))
        self.assertIn("      3  main    /r/solo", text)
        self.assertNotIn("[shared store:", text)
        self.assertNotIn("distinct entries", text)

    def test_attribution_never_rewrites_the_observed_count(self) -> None:
        row = {"path": "/r/wt", "stash_count": 2}
        git_estate._attribute_stash(row, "/r/main")
        # The checkout really can reach those two entries -- band, --only
        # stash and the fix handoff keep reading stash_count.
        self.assertEqual(2, row["stash_count"])
        self.assertEqual(0, row["stash_attributed"])
        self.assertEqual("/r/main", row["stash_store_primary"])

        owner = {"path": "/r/main", "stash_count": 2}
        git_estate._attribute_stash(owner, "/r/main")
        self.assertEqual(2, owner["stash_attributed"])
        self.assertEqual("/r/main", owner["stash_store_primary"])

        solo = {"path": "/r/solo", "stash_count": 7}
        git_estate._attribute_stash(solo, None)
        self.assertEqual(7, solo["stash_attributed"])
        self.assertNotIn("stash_store_primary", solo)


class SharedStoreFixtureTests(GitEstateFixtureCase):
    """Real linked worktrees / symlink aliases through ``build_report``."""

    def stash_twice(self, repo: Path) -> None:
        for i in range(2):
            (repo / "tracked.txt").write_text(f"stash {i}\n", encoding="utf-8")
            self.git(repo, "stash", "push", "-q", "-m", f"stash {i}")

    def add_worktree(self, repo: Path, name: str) -> Path:
        worktree = self.estate / name
        self.git(repo, "worktree", "add", "-q", str(worktree), "-b", name)
        return worktree

    def table_row(self, lines: list[str], path: Path) -> str:
        """The one table line whose PATH column is ``path`` (row markers and
        footer fix lines both mention paths; only a row indents its own)."""
        needle = f"  {path}"
        rows = [
            line
            for line in lines
            if line.endswith(needle) or f"{needle}  " in line
        ]
        self.assertEqual(1, len(rows), f"one table row for {path}: {rows}")
        return rows[0]

    def test_two_linked_worktrees_count_their_two_stashes_once(self) -> None:
        main = self.make_repo("a-main")
        self.stash_twice(main)
        wt_one = self.add_worktree(main, "b-wt-one")
        wt_two = self.add_worktree(main, "c-wt-two")
        self.write_config_fixture()

        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        rows = {row["path"]: row for row in report["repos"]}
        self.assertEqual({str(main), str(wt_one), str(wt_two)}, set(rows))

        # Every checkout observes the same two entries...
        for path in rows:
            self.assertEqual(2, rows[path]["stash_count"], path)
        # ...one physical store behind all three...
        self.assertEqual(
            1, len({row["common_dir"] for row in rows.values()})
        )
        # ...counted exactly once, at the main worktree.
        self.assertEqual(2, rows[str(main)]["stash_attributed"])
        self.assertEqual(0, rows[str(wt_one)]["stash_attributed"])
        self.assertEqual(0, rows[str(wt_two)]["stash_attributed"])
        for row in rows.values():
            self.assertEqual(str(main), row["stash_store_primary"])

        self.assertEqual(
            {
                "total": 2,
                "row_total": 6,
                "counted_rows": 1,
                "shared_rows": 2,
                "shared_stores": 1,
            },
            report["stash_summary"],
        )

    def test_text_table_shows_one_count_and_two_shared_markers(self) -> None:
        main = self.make_repo("a-main")
        self.stash_twice(main)
        wt_one = self.add_worktree(main, "b-wt-one")
        wt_two = self.add_worktree(main, "c-wt-two")
        self.write_config_fixture()

        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        lines = git_estate.report_text_lines(report, color=False)
        text = "\n".join(lines)

        marker = f"[shared store: {main}]"
        self.assertEqual(2, text.count(marker))
        for worktree in (wt_one, wt_two):
            row = self.table_row(lines, worktree)
            # The count is NOT duplicated into the sharer's STASH column.
            self.assertIn(marker, row)
            self.assertIn("    -  ", row)
            self.assertNotIn("    2  ", row)
        main_row = self.table_row(lines, main)
        self.assertIn("    2  ", main_row)
        self.assertNotIn(marker, main_row)

        # The STASH column now sums to the truth, and the estate header says
        # so outright so nobody has to sum it.
        self.assertIn(
            "  stash: 2 distinct entries (2 rows counted at their primary store)",
            lines,
        )

    def test_symlink_alias_root_adds_no_second_counted_row(self) -> None:
        real = self.make_repo("real-checkout")
        self.stash_twice(real)
        alias = self.tmp / "alias-checkout"
        alias.symlink_to(real)
        self.write_config_fixture()

        report = git_estate.build_report(
            roots=[str(self.estate), str(alias)], depth=2
        )
        rows = {row["path"]: row for row in report["repos"]}
        # Both views are reported (per-checkout visibility survives)...
        self.assertEqual({str(real), str(alias)}, set(rows))
        self.assertEqual(
            rows[str(real)]["common_dir"], rows[str(alias)]["common_dir"]
        )
        # ...but the alias is not a second store: two entries, counted once.
        self.assertEqual(2, report["stash_summary"]["total"])
        self.assertEqual(4, report["stash_summary"]["row_total"])
        self.assertEqual(1, report["stash_summary"]["counted_rows"])
        counted = [p for p, row in rows.items() if row["stash_attributed"]]
        self.assertEqual(1, len(counted))

    def test_ordinary_estate_is_untouched_by_attribution(self) -> None:
        stashed = self.make_repo("a-stashed")
        self.stash_twice(stashed)
        other = self.make_repo("b-other")
        (other / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture()

        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        rows = {row["path"]: row for row in report["repos"]}
        for path, row in rows.items():
            self.assertEqual(row["stash_count"], row["stash_attributed"], path)
            self.assertNotIn("stash_store_primary", row)
        self.assertEqual(2, report["stash_summary"]["total"])
        self.assertEqual(0, report["stash_summary"]["shared_rows"])

        text = "\n".join(git_estate.report_text_lines(report, color=False))
        # No shared store -> no marker, and no summary line either: the column
        # already sums to the truth.
        self.assertNotIn("[shared store:", text)
        self.assertNotIn("distinct entries", text)

    def test_shared_store_without_stashes_stays_quiet(self) -> None:
        main = self.make_repo("a-main")
        worktree = self.add_worktree(main, "b-wt")
        self.write_config_fixture()

        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        rows = {row["path"]: row for row in report["repos"]}
        # The structural fact still ships for machines...
        self.assertEqual(str(main), rows[str(worktree)]["stash_store_primary"])
        text = "\n".join(git_estate.report_text_lines(report, color=False))
        # ...but with nothing to double-count, the tty stays silent.
        self.assertNotIn("[shared store:", text)
        self.assertNotIn("distinct entries", text)

    def test_only_filter_does_not_move_the_attribution(self) -> None:
        main = self.make_repo("a-main")
        self.stash_twice(main)
        worktree = self.add_worktree(main, "b-wt")
        self.write_config_fixture()

        full = git_estate.build_report(roots=[str(self.estate)], depth=2)
        # --only stash keeps both rows; the worktree must NOT be promoted to
        # owner just because the view narrowed, and the estate total is still
        # counted over the whole ignore-filtered scan.
        filtered = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["stash"]
        )
        for report in (full, filtered):
            rows = {row["path"]: row for row in report["repos"]}
            self.assertEqual(2, rows[str(main)]["stash_attributed"])
            self.assertEqual(0, rows[str(worktree)]["stash_attributed"])
            self.assertEqual(2, report["stash_summary"]["total"])

    def test_cwd_detail_names_the_primary_when_cwd_is_a_worktree(self) -> None:
        main = self.make_repo("a-main")
        self.stash_twice(main)
        worktree = self.add_worktree(main, "b-wt")
        self.write_config_fixture()

        report = git_estate.build_report(
            roots=[str(self.estate)], depth=2, cwd=str(worktree)
        )
        self.assertEqual(str(main), report["cwd_repo"]["stash_store_primary"])
        self.assertEqual(0, report["cwd_repo"]["stash_attributed"])
        lines = git_estate.report_text_lines(report, color=False)
        # The detail block keeps the reachable count -- from here those two
        # entries really are reachable -- and says where it is counted.
        detail = next(line for line in lines if line.startswith("  stash: "))
        self.assertTrue(detail.startswith("  stash: 2 (newest "), detail)
        self.assertTrue(detail.endswith(f"  [shared store: {main}]"), detail)


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
