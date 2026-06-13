"""Flip-links-narrowed-set proof for the four mode-pack overlays (oh1.2).

repos-sbp-policy-estate-oh1.2 declares four mode-pack overlays -- ``swarm``,
``research``, ``hardening``, ``operator-maintenance`` -- each delivered by a
single overlay-tagged rule scoped to the broad devbox roots. The contract this
suite proves, per pack:

* with the pack's overlay FORCED ACTIVE, a policy-evaluated ``skill sync`` in a
  cwd UNDER the rule's broad roots proposes EXACTLY the pack's narrowed skill set
  (no more, no fewer) -- the whole mode set turns on together;
* in a cwd OUTSIDE the rule's roots, the same forced sync proposes ZERO from the
  pack (overlays never literal-link a pack into a non-matching cwd -- the vq0.1
  semantics the overlay-regression suite locks);
* with the overlay OFF, the pack proposes ZERO even in a matching cwd (the
  overlay is the gate);
* ``overlay_scoped_skill_names`` returns exactly the pack's literal skills.

It mirrors ``test_overlay_regression.py``'s ``MarketingScenario`` (broad +
category rules over the fixture-fleet estate) but for the four cross-repo mode
packs, which are broad-roots-only (no category): the point here is the
per-overlay NARROWING -- each flip lights its OWN set and only in a matching cwd.

TESTS ONLY -- never imports or edits runtime code beyond the public API.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import skill_visibility as sv  # noqa: E402


# The exact mode-pack sets from the bead (the NON-global delta -- swarm omits
# always-global divide-and-conquer per the precedence rule).
PACK_SKILLS: dict[str, list[str]] = {
    "swarm": ["dueling-idea-wizards", "ntm", "vibing-with-ntm"],
    "research": ["wiki", "wiki-duel", "wiki-forge", "cass-memory"],
    "hardening": [
        "multi-pass-bug-hunting",
        "profiling-software-performance",
        "extreme-software-optimization",
        "simplify-and-refactor-code-isomorphically",
        "testing-conformance-harnesses",
        "testing-fuzzing",
        "testing-golden-artifacts",
        "testing-metamorphic",
    ],
    "operator-maintenance": [
        "skill-registry-usage-audit",
        "mmdx-registry-usage-audit",
        "system-performance-remediation",
        "codebase-pattern-extraction",
        "codebase-archaeology",
    ],
}
ALL_PACK_SKILLS = sorted({s for skills in PACK_SKILLS.values() for s in skills})


def _write_skill(src_root: Path, name: str) -> None:
    skill_dir = src_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: mode-pack fixture skill {name}.\n---\n# {name}\n",
        encoding="utf-8",
    )


class ModePackScenario:
    """Four mode-pack overlays layered onto a fixture-fleet estate.

    Writes a fresh ``skill-scope.yaml`` carrying one overlay-tagged rule per pack,
    each scoped to the fixture's broad repos root (the analogue of the real
    ``/srv/skillbox/repos`` devbox roots), plus a dedicated source root holding
    every pack skill. Exposes a ``matching`` cwd (under the broad root) and a
    ``non_matching`` cwd (outside every rule path).
    """

    def __init__(self, fleet) -> None:
        self.fleet = fleet
        root = fleet.root
        self.src_root = root / "mode-pack-skills"
        for name in ALL_PACK_SKILLS:
            _write_skill(self.src_root, name)

        # matching: a real fixture repo under the broad repos root.
        self.matching = fleet.repo("healthy")
        self.broad_root = fleet.repos_real
        # non_matching: a repo outside every rule path.
        self.non_matching = root / "elsewhere" / "backend"
        self.non_matching.mkdir(parents=True, exist_ok=True)

        rule_blocks = []
        for overlay, skills in PACK_SKILLS.items():
            skill_lines = "\n".join(f"      - {s}" for s in skills)
            rule_blocks.append(
                f"  - id: {overlay}-overlay\n"
                f"    overlay: {overlay}\n"
                f"    paths: [{self.broad_root}]\n"
                # default: on + overlay-gated -> the OVERLAY is the gate. Overlay
                # off => rule filtered (zero); overlay on => expected-by-default,
                # so sync proposes the whole pack. This mirrors the real policy.
                "    default: on\n"
                "    skills:\n"
                f"{skill_lines}\n"
            )
        fleet.skill_scope_path.write_text(
            "version: 1\n"
            "skill_source_roots:\n"
            f"  - {self.src_root}\n"
            "overlays:\n"
            "  - name: swarm\n    default: off\n"
            "  - name: research\n    default: off\n"
            "  - name: hardening\n    default: off\n"
            "  - name: operator-maintenance\n    default: off\n"
            "rules:\n" + "".join(rule_blocks),
            encoding="utf-8",
        )

    @property
    def model(self) -> dict:
        return {
            "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(self.fleet.clients_root)},
            "clients": [],
            "skills": [],
        }

    def sync_forced_plan(
        self, cwd: Path, overlays: str, *, dry_run: bool = True
    ) -> set[str]:
        """Skills a policy-evaluated ``skill sync`` proposes to LINK at ``cwd``
        with ``overlays`` (comma-joined) forced active for the call."""
        prior = os.environ.get(sv.OVERLAY_ENV_VAR)
        os.environ[sv.OVERLAY_ENV_VAR] = overlays
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
        return {
            str(action.get("skill") or "")
            for action in (plan.get("actions") or [])
            if action.get("op") == "link" and action.get("skill")
        }


@pytest.fixture
def scenario(fixture_fleet) -> ModePackScenario:
    return ModePackScenario(fixture_fleet)


# --------------------------------------------------------------------------- #
# Per-pack: literal declaration + flip-links-narrowed-set                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("overlay", sorted(PACK_SKILLS))
def test_overlay_declares_exactly_its_pack_skills(
    scenario: ModePackScenario, overlay: str
) -> None:
    """Each overlay tag enumerates exactly its bead-specified skill set."""
    literal = sv.overlay_scoped_skill_names(scenario.model, overlay)
    assert literal == set(PACK_SKILLS[overlay])


@pytest.mark.parametrize("overlay", sorted(PACK_SKILLS))
def test_flip_on_in_matching_cwd_links_exactly_the_pack_set(
    scenario: ModePackScenario, overlay: str
) -> None:
    """Flipping ONE pack on in a matching cwd links EXACTLY that pack -- the
    whole mode set, nothing from the other packs."""
    linked = scenario.sync_forced_plan(scenario.matching, overlay)
    expected = set(PACK_SKILLS[overlay])
    assert linked == expected, (
        f"overlay {overlay!r} should link exactly {sorted(expected)} in a "
        f"matching cwd, got {sorted(linked)}"
    )
    # And NONE of the OTHER packs' skills leaked in.
    other = set(ALL_PACK_SKILLS) - expected
    assert not (linked & other), f"overlay {overlay!r} leaked other packs: {sorted(linked & other)}"


@pytest.mark.parametrize("overlay", sorted(PACK_SKILLS))
def test_flip_on_in_non_matching_cwd_links_zero(
    scenario: ModePackScenario, overlay: str
) -> None:
    """Outside the rule's roots, a forced sync links ZERO -- overlays never
    literal-link a pack into a non-matching cwd (vq0.1 semantics)."""
    linked = scenario.sync_forced_plan(scenario.non_matching, overlay)
    assert linked == set(), (
        f"overlay {overlay!r} must link zero in a non-matching cwd, got {sorted(linked)}"
    )
    assert not (scenario.non_matching / ".claude" / "skills").exists()


@pytest.mark.parametrize("overlay", sorted(PACK_SKILLS))
def test_overlay_off_links_zero_even_in_matching_cwd(
    scenario: ModePackScenario, overlay: str
) -> None:
    """With NO overlay forced active, the pack is invisible even where it would
    otherwise match -- the overlay flip is the gate."""
    linked = scenario.sync_forced_plan(scenario.matching, "")
    assert not (linked & set(PACK_SKILLS[overlay])), (
        f"overlay {overlay!r} skills linked while overlay OFF: "
        f"{sorted(linked & set(PACK_SKILLS[overlay]))}"
    )


def test_each_flip_is_independent_not_a_union(scenario: ModePackScenario) -> None:
    """Flipping swarm links ONLY swarm; flipping research links ONLY research --
    the packs narrow independently, they don't union into one big set."""
    swarm = scenario.sync_forced_plan(scenario.matching, "swarm")
    research = scenario.sync_forced_plan(scenario.matching, "research")
    assert swarm == set(PACK_SKILLS["swarm"])
    assert research == set(PACK_SKILLS["research"])
    assert swarm.isdisjoint(research)


def test_multiple_overlays_on_links_the_union_of_those_packs(
    scenario: ModePackScenario,
) -> None:
    """Flipping two packs on together links exactly the union of the two -- and
    still nothing from the packs left off."""
    linked = scenario.sync_forced_plan(scenario.matching, "swarm,hardening")
    expected = set(PACK_SKILLS["swarm"]) | set(PACK_SKILLS["hardening"])
    assert linked == expected
    # research + operator-maintenance stayed off.
    off = set(PACK_SKILLS["research"]) | set(PACK_SKILLS["operator-maintenance"])
    assert not (linked & off)


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-q"]))
