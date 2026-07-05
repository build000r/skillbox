"""Golden-plan test for ``sbp fleet converge`` over the fixture fleet.

``fleet converge`` walks the deduped canonical fleet and emits ONE diffable
heal PLAN: every repo's skill/MCP drift grouped into five triage classes
(relink / prune / sync / policy / mcp), each action carrying its EXACT
single-repo command. This is PLAN ONLY — it must never write.

The miniature estate from ``tests/fixture_fleet.py`` already models exactly the
shapes the plan must classify:

* ``other-machine`` repo -> a ``moved`` broken link  => ``relink`` action,
  plus the same install is a scope violation => ``policy`` action with the
  rule id.
* ``dangling`` repo -> a ``dangling`` broken link    => ``prune`` action.
* ``overlay-repo`` -> a policy-expected-but-absent skill => ``sync`` action.
* ``healthy`` repo (given an MCP config in one test) -> Claude/Codex parity
  drift => ``mcp`` actions.

Run just this surface with::

    python3 -m pytest tests/ -k "fleet or converge" -q
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


import sys

ENV_MANAGER_DIR = Path(__file__).resolve().parents[1] / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import fleet_converge as fc  # noqa: E402
from runtime_manager import cli as runtime_cli  # noqa: E402
from runtime_manager.policy_eval import _repo_override_policy  # noqa: E402
from tests.fixture_fleet import build_fixture_fleet  # noqa: E402


def _build_plan(fleet, *, include_mcp=False, include_clean=True, root_dir=None,
                declared_servers=None, cwd_repo="overlay-repo"):
    with fleet._home_patched():
        return fc.build_fleet_converge_plan(
            fleet.model(),
            cwd=str(fleet.repo(cwd_repo)),
            scan_roots=[str(fleet.aliased_root)],
            include_clean=include_clean,
            include_mcp=include_mcp,
            root_dir=root_dir,
            declared_servers=declared_servers,
        )


def _repo(plan, name_suffix):
    for row in plan["repos"]:
        if Path(row["path"]).name == name_suffix:
            return row
    raise AssertionError(f"repo {name_suffix!r} not in plan: "
                         f"{[Path(r['path']).name for r in plan['repos']]}")


def _skill_default_args(fleet, *, repos=None, category=None, skill="alpha") -> Namespace:
    return Namespace(
        skill_action="default",
        default_action="on",
        skill_name=skill,
        default_scope=None,
        default_repos=repos,
        default_category=category,
        cwd=str(fleet.repo("healthy")),
        dry_run=False,
        yes=False,
        policy_path=None,
        format="json",
        client=None,
        profile="local-all",
    )


class SkillDefaultFleetTests(unittest.TestCase):
    def test_skill_default_cohort_resolution_from_registry_ids_and_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fleet = build_fixture_fleet(tmpdir)
            with fleet._home_patched():
                targets = fc.resolve_skill_default_targets(
                    fleet.model(),
                    repo_selectors=["healthy,other-machine"],
                    category_selectors=["frontend"],
                )

        self.assertEqual(
            [target["repo_id"] for target in targets],
            ["healthy", "other-machine", "overlay-repo"],
        )
        self.assertEqual(
            {Path(target["path"]).name for target in targets},
            {"healthy", "other-machine", "overlay-repo"},
        )

    def test_skill_default_cross_repo_apply_is_idempotent_after_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fleet = build_fixture_fleet(tmpdir)
            state_root = fleet.root / "state"
            args = _skill_default_args(fleet, category="frontend", skill="tiny-ui")

            with fleet._home_patched(), mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": str(state_root)}):
                dry = runtime_cli._handle_skill_default(args, dry_run=True, model=fleet.model())
                applied = runtime_cli._handle_skill_default(args, dry_run=False, model=fleet.model())
                second = runtime_cli._handle_skill_default(args, dry_run=False, model=fleet.model())

            policy = _repo_override_policy(fleet.repo("overlay-repo"))

        self.assertTrue(dry["review"]["recorded"])
        self.assertEqual(
            [target["policy_path"] for target in dry["targets"]],
            [target["policy_path"] for target in applied["targets"]],
        )
        self.assertTrue(applied["ok"])
        self.assertTrue(applied["changed"])
        self.assertEqual(applied["changed_count"], 1)
        self.assertTrue(second["ok"])
        self.assertFalse(second["changed"])
        self.assertTrue(second["noop"])
        self.assertEqual(policy["defaults"], ["tiny-ui"])
        self.assertEqual(policy["pin_on"], ["tiny-ui"])

    def test_skill_default_cross_repo_reports_residue_from_forced_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fleet = build_fixture_fleet(tmpdir)
            state_root = fleet.root / "state"
            args = _skill_default_args(fleet, repos="healthy,overlay-repo", skill="alpha")
            overlay_policy = fleet.repo("overlay-repo") / ".skillbox" / "skill-overrides.yaml"
            original_write = runtime_cli.atomic_write_text

            def flaky_write(path, content, **kwargs):
                if Path(path) == overlay_policy:
                    raise OSError("forced failure")
                return original_write(path, content, **kwargs)

            with fleet._home_patched(), mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": str(state_root)}):
                runtime_cli._handle_skill_default(args, dry_run=True, model=fleet.model())
                with mock.patch.object(runtime_cli, "atomic_write_text", side_effect=flaky_write):
                    payload = runtime_cli._handle_skill_default(args, dry_run=False, model=fleet.model())

            healthy_policy = _repo_override_policy(fleet.repo("healthy"))
            overlay_policy_payload = _repo_override_policy(fleet.repo("overlay-repo"))

        self.assertFalse(payload["ok"])
        self.assertTrue(payload["partial_apply"])
        self.assertEqual(payload["failed"]["repo_id"], "overlay-repo")
        self.assertEqual([item["repo_id"] for item in payload["residue"]], ["healthy"])
        self.assertIn("alpha", healthy_policy["defaults"])
        self.assertNotIn("alpha", overlay_policy_payload["defaults"])


# --- plan shape -------------------------------------------------------------


def test_plan_is_dry_run_only(fixture_fleet) -> None:
    """The plan declares itself plan-only and never claims to write."""
    plan = _build_plan(fixture_fleet)
    assert plan["kind"] == "fleet-converge-plan"
    assert plan["dry_run"] is True
    assert plan["classes"] == list(fc.CONVERGE_CLASSES)


def test_fleet_is_deduped_canonical_and_sorted(fixture_fleet) -> None:
    """Repos come from the deduped candidate set, sorted by realpath."""
    plan = _build_plan(fixture_fleet)
    names = [Path(r["path"]).name for r in plan["repos"]]
    # All four fixture repos present, lexicographically sorted by full path.
    assert names == sorted(names)
    assert set(names) == {"dangling", "healthy", "other-machine", "overlay-repo"}
    paths = [r["path"] for r in plan["repos"]]
    assert paths == sorted(paths), "repos must be sorted deterministically by path"


def test_plan_is_deterministic_byte_stable(fixture_fleet) -> None:
    """Two builds over the same estate yield byte-identical JSON."""
    plan_a = _build_plan(fixture_fleet)
    plan_b = _build_plan(fixture_fleet)
    assert json.dumps(plan_a, sort_keys=True) == json.dumps(plan_b, sort_keys=True)


# --- the five triage classes ------------------------------------------------


def test_relink_action_from_moved_broken_link(fixture_fleet) -> None:
    """other-machine repo's moved link -> a relink action with an ln -sfn command."""
    fleet = fixture_fleet
    plan = _build_plan(fleet)
    row = _repo(plan, "other-machine")
    relinks = row["actions"]["relink"]
    assert len(relinks) == 1
    action = relinks[0]
    assert action["class"] == "relink"
    assert action["skill"] == "tiny-ui"
    assert action["origin"] == "moved"
    assert action["suggested_action"] == "relink"
    # Exact heal: repoint the link at the live source under a current root.
    assert action["command"].startswith("ln -sfn ")
    assert action["command"].endswith(
        f"{fleet.repo('other-machine')}/.claude/skills/tiny-ui"
    )
    assert str(fleet.skill("tiny-ui")) in action["command"]


