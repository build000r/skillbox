"""Fixture-fleet END-TO-END test for ``fleet converge`` / ``fleet relink``.

Why this is the most-tested path in sbp
=======================================

Converge and relink are the two surfaces that *mutate many repos in one
command*. Converge reports a per-repo heal PLAN (it never writes); relink owns
the machine-migration rewrite and is the one surface with a real ``--yes``
APPLY that repoints installed skill symlinks across the whole fleet. A bug in
either silently corrupts the entire skill estate, so the only safe way to prove
their apply semantics is to drive them end-to-end against a *simulated* fleet on
a throwaway tmp tree and assert the filesystem end-state. Unit tests
(``tests/test_fleet_converge.py`` / ``tests/test_fleet_relink.py``) lock the
plan *shape*; this module locks the full lifecycle:

    build  ->  plan (dry-run golden)  ->  apply (on the fixture tree ONLY)
           ->  re-audit (zero issues)  ->  idempotence (second apply is a no-op)

What the simulated two-machine tree exhibits
============================================

It is the shared :func:`tests.fixture_fleet.build_fixture_fleet` estate (an
aliased ``/srv/repos`` style root that is a symlink dir to ``repos_real``, two
machine profiles, OS + managed homes) AUGMENTED inline so every triage class is
present at once:

* **other-machine** — the fixture ``other-machine`` repo, with a machines.yaml
  injected so its ``/fake-mac-root/...`` link classifies as *foreign*. We also
  create the *devbox-side translated target* inline so ``fleet relink`` can
  REWRITE it (the migration heal). This is the only class with a real apply.
* **moved** — the SAME ``other-machine`` link, classified *without* the machine
  override, where the same-named source still lives under a current source
  root: converge surfaces it as a ``relink`` (repoint) action.
* **dangling** — the fixture ``dangling`` repo's dead link: converge ``prune``,
  healed via the ``skill prune`` lifecycle apply.
* **unreadable** — a symlink-loop target makes ``resolve()`` raise. Materializing
  one *inside* an audited repo crashes the (un-edited) beads-status path in
  ``collect_skill_visibility``, so it has NO safe in-repo apply here; we assert
  its TAXONOMY classification + converge ``prune``/``investigate`` mapping
  directly (the documented entry points) and note the gap.
* **aliased root + healthy links** — the scan root is the aliased symlink dir,
  and the ``healthy`` repo's link resolves on-box. The hard invariant: a healthy
  link is NEVER a relink/prune candidate and is left byte-identical after apply.

HARD RULE honored here: every apply runs against the tmp fixture tree the
harness builds under pytest's ``tmp_path``. Nothing ever touches the operator's
real ``/srv/skillbox/repos`` repos.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fixture_fleet import build_fixture_fleet  # noqa: E402
from runtime_manager import fleet_converge as fc  # noqa: E402
from runtime_manager import fleet_relink as fr  # noqa: E402
from runtime_manager import machines as m  # noqa: E402
from runtime_manager import skill_visibility as sv  # noqa: E402


GOLDEN_PATH = Path(__file__).resolve().parent / "goldens" / "fleet_converge_e2e.json"

# Set to "1" to (re)write the converge golden from the current run. Off by
# default so an accidental run never silently overwrites the locked plan.
REGEN_GOLDEN = os.environ.get("REGEN_FLEET_E2E_GOLDEN") == "1"


# --- machines injection (so the foreign link classifies as other-machine) ---


@contextmanager
def _inject_machines(config: m.MachinesConfig | None, machine_id: str | None) -> Iterator[None]:
    """Patch the taxonomy's machines resolution for the duration.

    Mirrors ``tests/test_fleet_relink.py``: the broken-link taxonomy resolves the
    live box's machine identity through ``_machines_classifier``; overriding it
    lets the fixture's ``/fake-mac-root`` link classify as *other-machine*
    host-independently. Always restored on exit.
    """
    sv._machines_classifier_override = lambda: (config, machine_id)  # type: ignore[attr-defined]
    try:
        yield
    finally:
        sv._machines_classifier_override = None  # type: ignore[attr-defined]


def _machines_config(fleet) -> m.MachinesConfig:
    """A two-machine config: foreign 'mac-like' + current 'devbox-like'.

    ``mac-like`` owns ``fake-mac-root`` (where the fixture's other-machine link
    points); ``devbox-like`` owns ``repos_real`` (this box). A foreign target
    under ``fake-mac-root`` translates onto ``repos_real`` via the machine table.
    """
    return m.MachinesConfig(
        machines={
            "mac-like": m.MachineProfile(
                machine_id="mac-like",
                hostnames=("mac-like",),
                repo_roots=(str(fleet.root / "fake-mac-root"),),
            ),
            "devbox-like": m.MachineProfile(
                machine_id="devbox-like",
                hostnames=("devbox-like",),
                repo_roots=(str(fleet.repos_real),),
            ),
        }
    )


def _create_devbox_relink_target(fleet) -> Path:
    """Materialize the devbox-side target the other-machine link translates onto.

    The fixture's other-machine link points at ``fake-mac-root/skills/tiny-ui``.
    The machine table translates that onto ``repos_real/skills/tiny-ui``; for
    ``fleet relink`` to REWRITE (vs. reclassify) that translated path must EXIST
    and be a valid skill dir. We create it inline rather than editing the harness.
    """
    target = fleet.repos_real / "skills" / "tiny-ui"
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        "---\nname: tiny-ui\ndescription: relink target\n---\n# tiny-ui\n",
        encoding="utf-8",
    )
    return target


# --- shared estate fixture --------------------------------------------------


@pytest.fixture
def estate(tmp_path):
    """The fixture fleet + machines config + devbox relink target, all on tmp.

    Returns a small bundle so each phase test reuses the SAME built tree.
    Everything lives under pytest's ``tmp_path`` — never the live estate.
    """
    fleet = build_fixture_fleet(tmp_path)
    config = _machines_config(fleet)
    target = _create_devbox_relink_target(fleet)

    class _Estate:
        pass

    e = _Estate()
    e.fleet = fleet
    e.config = config
    e.relink_target = target
    e.machine_id = "devbox-like"
    return e


# --- normalization (stable golden) ------------------------------------------


def _normalize(obj: Any, root: str) -> Any:
    """Replace the tmp root prefix with ``<ROOT>`` so the plan is golden-stable.

    The plan is byte-stable run-to-run for a *fixed* tree, but the tmp root
    differs every run; normalizing the one variable prefix yields a checked-in
    golden that survives across machines (mirrors the name-keyed goldens already
    in ``tests/goldens/``).
    """
    if isinstance(obj, str):
        return obj.replace(root, "<ROOT>")
    if isinstance(obj, list):
        return [_normalize(item, root) for item in obj]
    if isinstance(obj, dict):
        return {key: _normalize(value, root) for key, value in obj.items()}
    return obj


def _converge_plan(estate, *, include_clean: bool):
    fleet = estate.fleet
    with fleet._home_patched(), _inject_machines(estate.config, estate.machine_id):
        return fc.build_fleet_converge_plan(
            fleet.model(),
            cwd=str(fleet.repo("overlay-repo")),
            scan_roots=[str(fleet.aliased_root)],
            include_clean=include_clean,
            include_mcp=False,
        )


def _relink_plan(estate, *, cwd: str | None, apply: bool):
    fleet = estate.fleet
    scan_roots = None if cwd is not None else [str(fleet.aliased_root)]
    with fleet._home_patched(), _inject_machines(estate.config, estate.machine_id):
        return fr.build_relink_plan(
            fleet.model(),
            config=estate.config,
            machine_id=estate.machine_id,
            scan_roots=scan_roots,
            cwd=cwd,
            apply=apply,
        )


def _prune_apply(estate, repo_name: str, skill_name: str, *, dry_run: bool):
    """Apply (or dry-run) the ``skill prune`` lifecycle heal for one broken link.

    This is converge's ``prune`` class apply path: ``skill_lifecycle_plan`` +
    ``apply_skill_lifecycle_plan`` (the exact pair the fixture's ``apply_plan``
    helper wraps). Scoped to ``--from project`` so only the repo-local link goes.
    """
    fleet = estate.fleet
    with fleet._home_patched():
        plan = sv.skill_lifecycle_plan(
            fleet.model(),
            "prune",
            skill_name=skill_name,
            cwd=str(fleet.repo(repo_name)),
            to="project",
            categories=None,
            source=None,
        )
        return sv.apply_skill_lifecycle_plan(plan, dry_run=dry_run)


def _broken_links(estate, repo_name: str) -> list[dict[str, Any]]:
    fleet = estate.fleet
    with fleet._home_patched(), _inject_machines(estate.config, estate.machine_id):
        visibility = sv.collect_skill_visibility(
            fleet.model(),
            cwd=str(fleet.repo(repo_name)),
            include_global=False,
            include_project=True,
            include_sources=False,
        )
    return (visibility.get("issues") or {}).get("broken_project") or []


def _dump(label: str, obj: Any) -> str:
    return f"\n----- {label} -----\n{json.dumps(obj, indent=2, sort_keys=True)}"


# ===========================================================================
# Phase 1 — BUILD: the simulated tree exhibits every triage class at once.
# ===========================================================================


def test_build_exhibits_all_triage_classes(estate) -> None:
    """The built fleet shows other-machine, moved, dangling + aliased + healthy."""
    fleet = estate.fleet

    # Aliased root: the scan root is a symlink dir resolving to repos_real.
    assert fleet.aliased_root.is_symlink(), "aliased root must be a symlink dir"
    assert fleet.aliased_root.resolve() == fleet.repos_real.resolve()

    # other-machine link (with machines injected) classifies as other-machine,
    # and WITHOUT the override the SAME link is 'moved' (same-named live source).
    om = _broken_links(estate, "other-machine")
    assert len(om) == 1
    assert om[0]["origin"] == "other-machine", _dump("other-machine link", om)

    with fleet._home_patched():  # no machines override -> moved
        vis = sv.collect_skill_visibility(
            fleet.model(), cwd=str(fleet.repo("other-machine")),
            include_global=False, include_project=True, include_sources=False,
        )
    moved = (vis.get("issues") or {}).get("broken_project") or []
    assert moved and moved[0]["origin"] == "moved", _dump("moved link", moved)

    # dangling link is origin=dangling.
    dangling = _broken_links(estate, "dangling")
    assert len(dangling) == 1
    assert dangling[0]["origin"] == "dangling", _dump("dangling link", dangling)

    # healthy link resolves on-box -> NOT in broken_project at all.
    assert _broken_links(estate, "healthy") == []


def test_unreadable_class_is_plan_only_and_noted(estate) -> None:
    """unreadable: assert taxonomy + converge mapping; note the missing apply.

    A symlink-loop link (the only deterministic non-root way to make
    ``resolve()`` raise -> ``broken_reason=unreadable``) crashes the un-edited
    beads-status path inside ``collect_skill_visibility`` when materialized in an
    audited repo, so there is NO safe in-repo APPLY for this class here. We
    instead drive the documented taxonomy entry point + converge's class splitter
    on a synthetic unreadable occurrence: it maps to converge's ``prune`` family
    with an ``investigate`` (``ls -ld``) action — a diagnostic, not a heal.
    """
    occ = {
        "name": "loopy",
        "path": "/x/.claude/skills/loopy",
        "state": "broken",
        "availability": "installed",
        "broken_reason": "unreadable",
        "link_target": "/x/.claude/skills/loopy",
        "link_target_abs": "/x/.claude/skills/loopy",
    }
    classified = sv._classify_broken_link(
        occ, estate.fleet.model(), machines_config=None, machine_id=None
    )
    assert classified["origin"] == "unreadable"
    assert classified["suggested_action"] == "investigate"

    relink, prune = fc._relink_and_prune_actions([{**occ, **classified}])
    assert relink == []
    assert len(prune) == 1
    assert prune[0]["class"] == "prune"
    assert prune[0]["origin"] == "unreadable"
    # investigate is a diagnostic, NOT a heal: there is no apply path for it.
    assert prune[0]["command"].startswith("ls -ld ")
    assert fc._ORIGIN_TO_CLASS["unreadable"] == "prune"


# ===========================================================================
# Phase 2 — DRY-RUN converge plan -> golden (stable / sorted).
# ===========================================================================


def test_converge_dry_run_matches_golden(estate) -> None:
    """The converge plan over the full fleet matches a checked-in, sorted golden.

    The plan is path-normalized (tmp root -> ``<ROOT>``) so the golden is stable
    across runs/machines. On mismatch the full normalized plan is printed.
    """
    plan = _converge_plan(estate, include_clean=False)
    assert plan["dry_run"] is True, "converge is plan-only and must declare dry_run"

    normalized = _normalize(plan, str(estate.fleet.root))

    if REGEN_GOLDEN:
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(
            json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    assert GOLDEN_PATH.exists(), (
        f"golden missing: regenerate with REGEN_FLEET_E2E_GOLDEN=1\n"
        f"{_dump('current normalized plan', normalized)}"
    )
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert normalized == golden, (
        "converge plan diverged from golden"
        + _dump("EXPECTED (golden)", golden)
        + _dump("ACTUAL (normalized)", normalized)
    )


def test_converge_plan_is_sorted_and_deterministic(estate) -> None:
    """Repos sorted by path; two builds are byte-identical; in-class sorted."""
    plan_a = _converge_plan(estate, include_clean=True)
    plan_b = _converge_plan(estate, include_clean=True)
    assert json.dumps(plan_a, sort_keys=True) == json.dumps(plan_b, sort_keys=True)

    paths = [row["path"] for row in plan_a["repos"]]
    assert paths == sorted(paths), _dump("repo paths (unsorted!)", paths)
    for row in plan_a["repos"]:
        for cls, actions in row["actions"].items():
            keys = [fc._stable_action_sort_key(a) for a in actions]
            assert keys == sorted(keys), f"{Path(row['path']).name}:{cls} unsorted"


def test_converge_summary_counts_every_class(estate) -> None:
    """The fixture fleet exercises relink/prune/sync/policy; mcp key present."""
    plan = _converge_plan(estate, include_clean=False)
    by_class = plan["summary"]["by_class"]
    assert by_class["relink"] == 1   # other-machine repo
    assert by_class["prune"] == 1    # dangling repo
    assert by_class["sync"] == 1     # overlay-repo expects tiny-ui
    assert by_class["policy"] == 1   # other-machine install violates scope
    assert "mcp" in by_class
    assert plan["summary"]["candidate_repos"] == 4
    assert plan["summary"]["repos_with_plan"] == 3  # healthy is converged


# ===========================================================================
# Phase 3 — APPLY the heal ON THE FIXTURE TREE ONLY, assert filesystem end-state.
# ===========================================================================


def test_apply_heals_relink_and_prune_and_leaves_healthy_untouched(estate) -> None:
    """Apply relink (rewrite) + prune (dangling) on the tmp tree; assert FS state.

    * relink: the other-machine link is repointed at the devbox translated target
      and now RESOLVES.
    * prune: the dangling link is removed from the filesystem.
    * HEALTHY: the healthy repo's link is byte-identical (same readlink) — proving
      the apply never touches a converged link. This is the load-bearing
      invariant of any fleet-wide mutate.
    """
    fleet = estate.fleet
    relink_link = fleet.repo("other-machine") / ".claude" / "skills" / "tiny-ui"
    dangling_link = fleet.repo("dangling") / ".claude" / "skills" / "ghost"
    healthy_link = fleet.repo("healthy") / ".claude" / "skills" / "tiny-cli"

    # Pre-state snapshots.
    relink_before = os.readlink(relink_link)
    healthy_before = os.readlink(healthy_link)
    assert os.path.lexists(dangling_link)
    assert not relink_link.resolve().is_dir(), "other-machine link must start broken"
    assert healthy_link.resolve().is_dir(), "healthy link must start resolving"

    # --- relink APPLY (the rewrite class — fleet_relink --yes path) ---
    plan = _relink_plan(estate, cwd=str(fleet.repo("other-machine")), apply=True)
    assert plan["summary"]["rewrite"] == 1, _dump("relink plan", plan)
    result = fr.apply_relink_plan(plan, dry_run=False)
    assert result["summary"]["rewritten"] == 1, _dump("relink apply", result)
    assert result["summary"]["failed"] == 0

    # --- prune APPLY (the dangling class — skill prune lifecycle path) ---
    pruned = _prune_apply(estate, "dangling", "ghost", dry_run=False)
    assert pruned["summary"]["applied"] == 1, _dump("prune apply", pruned)

    # --- filesystem end-state -------------------------------------------------
    # relinked link now resolves to the devbox target.
    assert os.readlink(relink_link) == str(estate.relink_target), _dump(
        "relink end-state",
        {"readlink": os.readlink(relink_link), "before": relink_before,
         "target": str(estate.relink_target)},
    )
    assert relink_link.resolve().is_dir(), "relinked link must now resolve"

    # dangling link is gone.
    assert not os.path.lexists(dangling_link), "dangling link must be pruned"

    # HEALTHY link untouched: identical readlink, still resolves.
    assert os.readlink(healthy_link) == healthy_before, _dump(
        "healthy link CHANGED (invariant violation)",
        {"after": os.readlink(healthy_link), "before": healthy_before},
    )
    assert healthy_link.resolve().is_dir()


# ===========================================================================
# Phase 4 — RE-AUDIT: zero issues for the healed classes.
# ===========================================================================


def test_reaudit_shows_zero_issues_for_healed_classes(estate) -> None:
    """After applying both heals, the healed repos report zero broken links."""
    fleet = estate.fleet

    # Apply both heals first.
    plan = _relink_plan(estate, cwd=str(fleet.repo("other-machine")), apply=True)
    fr.apply_relink_plan(plan, dry_run=False)
    _prune_apply(estate, "dangling", "ghost", dry_run=False)

    # Per-repo re-audit: both broken classes are gone.
    assert _broken_links(estate, "other-machine") == [], _dump(
        "other-machine still broken after relink",
        _broken_links(estate, "other-machine"),
    )
    assert _broken_links(estate, "dangling") == [], _dump(
        "dangling still broken after prune", _broken_links(estate, "dangling")
    )

    # Fleet audit rollup: broken_links count drops to zero for the two healed.
    with fleet._home_patched(), _inject_machines(estate.config, estate.machine_id):
        audit = sv.collect_skill_audit(
            fleet.model(),
            cwd=None,
            scan_roots=[str(fleet.aliased_root)],
            max_depth=3,
            include_clean=False,
        )
    by_class = (audit.get("summary") or {}).get("broken_by_class") or {}
    assert by_class.get("dangling", 0) == 0, _dump("audit broken_by_class", by_class)
    assert by_class.get("other-machine", 0) == 0, _dump("audit broken_by_class", by_class)
    assert by_class.get("moved", 0) == 0, _dump("audit broken_by_class", by_class)


# ===========================================================================
# Phase 5 — IDEMPOTENCE: a second apply is a no-op.
# ===========================================================================


def test_second_apply_is_a_no_op(estate) -> None:
    """A second relink + prune apply changes nothing (the fleet is converged)."""
    fleet = estate.fleet
    relink_link = fleet.repo("other-machine") / ".claude" / "skills" / "tiny-ui"
    dangling_link = fleet.repo("dangling") / ".claude" / "skills" / "ghost"

    # First apply.
    fr.apply_relink_plan(
        _relink_plan(estate, cwd=str(fleet.repo("other-machine")), apply=True),
        dry_run=False,
    )
    _prune_apply(estate, "dangling", "ghost", dry_run=False)
    relink_after_first = os.readlink(relink_link)
    assert not os.path.lexists(dangling_link)

    # --- relink: a re-plan finds NO other-machine links (link is now healthy) ---
    replan = _relink_plan(estate, cwd=str(fleet.repo("other-machine")), apply=True)
    assert replan["summary"]["actions_total"] == 0, _dump("re-plan not empty", replan)
    # And applying that empty plan rewrites nothing.
    rereap = fr.apply_relink_plan(replan, dry_run=False)
    assert rereap["summary"]["rewritten"] == 0

    # --- prune: a re-plan finds no broken link to prune ---
    with fleet._home_patched():
        reprune_plan = sv.skill_lifecycle_plan(
            fleet.model(), "prune", skill_name="ghost",
            cwd=str(fleet.repo("dangling")), to="project", categories=None, source=None,
        )
    assert (reprune_plan.get("actions") or []) == [], _dump(
        "re-prune found actions (not idempotent)", reprune_plan.get("actions")
    )

    # Filesystem unchanged by the second pass.
    assert os.readlink(relink_link) == relink_after_first
    assert not os.path.lexists(dangling_link)


def test_relink_dry_run_equals_apply_plan(estate) -> None:
    """The plan a relink apply runs is byte-for-byte the plan a dry-run prints."""
    repo = str(estate.fleet.repo("other-machine"))
    dry = _relink_plan(estate, cwd=repo, apply=False)
    applied = _relink_plan(estate, cwd=repo, apply=True)
    assert dry["dry_run"] is True
    assert applied["dry_run"] is False
    assert {k: v for k, v in dry.items() if k != "dry_run"} == {
        k: v for k, v in applied.items() if k != "dry_run"
    }, _dump("dry vs apply diverged", {"dry": dry, "apply": applied})


def test_apply_dry_run_writes_nothing(estate) -> None:
    """A relink/prune DRY-RUN reports the heal but mutates no filesystem entry."""
    fleet = estate.fleet
    relink_link = fleet.repo("other-machine") / ".claude" / "skills" / "tiny-ui"
    dangling_link = fleet.repo("dangling") / ".claude" / "skills" / "ghost"
    relink_before = os.readlink(relink_link)

    # relink dry-run.
    plan = _relink_plan(estate, cwd=str(fleet.repo("other-machine")), apply=True)
    result = fr.apply_relink_plan(plan, dry_run=True)
    assert result["summary"]["rewritten"] == 0
    assert os.readlink(relink_link) == relink_before, "dry-run rewrote a link!"

    # prune dry-run.
    pruned = _prune_apply(estate, "dangling", "ghost", dry_run=True)
    assert pruned["summary"].get("applied", 0) == 0
    assert os.path.lexists(dangling_link), "dry-run pruned a link!"
