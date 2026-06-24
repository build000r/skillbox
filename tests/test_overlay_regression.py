"""Durable regression suite for overlay-activate semantics (the 35-vs-0 case).

Why this file exists
====================

repos-sbp-overlay-semantics-vq0.1 fixed a real production bug: activating a
marketing overlay on a repo whose cwd the policy does NOT match used to
literal-link every overlay-tagged skill (~35 of them) instead of linking what a
policy-evaluated ``skill sync`` (with the overlay forced active) would link --
which for a non-matching cwd is ZERO. ``activate_overlay_scoped_skills`` now
runs the SAME policy evaluation as ``skill sync`` with ``SKILLBOX_OVERLAYS``
forced for the call only, narrowed to the given cwd.

This suite is the durable proof that the fix stays dead. It builds, on top of
the fixture-fleet harness (``tests/fixture_fleet.py``), a marketing-style
overlay with BOTH (a) a category-scoped rule and (b) a broad path-scoped rule,
plus a bulk path-scoped rule carrying 35 literal skills, and exercises three
distinct cwds:

* ``non_matching`` -- a repo under NEITHER the category nor any path rule:
  activate links 0 (NOT the full literal overlay set).
* ``broad_only``   -- the ``healthy`` repo, under the broad path rule only:
  activate links exactly the broad-path subset (1 skill), NOT all ~37.
* ``full_match``   -- the ``overlay-repo`` (frontend category + bulk path +
  broad path): activate links the full policy-evaluated set.

In every case the invariant asserted is the SAME: activate's planned links are
IDENTICAL to what a policy-evaluated ``skill sync`` (overlay forced) plans for
that cwd -- never the literal overlay set unless policy agrees.

This builds on -- and does not duplicate -- the unit-level vq0.1 regression
``test_overlay_activate_is_policy_evaluated_not_literal_link`` in
``tests/test_cli_units.py`` (a single all-matching path rule). Here the point is
the *divergence* across category vs broad vs non-matching scopes on the shared
fixture-fleet estate, plus the dry-run==apply contract, the overlay on/off
round-trip, and a golden for the activation-packet shape.

TESTS ONLY -- this file never imports or edits runtime code beyond calling the
public runtime API.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import skill_visibility as sv  # noqa: E402

GOLDEN_PATH = ROOT_DIR / "tests" / "goldens" / "overlay_activation_packet.json"

BULK_SKILL_NAMES = [f"mk{i:02d}" for i in range(35)]
CATEGORY_SKILL = "cat-mk"
BROAD_SKILL = "broad-mk"
ALL_OVERLAY_SKILLS = sorted([*BULK_SKILL_NAMES, CATEGORY_SKILL, BROAD_SKILL])


# --------------------------------------------------------------------------- #
# Scenario builder: a marketing overlay with category + broad + bulk rules.    #
# --------------------------------------------------------------------------- #


def _write_marketing_skill(src_root: Path, name: str) -> Path:
    skill_dir = src_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: marketing fixture skill {name}.\n---\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


class MarketingScenario:
    """A marketing overlay scenario layered onto a fixture-fleet estate.

    Reuses the fleet's clients-root, config-root, home, and repos, but writes a
    *fresh* ``skill-scope.yaml`` carrying the marketing overlay rules and a
    dedicated source root with all 37 marketing skills. Three cwds are exposed:

    * ``non_matching`` -- under no rule path or category.
    * ``broad_only``   -- the ``healthy`` repo (broad path rule only).
    * ``full_match``   -- the ``overlay-repo`` (category + bulk + broad).
    """

    def __init__(self, fleet) -> None:
        self.fleet = fleet
        root = fleet.root
        self.src_root = root / "marketing-skills"
        for name in ALL_OVERLAY_SKILLS:
            _write_marketing_skill(self.src_root, name)

        # full_match: the fixture frontend-category repo.
        self.full_match = fleet.repo("overlay-repo")
        # broad_only: the healthy repo -- under the broad repos root, but NOT
        # the frontend category and NOT the bulk path.
        self.broad_only = fleet.repo("healthy")
        # broad path root: the real repos root that contains every fixture repo.
        self.broad_root = fleet.repos_real
        # non_matching: a repo outside every rule path/category.
        self.non_matching = root / "elsewhere" / "backend"
        self.non_matching.mkdir(parents=True, exist_ok=True)

        bulk_block = "\n".join(f"      - {name}" for name in BULK_SKILL_NAMES)
        fleet.skill_scope_path.write_text(
            "version: 1\n"
            "skill_source_roots:\n"
            f"  - {self.src_root}\n"
            "project_categories:\n"
            "  frontend:\n"
            f"    paths: [{self.full_match}]\n"
            "rules:\n"
            # (a) category-scoped overlay rule.
            "  - id: marketing-category\n"
            "    overlay: marketing\n"
            "    categories: [frontend]\n"
            "    default: on\n"
            "    skills:\n"
            f"      - {CATEGORY_SKILL}\n"
            # (b) broad path-scoped overlay rule (matches all fixture repos).
            "  - id: marketing-broad\n"
            "    overlay: marketing\n"
            f"    paths: [{self.broad_root}]\n"
            "    default: on\n"
            "    skills:\n"
            f"      - {BROAD_SKILL}\n"
            # bulk: 35 literal skills scoped to the frontend repo only.
            "  - id: marketing-bulk\n"
            "    overlay: marketing\n"
            f"    paths: [{self.full_match}]\n"
            "    default: on\n"
            "    skills:\n"
            f"{bulk_block}\n",
            encoding="utf-8",
        )

    @property
    def model(self) -> dict:
        return {
            "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(self.fleet.clients_root)},
            "clients": [],
            "skills": [],
        }

    # -- plan extraction helpers ------------------------------------------- #
    def activate_plan(self, cwd: Path, *, dry_run: bool = True) -> dict[str, list[str]]:
        """Return {skill: sorted link destinations} from activate, home-patched."""
        with self.fleet._home_patched():
            activations = sv.activate_overlay_scoped_skills(
                self.model, "marketing", str(cwd), to="project", dry_run=dry_run
            )
        return _link_destinations_by_skill(activations)

    def sync_forced_plan(self, cwd: Path, *, dry_run: bool = True) -> dict[str, list[str]]:
        """Return {skill: sorted link destinations} from a policy-evaluated
        ``skill sync`` with the marketing overlay forced active for the call.

        This is the *independent* reference implementation of what activate is
        supposed to do -- forcing ``SKILLBOX_OVERLAYS`` ourselves and running
        ``skill_lifecycle_plan('sync', ...)`` directly -- so the equality
        assertion is meaningful (not activate compared against itself).
        """
        prior = os.environ.get(sv.OVERLAY_ENV_VAR)
        os.environ[sv.OVERLAY_ENV_VAR] = "marketing"
        try:
            with self.fleet._home_patched():
                plan = sv.skill_lifecycle_plan(
                    self.model,
                    "sync",
                    skill_name=None,
                    cwd=str(cwd),
                    to="project",
                    categories=[],
                )
                plan = sv.apply_skill_lifecycle_plan(plan, dry_run=dry_run)
        finally:
            if prior is None:
                os.environ.pop(sv.OVERLAY_ENV_VAR, None)
            else:
                os.environ[sv.OVERLAY_ENV_VAR] = prior
        return _plan_link_destinations_by_skill(plan)


def _link_destinations_by_skill(activations: list[dict]) -> dict[str, list[str]]:
    return {
        str(activation["skill"]): sorted(
            str(action.get("destination"))
            for action in (activation.get("actions") or [])
            if action.get("op") == "link"
        )
        for activation in activations
    }


def _plan_link_destinations_by_skill(plan: dict) -> dict[str, list[str]]:
    by_skill: dict[str, list[str]] = {}
    for action in plan.get("actions") or []:
        if action.get("op") != "link":
            continue
        skill = str(action.get("skill") or "")
        if not skill:
            continue
        by_skill.setdefault(skill, []).append(str(action.get("destination")))
    return {skill: sorted(dests) for skill, dests in by_skill.items()}


def _render_side_by_side(
    label_left: str,
    left: dict[str, list[str]],
    label_right: str,
    right: dict[str, list[str]],
) -> str:
    """Render the two plans SIDE BY SIDE for great failure logging.

    Explicit acceptance criterion: on mismatch the test prints both the
    activate plan and the sync plan together so the divergence is obvious in
    the pytest output (which skill is extra/missing, and where it would link).
    """
    skills = sorted(set(left) | set(right))
    width = max([len(label_left), *[len(s) for s in skills], 10]) + 2
    lines = [
        "",
        "overlay-activate vs policy-evaluated skill-sync plan divergence:",
        f"  {'skill'.ljust(width)} | {label_left.ljust(40)} | {label_right}",
        f"  {'-' * width} | {'-' * 40} | {'-' * 40}",
    ]
    for skill in skills:
        in_left = skill in left
        in_right = skill in right
        marker = "  " if (in_left and in_right and left[skill] == right[skill]) else ">>"
        left_cell = ("links=%d" % len(left[skill])) if in_left else "(absent)"
        right_cell = ("links=%d" % len(right[skill])) if in_right else "(absent)"
        lines.append(f"{marker}{skill.ljust(width)} | {left_cell.ljust(40)} | {right_cell}")
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    if only_left:
        lines.append(f"  only in {label_left}: {only_left}")
    if only_right:
        lines.append(f"  only in {label_right}: {only_right}")
    return "\n".join(lines)


def _assert_plans_identical(scenario_label: str, activate: dict, sync: dict) -> None:
    if activate != sync:
        pytest.fail(
            f"[{scenario_label}] activate plan diverged from policy-evaluated sync plan."
            + _render_side_by_side("activate", activate, "skill-sync(forced)", sync)
        )


@pytest.fixture
def scenario(fixture_fleet) -> MarketingScenario:
    return MarketingScenario(fixture_fleet)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_overlay_declares_many_literal_skills(scenario: MarketingScenario) -> None:
    """Sanity anchor: the marketing overlay literally declares 37 skills.

    The pre-fix activate path would have linked ALL of these into ANY cwd. The
    following tests prove the post-fix path links only the policy-evaluated
    subset per cwd.
    """
    literal = sv.overlay_scoped_skill_names(scenario.model, "marketing")
    assert literal == set(ALL_OVERLAY_SKILLS)
    assert len(literal) == 37


def test_non_matching_cwd_links_zero_not_the_literal_overlay_set(
    scenario: MarketingScenario,
) -> None:
    """The 35-vs-0 example-app case: a cwd matching NO rule links ZERO.

    activate's plan must equal the policy-evaluated sync plan (both empty),
    NOT the full literal overlay set.
    """
    activate = scenario.activate_plan(scenario.non_matching, dry_run=True)
    sync = scenario.sync_forced_plan(scenario.non_matching, dry_run=True)

    _assert_plans_identical("non_matching", activate, sync)
    # Post-fix the policy-correct set is empty -- never the ~37 literal skills.
    assert activate == {}
    assert sync == {}
    # And nothing was created on disk by the dry-run.
    assert not (scenario.non_matching / ".claude" / "skills").exists()


def test_force_cannot_link_the_literal_overlay_set_in_a_non_matching_cwd(
    scenario: MarketingScenario,
) -> None:
    """No silent over-linking path: ``--force`` is NOT a literal-pack backdoor.

    Decision for repos-sbp-overlay-semantics-vq0.2: the literal-pack activation
    path was REMOVED (vq0.1 made activate policy-evaluated); no replacement flag
    was added. ``--force`` is the only remaining knob on activate, and it only
    affects link-conflict / global-block resolution inside the plan -- it does
    NOT widen the skill universe, which is derived from the cwd-narrowed
    ``missing_for_cwd`` set. So forcing activation in a cwd the policy does not
    match STILL links zero, never the ~37 literal overlay skills. This is the
    durable proof that the only way to link the full literal set is for the
    policy to genuinely match the cwd (the full_match test) -- there is no
    explicit-flag or force escape hatch.
    """
    with scenario.fleet._home_patched():
        forced = sv.activate_overlay_scoped_skills(
            scenario.model,
            "marketing",
            str(scenario.non_matching),
            to="project",
            dry_run=True,
            force=True,
        )
    # force changes nothing about scope selection: zero, not the literal set.
    assert forced == []
    # And dry-run with force still creates nothing on disk.
    assert not (scenario.non_matching / ".claude" / "skills").exists()


def test_broad_only_cwd_links_exactly_the_broad_subset(
    scenario: MarketingScenario,
) -> None:
    """A cwd under ONLY the broad path rule links exactly that subset.

    ``healthy`` is under the broad repos root (broad-mk) but is NOT the frontend
    category (cat-mk) and NOT the bulk path. Activate must link exactly
    {broad-mk}, identical to the policy-evaluated sync plan -- NOT all 37.
    """
    activate = scenario.activate_plan(scenario.broad_only, dry_run=True)
    sync = scenario.sync_forced_plan(scenario.broad_only, dry_run=True)

    _assert_plans_identical("broad_only", activate, sync)
    assert set(activate) == {BROAD_SKILL}
    # Each policy-correct skill links one destination per agent surface.
    assert len(activate[BROAD_SKILL]) == 2
    # Categorically NOT the full literal overlay set.
    assert set(activate) != set(ALL_OVERLAY_SKILLS)


def test_full_match_cwd_links_the_full_policy_evaluated_set(
    scenario: MarketingScenario,
) -> None:
    """The frontend repo matches category + bulk + broad rules.

    Here the policy-evaluated set happens to be the full 37 skills -- but the
    point is activate STILL equals the policy-evaluated sync plan, derived from
    policy, not from a literal-link shortcut.
    """
    activate = scenario.activate_plan(scenario.full_match, dry_run=True)
    sync = scenario.sync_forced_plan(scenario.full_match, dry_run=True)

    _assert_plans_identical("full_match", activate, sync)
    assert set(activate) == set(ALL_OVERLAY_SKILLS)


def test_dry_run_plan_equals_apply_plan(scenario: MarketingScenario) -> None:
    """The dry-run output is the contract: it equals the apply plan exactly.

    Asserted on the broad-only cwd (a non-trivial, partial subset) so the
    equality is meaningful, then confirmed by the on-disk symlinks apply created
    matching the dry-run's planned destinations.
    """
    dry = scenario.activate_plan(scenario.broad_only, dry_run=True)
    applied = scenario.activate_plan(scenario.broad_only, dry_run=False)

    assert dry == applied
    # apply created exactly the planned MARKETING links -- no more, no fewer.
    # (The fixture pre-seeds unrelated links like tiny-cli, so scope the
    # equality to the overlay's skill universe.)
    created = sorted(
        p.name for p in (scenario.broad_only / ".claude" / "skills").iterdir()
        if p.is_symlink()
    )
    created_marketing = {name for name in created if name in set(ALL_OVERLAY_SKILLS)}
    assert created_marketing == {BROAD_SKILL}
    for surface in ("claude", "codex"):
        link = scenario.broad_only / f".{surface}" / "skills" / BROAD_SKILL
        assert link.is_symlink()
        assert link.resolve() == (scenario.src_root / BROAD_SKILL).resolve()


def test_dry_run_plan_equals_apply_plan_for_full_match(
    scenario: MarketingScenario,
) -> None:
    """Same dry-run==apply contract on the full-match cwd (the 37-skill set)."""
    dry = scenario.activate_plan(scenario.full_match, dry_run=True)
    applied = scenario.activate_plan(scenario.full_match, dry_run=False)
    assert dry == applied
    created = sorted(
        p.name for p in (scenario.full_match / ".claude" / "skills").iterdir()
        if p.is_symlink()
    )
    # Scope to the overlay's skill universe -- the fixture pre-seeds an unrelated
    # tiny-marketing link on overlay-repo that is not part of this overlay.
    created_marketing = {name for name in created if name in set(ALL_OVERLAY_SKILLS)}
    assert created_marketing == set(ALL_OVERLAY_SKILLS)


def test_activation_is_ephemeral_no_overlay_state_persists(
    scenario: MarketingScenario,
) -> None:
    """Forcing the overlay active for the call must not leak SKILLBOX_OVERLAYS."""
    prior = os.environ.get(sv.OVERLAY_ENV_VAR)
    scenario.activate_plan(scenario.full_match, dry_run=True)
    assert os.environ.get(sv.OVERLAY_ENV_VAR) == prior


def test_overlay_round_trip_on_sync_off_restores_pre_state(
    scenario: MarketingScenario, tmp_path
) -> None:
    """Round-trip: overlay ON -> sync -> overlay OFF restores the pre-state.

    Pre-state = no marketing links in the broad-only repo. After ON + sync the
    links exist; after OFF (the default unlink path) they are gone and the
    overlay is inactive again -- a clean restore.
    """
    state_path = tmp_path / "overlay-state"
    claude_link = scenario.broad_only / ".claude" / "skills" / BROAD_SKILL
    codex_link = scenario.broad_only / ".codex" / "skills" / BROAD_SKILL

    # Pre-state: no marketing links.
    assert not claude_link.exists()
    assert not codex_link.exists()

    from unittest import mock

    with (
        scenario.fleet._home_patched(),
        mock.patch.dict(os.environ, {"SKILLBOX_OVERLAY_STATE": str(state_path)}, clear=False),
    ):
        # ON.
        sv.set_overlay("marketing", True)
        assert "marketing" in sv.active_overlays()

        # sync (apply) with the overlay forced for the call.
        sv.activate_overlay_scoped_skills(
            scenario.model, "marketing", str(scenario.broad_only), to="project", dry_run=False
        )
        assert claude_link.is_symlink()
        assert codex_link.is_symlink()

        # OFF (default = unlink overlay-scoped links, then clear state).
        removed = sv.unlink_overlay_scoped_skills(
            scenario.model, "marketing", scenario.broad_only, scope="project"
        )
        sv.set_overlay("marketing", False)

        assert sorted(Path(r).name for r in removed) == [BROAD_SKILL, BROAD_SKILL]
        assert "marketing" not in sv.active_overlays()

    # Pre-state restored: links gone.
    assert not claude_link.exists()
    assert not codex_link.exists()


def test_overlay_off_keep_preserves_the_links(
    scenario: MarketingScenario, tmp_path
) -> None:
    """``overlay off --keep`` clears overlay state but KEEPS the symlinks.

    The ``--keep`` path simply does NOT call unlink_overlay_scoped_skills, so
    the links survive even though the overlay is toggled off.
    """
    state_path = tmp_path / "overlay-state"
    claude_link = scenario.broad_only / ".claude" / "skills" / BROAD_SKILL
    codex_link = scenario.broad_only / ".codex" / "skills" / BROAD_SKILL

    from unittest import mock

    with (
        scenario.fleet._home_patched(),
        mock.patch.dict(os.environ, {"SKILLBOX_OVERLAY_STATE": str(state_path)}, clear=False),
    ):
        sv.set_overlay("marketing", True)
        sv.activate_overlay_scoped_skills(
            scenario.model, "marketing", str(scenario.broad_only), to="project", dry_run=False
        )
        assert claude_link.is_symlink()
        assert codex_link.is_symlink()

        # off --keep: turn the overlay off but DO NOT unlink.
        sv.set_overlay("marketing", False)
        assert "marketing" not in sv.active_overlays()

    # Links survive the toggle-off because --keep skips the unlink.
    assert claude_link.is_symlink()
    assert codex_link.is_symlink()


def test_activation_packet_shape_matches_golden(scenario: MarketingScenario) -> None:
    """Golden for the activation-packet shape on the broad-only skill.

    Pins the structural contract (entry keys, packet keys, surfaces, per-skill
    summary, packet name/bucket, content sha) without pinning tmp-tree absolute
    paths. ``activate`` returns a usable packet (SKILL.md + sha) so the
    requesting agent can use the skill immediately while the symlinks make it
    visible to future sessions.
    """
    import hashlib

    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    with scenario.fleet._home_patched():
        activations = sv.activate_overlay_scoped_skills(
            scenario.model, "marketing", str(scenario.broad_only), to="project", dry_run=True
        )
    assert len(activations) == 1
    entry = activations[0]

    assert sorted(entry.keys()) == golden["entry_keys"]
    assert entry["skill"] == golden["skill"]
    assert entry["summary"] == golden["summary"]
    assert entry["warnings"] == golden["warnings"]

    packet = entry["activation_packet"]
    assert sorted(packet.keys()) == golden["activation_packet_keys"]
    assert packet["name"] == golden["packet_name"]
    assert packet["source_bucket"] == golden["source_bucket"]
    assert packet["skill_md"] == golden["skill_md"]
    # sha is content-only (independent of tmp paths) -> stable to pin.
    assert packet["skill_md_sha256"] == hashlib.sha256(
        golden["skill_md"].encode("utf-8")
    ).hexdigest()

    # Both agent surfaces, one link each (paths themselves are tmp-tree-relative).
    assert sorted(packet["surface_targets"]) == golden["surfaces"]
    for surface in golden["surfaces"]:
        targets = packet["surface_targets"][surface]
        assert len(targets) == golden["links_per_surface"]
        assert targets[0].endswith(f"/.{surface}/skills/{BROAD_SKILL}")


def test_side_by_side_failure_logging_renders_both_plans() -> None:
    """The side-by-side renderer is itself covered.

    Feeding it a deliberate divergence (the literal-link regression: activate
    linking all 37 while sync links 0) must surface BOTH plans and call out the
    extra skills, so a future regression produces a great failure log.
    """
    literal_all = {name: ["/x/a", "/x/b"] for name in ALL_OVERLAY_SKILLS}
    policy_zero: dict[str, list[str]] = {}

    rendered = _render_side_by_side(
        "activate", literal_all, "skill-sync(forced)", policy_zero
    )

    assert "overlay-activate vs policy-evaluated skill-sync plan divergence:" in rendered
    assert "activate" in rendered and "skill-sync(forced)" in rendered
    # Every literal skill shows up as present-in-activate, absent-in-sync.
    for name in ALL_OVERLAY_SKILLS:
        assert name in rendered
    assert "only in activate:" in rendered
    assert "(absent)" in rendered
    # The divergence marker flags every row.
    assert ">>" in rendered