def test_prune_action_from_dangling_broken_link(fixture_fleet) -> None:
    """dangling repo's dead link -> a prune action with an rm command."""
    fleet = fixture_fleet
    plan = _build_plan(fleet)
    row = _repo(plan, "dangling")
    prunes = row["actions"]["prune"]
    assert len(prunes) == 1
    action = prunes[0]
    assert action["class"] == "prune"
    assert action["skill"] == "ghost"
    assert action["origin"] == "dangling"
    assert action["suggested_action"] == "prune"
    assert action["command"].startswith("rm ")
    assert f"{fleet.repo('dangling')}/.claude/skills/ghost" in action["command"]


def test_sync_action_from_missing_for_cwd(fixture_fleet) -> None:
    """overlay-repo's policy-expected-but-absent skill -> a sync action + rule id."""
    fleet = fixture_fleet
    plan = _build_plan(fleet)
    row = _repo(plan, "overlay-repo")
    syncs = row["actions"]["sync"]
    assert len(syncs) == 1
    action = syncs[0]
    assert action["class"] == "sync"
    assert action["skill"] == "tiny-ui"
    # The scope rule that made the skill expected here is carried.
    assert action["scope_rule"] == "frontend-local"
    expected_cmd = (
        f"manage.py skill sync tiny-ui "
        f"--cwd {fleet.repo('overlay-repo')} --dry-run"
    )
    assert action["command"] == expected_cmd


