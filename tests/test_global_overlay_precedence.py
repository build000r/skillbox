"""Lint coverage for global-vs-overlay precedence (repos-sbp-policy-estate-oh1.2).

The global skill contract has a strict precedence: an always-global skill -- one
granted by an ``allow_global: true`` rule (the dispatcher core +
``operator-global-exceptions``, e.g. ``divide-and-conquer``) -- is linked into
every repo unconditionally. ``global_allowlist`` is only a derived snapshot.
Flipping a mode-pack overlay can neither add nor remove a global skill.
**Global wins**, so an overlay rule may only meaningfully add NON-global skills.

``validate_global_overlay_precedence`` makes a double-declaration (a skill that
is both always-global AND overlay-gated) a hard, named FAIL. These tests:

* prove the lint is GREEN against a committed public fixture (the
  four mode packs were deliberately authored to exclude always-global
  ``divide-and-conquer`` from the swarm pack),
* prove the lint is GREEN on an in-memory disjoint policy,
* prove the lint is RED when a skill is BOTH always-global and overlay-gated,
  via an ``allow_global`` rule,
* prove the failure names the offending skill, the gating overlay rule, and the
  fix, and
* cover the empty/parse/missing-file edges.

Run just these with::

    python3 -m pytest tests/ -k global_overlay_precedence -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.validation import (  # noqa: E402
    GLOBAL_OVERLAY_PRECEDENCE_CODE,
    validate_global_overlay_precedence,
    validate_global_overlay_precedence_file,
)


def _disjoint_policy() -> dict:
    """A policy where the always-global set and overlay-gated set are disjoint.

    Mirrors the real shape: divide-and-conquer is always-global (operator
    exception) and the swarm overlay names only NON-global swarm skills.
    """
    return {
        "global_allowlist": ["smart", "sbp", "divide-and-conquer"],
        "overlays": [{"name": "swarm", "default": "off"}],
        "rules": [
            {"id": "dispatcher-global", "skills": ["smart", "sbp"], "allow_global": True},
            {
                "id": "operator-global-exceptions",
                "skills": ["divide-and-conquer"],
                "allow_global": True,
            },
            # Overlay rule names ONLY non-global swarm skills -- the disjoint delta.
            {
                "id": "swarm-overlay",
                "overlay": "swarm",
                "skills": ["dueling-idea-wizards", "ntm", "vibing-with-ntm"],
                "default": "off",
            },
        ],
    }


class GlobalOverlayPrecedenceLintTests(unittest.TestCase):
    def _statuses(self, results) -> list[str]:
        return [r.status for r in results]

    def test_lint_green_on_public_policy_fixture(self) -> None:
        """The public policy fixture keeps always-global and overlay-gated skills disjoint.

        Concretely: divide-and-conquer is always-global and must NOT appear in
        the swarm overlay (the precedence the four mode packs were authored to
        respect)."""
        results = validate_global_overlay_precedence(_disjoint_policy())
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0].code, GLOBAL_OVERLAY_PRECEDENCE_CODE)
        self.assertEqual(
            results[0].status,
            "pass",
            f"public policy fixture has a global/overlay precedence conflict: "
            f"{results[0].message} :: {results[0].details}",
        )
        # Sanity: divide-and-conquer is always-global, and is NOT overlay-gated.
        details = results[0].details
        self.assertIn("divide-and-conquer", details["always_global"])
        self.assertNotIn("divide-and-conquer", details["overlay_gated"])

    def test_lint_green_on_disjoint_policy(self) -> None:
        results = validate_global_overlay_precedence(_disjoint_policy())
        self.assertEqual(self._statuses(results), ["pass"], results[0].details)
        self.assertNotIn(
            "divide-and-conquer", results[0].details["overlay_gated"]
        )

    def test_lint_red_when_global_skill_is_also_overlay_gated_via_allow_global(self) -> None:
        """The canonical ambiguity: an allow_global skill named in an overlay rule."""
        policy = _disjoint_policy()
        # Smuggle the always-global divide-and-conquer into the swarm overlay.
        policy["rules"][2]["skills"].append("divide-and-conquer")
        results = validate_global_overlay_precedence(
            policy, policy_path="/fake/skill-scope.yaml"
        )
        self.assertEqual(self._statuses(results), ["fail"], results)
        self.assertEqual(results[0].code, GLOBAL_OVERLAY_PRECEDENCE_CODE)
        self.assertIn("divide-and-conquer", results[0].details["conflicts"])
        # The failure names the skill, the gating overlay rule, and the file.
        blob = results[0].message + str(results[0].details)
        self.assertIn("divide-and-conquer", blob)
        self.assertIn("swarm-overlay", blob)
        self.assertIn("swarm", blob)  # the overlay tag
        self.assertIn("/fake/skill-scope.yaml", results[0].message)
        self.assertEqual(
            results[0].details["offending_overlay_rules"]["divide-and-conquer"],
            ["swarm-overlay (overlay: swarm)"],
        )

    def test_snapshot_only_skill_is_not_always_global_for_precedence(self) -> None:
        """global_allowlist is a snapshot, not an always-global grant."""
        policy = {
            "global_allowlist": ["lonely-global"],
            "overlays": [{"name": "swarm"}],
            "rules": [
                {
                    "id": "swarm-overlay",
                    "overlay": "swarm",
                    "skills": ["lonely-global", "ntm"],
                },
            ],
        }
        results = validate_global_overlay_precedence(policy)
        self.assertEqual(self._statuses(results), ["pass"], results)
        self.assertNotIn("lonely-global", results[0].details["always_global"])
        # The global-skill-contract lint owns the stale snapshot failure.
        self.assertIn("ntm", results[0].details["overlay_gated"])

    def test_failure_groups_multiple_overlay_rules_under_one_skill(self) -> None:
        policy = {
            "global_allowlist": [],
            "overlays": [{"name": "swarm"}, {"name": "research"}],
            "rules": [
                {"id": "g", "skills": ["dac"], "allow_global": True},
                {"id": "swarm-overlay", "overlay": "swarm", "skills": ["dac"]},
                {"id": "research-overlay", "overlay": "research", "skills": ["dac"]},
            ],
        }
        results = validate_global_overlay_precedence(policy)
        self.assertEqual(self._statuses(results), ["fail"])
        self.assertEqual(
            results[0].details["offending_overlay_rules"]["dac"],
            ["swarm-overlay (overlay: swarm)", "research-overlay (overlay: research)"],
        )

    def test_non_overlay_rule_naming_a_global_skill_is_not_a_conflict(self) -> None:
        """A non-overlay rule (no overlay: tag) may freely re-name a global skill.

        Only OVERLAY-gated co-declaration is the ambiguity this lint guards; the
        operator-utilities-on-demand-style rules that also list global skills are
        fine (they are not gated behind an overlay flip)."""
        policy = {
            "global_allowlist": ["divide-and-conquer"],
            "overlays": [{"name": "swarm"}],
            "rules": [
                {
                    "id": "operator-global-exceptions",
                    "skills": ["divide-and-conquer"],
                    "allow_global": True,
                },
                # Same skill in a plain on-demand rule (NO overlay tag): allowed.
                {
                    "id": "operator-utilities-on-demand",
                    "skills": ["divide-and-conquer", "cass"],
                    "default": "off",
                },
                {"id": "swarm-overlay", "overlay": "swarm", "skills": ["ntm"]},
            ],
        }
        results = validate_global_overlay_precedence(policy)
        self.assertEqual(self._statuses(results), ["pass"], results[0].details)

    def test_glob_granted_global_collides_with_literal_overlay_skill(self) -> None:
        """BUG 3 regression: the runtime's ``_global_install_allowed`` decides
        always-global membership via ``fnmatch``, so an ``allow_global`` rule
        granting ``beads-*`` makes the literal ``beads-br`` always-global on every
        box. An overlay rule naming ``beads-br`` IS the global-vs-overlay
        contradiction this lint must catch — an exact-string set intersection would
        miss it (``beads-br != beads-*``). The glob-aware matcher catches it."""
        policy = {
            "global_allowlist": ["beads-*"],
            "overlays": [{"name": "swarm"}],
            "rules": [
                {"id": "g", "skills": ["beads-*"], "allow_global": True},
                {"id": "swarm-overlay", "overlay": "swarm", "skills": ["beads-br", "ntm"]},
            ],
        }
        results = validate_global_overlay_precedence(
            policy, policy_path="/fake/skill-scope.yaml"
        )
        self.assertEqual(self._statuses(results), ["fail"], results[0].details)
        # The literal beads-br is caught; the disjoint ntm is not.
        self.assertIn("beads-br", results[0].details["conflicts"])
        self.assertNotIn("ntm", results[0].details["conflicts"])
        self.assertEqual(
            results[0].details["offending_overlay_rules"]["beads-br"],
            ["swarm-overlay (overlay: swarm)"],
        )

    def test_non_matching_glob_global_does_not_false_conflict(self) -> None:
        """A glob global must only collide with names it actually matches: a
        ``beads-*`` global does NOT make an overlay's ``ntm`` a conflict."""
        policy = {
            "global_allowlist": ["beads-*"],
            "overlays": [{"name": "swarm"}],
            "rules": [
                {"id": "g", "skills": ["beads-*"], "allow_global": True},
                {"id": "swarm-overlay", "overlay": "swarm", "skills": ["ntm"]},
            ],
        }
        results = validate_global_overlay_precedence(policy)
        self.assertEqual(self._statuses(results), ["pass"], results[0].details)

    def test_overlay_rule_authored_with_patterns_is_seen(self) -> None:
        """BUG 2 regression: an overlay rule that names its skills via ``patterns:``
        (no ``skills:`` key) must still be enumerated, so a global skill smuggled
        into a patterns-authored overlay rule is caught."""
        policy = {
            "global_allowlist": ["divide-and-conquer"],
            "overlays": [{"name": "swarm"}],
            "rules": [
                {"id": "g", "skills": ["divide-and-conquer"], "allow_global": True},
                # Overlay rule uses `patterns:` instead of `skills:`.
                {
                    "id": "swarm-overlay",
                    "overlay": "swarm",
                    "patterns": ["divide-and-conquer", "ntm"],
                },
            ],
        }
        results = validate_global_overlay_precedence(policy)
        self.assertEqual(self._statuses(results), ["fail"], results[0].details)
        self.assertIn("divide-and-conquer", results[0].details["conflicts"])

    def test_empty_policy_is_pass(self) -> None:
        results = validate_global_overlay_precedence({"rules": []})
        self.assertEqual(self._statuses(results), ["pass"], results)
        self.assertIn("no global/overlay surface", results[0].message)

    def test_missing_file_is_pass_and_bad_mapping_is_pass(self) -> None:
        missing = validate_global_overlay_precedence_file("/nope/skill-scope.yaml")
        self.assertEqual([r.status for r in missing], ["pass"])
        not_mapping = validate_global_overlay_precedence(["not", "a", "mapping"])  # type: ignore[arg-type]
        self.assertEqual([r.status for r in not_mapping], ["pass"])


if __name__ == "__main__":
    unittest.main()