def test_policy_action_carries_rule_id_and_both_heals(fixture_fleet) -> None:
    """other-machine's scope violation -> a policy action WITH the rule id."""
    fleet = fixture_fleet
    plan = _build_plan(fleet)
    row = _repo(plan, "other-machine")
    policies = row["actions"]["policy"]
    assert len(policies) == 1
    action = policies[0]
    assert action["class"] == "policy"
    assert action["skill"] == "tiny-ui"
    # The rule id is the whole point of a policy-edit suggestion.
    assert action["scope_rule"] == "frontend-local"
    assert action["scope_policy_path"].endswith("skill-scope.yaml")
    # Default heal: prune the violating install (dry-run).
    expected_cmd = (
        f"manage.py skill prune --cwd {fleet.repo('other-machine')} "
        f"--from project --dry-run"
    )
    assert action["command"] == expected_cmd
    # Alternative heal: edit the named rule to permit the install.
    assert "frontend-local" in action["policy_edit"]
    assert "skill-scope.yaml" in action["policy_edit"]


def test_mcp_parity_actions_classify_drift(fixture_fleet) -> None:
    """A repo with Claude/Codex parity drift -> mcp actions with mcp sync command."""
    fleet = fixture_fleet
    healthy = fleet.repo("healthy")
    # Claude declares foo, Codex declares bar -> each is single-surface drift.
    (healthy / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"foo": {"command": "x"}}}), encoding="utf-8"
    )
    (healthy / ".codex").mkdir(exist_ok=True)
    (healthy / ".codex" / "config.toml").write_text(
        '[mcp_servers.bar]\ncommand = "y"\n', encoding="utf-8"
    )
    # declared_servers=[] and a root_dir distinct from the repo so neither foo
    # nor bar is "declared": both are unexplained drift.
    plan = _build_plan(
        fleet,
        include_mcp=True,
        root_dir=fleet.config_root,  # distinct from any fixture repo
        declared_servers=[],
        cwd_repo="healthy",
    )
    row = _repo(plan, "healthy")
    mcp_actions = row["actions"]["mcp"]
    by_surface = {(a["surface"], a["server"]): a for a in mcp_actions}
    assert ("claude", "foo") in by_surface
    assert ("codex", "bar") in by_surface
    foo = by_surface[("claude", "foo")]
    assert foo["class"] == "mcp"
    assert foo["kind"] == "unexpected"
    assert "mcp sync" in foo["command"]
    assert str(healthy) in foo["command"]


def test_all_five_classes_present_in_summary(fixture_fleet) -> None:
    """The fixture fleet exercises every non-mcp class; summary rolls them up."""
    plan = _build_plan(fixture_fleet)
    by_class = plan["summary"]["by_class"]
    assert by_class["relink"] == 1
    assert by_class["prune"] == 1
    assert by_class["sync"] == 1
    assert by_class["policy"] == 1
    # mcp is 0 here (no MCP configs in this build) but the key is always present.
    assert "mcp" in by_class
    assert plan["summary"]["actions_total"] == 4
    assert plan["summary"]["candidate_repos"] == 4
    assert plan["summary"]["repos_with_plan"] == 3  # healthy is converged


# --- determinism + filtering ------------------------------------------------


def test_include_clean_false_drops_converged_repos(fixture_fleet) -> None:
    """Without --all, the converged 'healthy' repo is omitted from the plan."""
    with fixture_fleet._home_patched():
        plan = fc.build_fleet_converge_plan(
            fixture_fleet.model(),
            cwd=str(fixture_fleet.repo("overlay-repo")),
            scan_roots=[str(fixture_fleet.aliased_root)],
            include_clean=False,
            include_mcp=False,
        )
    names = {Path(r["path"]).name for r in plan["repos"]}
    assert "healthy" not in names
    assert names == {"dangling", "other-machine", "overlay-repo"}
    # The candidate count still reflects the full deduped fleet.
    assert plan["summary"]["candidate_repos"] == 4


def test_in_class_actions_are_sorted(fixture_fleet) -> None:
    """Within a class, actions sort by (skill/server, path) for stable diffs."""
    fleet = fixture_fleet
    plan = _build_plan(fleet)
    for row in plan["repos"]:
        for cls, actions in row["actions"].items():
            keys = [fc._stable_action_sort_key(a) for a in actions]
            assert keys == sorted(keys), f"{Path(row['path']).name}:{cls} unsorted"


# --- text renderer ----------------------------------------------------------


def test_text_renderer_is_stable_and_labels_classes(fixture_fleet) -> None:
    """The human table renders headers, per-class sections, and commands."""
    plan = _build_plan(fixture_fleet)
    lines = fc.fleet_converge_text_lines(plan, limit=0)
    text = "\n".join(lines)
    assert "fleet converge plan (DRY-RUN" in text
    assert "by_class:" in text
    # A per-repo section header and at least one command line.
    assert any(line.startswith("## ") for line in lines)
    assert any("$ ln -sfn" in line for line in lines)
    assert any("$ rm " in line for line in lines)
    # Policy action surfaces the alternative rule-edit heal.
    assert any("or: edit rule 'frontend-local'" in line for line in lines)
    # Rendering twice is byte-stable.
    assert lines == fc.fleet_converge_text_lines(plan, limit=0)


# --- BUG 3 regression: emitted commands shell-quote paths/names -------------

import shlex as _shlex  # noqa: E402


def test_sync_command_quotes_repo_path_and_skill_name_with_space() -> None:
    """A repo path / skill name with a space yields a properly-quoted command.

    ``skill sync`` commands are advertised as the EXACT single-repo command, so a
    path or name carrying a space (or shell metachar) must paste safely.
    """
    actions = fc._sync_actions(
        [{"name": "my skill", "scope_rule": "r1"}],
        "/srv/repo with space",
    )
    command = actions[0]["command"]
    tokens = _shlex.split(command)
    # manage.py skill sync '<name>' --cwd '<path>' --dry-run -> the lexer must
    # re-assemble the space-bearing name and path as single tokens.
    assert "my skill" in tokens
    assert "/srv/repo with space" in tokens
    assert tokens[-1] == "--dry-run"
    assert "'" in command, "space-bearing args must be quoted in the command"


def test_policy_prune_command_quotes_repo_path_with_space() -> None:
    """The dangerous ``skill prune`` command quotes the repo path (a space-safe rm)."""
    actions = fc._policy_actions(
        [{"name": "x", "scope_rule": "r1", "allowed_paths": [], "path": "/p"}],
        "/srv/repo with space",
    )
    command = actions[0]["command"]
    tokens = _shlex.split(command)
    assert "/srv/repo with space" in tokens
    assert "--cwd" in tokens
    assert "'" in command


def test_mcp_sync_command_quotes_repo_path_with_space() -> None:
    """The ``mcp sync`` command also quotes the repo path."""
    payload = {"surfaces": {"claude": {"missing": ["foo"]}, "codex": {}}}
    actions = fc._mcp_actions(payload, "/srv/repo with space")
    command = actions[0]["command"]
    tokens = _shlex.split(command)
    assert "/srv/repo with space" in tokens
    assert "'" in command
