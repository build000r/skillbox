"""Lint coverage for the global skill contract (repos-sbp-canon-hdl.2).

The operator's ``skill-scope.yaml`` declares the always-global skill surface in
``allow_global: true`` rules. The flat ``global_allowlist`` key is an optional
derived snapshot, not a second authority. ``validate_global_skill_contract``
asserts that any committed snapshot matches the rule-derived set. These tests:

* prove the lint is GREEN against the committed public contract fixture,
* prove the lint is GREEN on an in-memory policy without a snapshot, and
* prove the lint is RED on a planted drift (in each direction).

Run just these with::

    python3 -m pytest tests/ -k global_contract -v
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
    GLOBAL_SKILL_CONTRACT_CODE,
    validate_global_skill_contract,
)
from runtime_manager._skill_common import DISPATCHER_CORE  # noqa: E402


OPERATOR_EXCEPTIONS = [
    "beads-br",
    "beads-bv",
    "beads-workflow",
    "codebase-audit",
    "divide-and-conquer",
    "git-stash-janitor",
    "lube",
    "mmdx",
    "no-ragrets",
    "project-status-mmdx",
    "skill-issue",
    "ui-fresh-eyes",
]


def _policy_rules() -> list[dict]:
    return [
        {"id": "dispatcher-global", "skills": list(DISPATCHER_CORE), "allow_global": True},
        {
            "id": "operator-global-exceptions",
            "skills": list(OPERATOR_EXCEPTIONS),
            "allow_global": True,
        },
        # A task-skill rule with no allow_global must be ignored by the union.
        {"id": "task-on-demand", "skills": ["cass", "describe"], "default": "off"},
    ]


def _derived_global_skills(policy: dict) -> set[str]:
    names: set[str] = set()
    for rule in policy.get("rules") or []:
        if not rule.get("allow_global"):
            continue
        for key in ("skills", "patterns", "names"):
            for name in rule.get(key) or []:
                if str(name).strip():
                    names.add(str(name).strip())
            if rule.get(key):
                break
    return names


def _consistent_policy(*, include_snapshot: bool = True) -> dict:
    """A policy where any global_allowlist snapshot matches allow_global rules."""
    policy = {
        "rules": [
            dict(rule) for rule in _policy_rules()
        ],
    }
    if include_snapshot:
        policy["global_allowlist"] = sorted(_derived_global_skills(policy))
    return policy


class GlobalContractLintTests(unittest.TestCase):
    def _statuses(self, results) -> list[str]:
        return [r.status for r in results]

    def test_lint_green_on_public_contract_fixture(self) -> None:
        """The public contract fixture keeps the two lists in agreement."""
        results = validate_global_skill_contract(_consistent_policy())
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0].code, GLOBAL_SKILL_CONTRACT_CODE)
        self.assertEqual(
            results[0].status,
            "pass",
            f"public skill contract drifted: {results[0].message} :: {results[0].details}",
        )
        # And the public set is exactly the decided 14 operator skills.
        expected = _derived_global_skills(_consistent_policy())
        self.assertEqual(
            set(results[0].details["global_skills"]),
            expected,
        )
        self.assertEqual(len(expected), 14)
        self.assertTrue(set(DISPATCHER_CORE).issubset(expected))

    def test_dispatcher_core_is_the_floor_not_all_operator_globals(self) -> None:
        expected = _derived_global_skills(_consistent_policy())

        self.assertEqual(set(DISPATCHER_CORE), {"smart", "sbp"})
        self.assertTrue(set(DISPATCHER_CORE).issubset(expected))
        self.assertTrue(set(OPERATOR_EXCEPTIONS).issubset(expected))
        self.assertTrue(set(DISPATCHER_CORE).isdisjoint(OPERATOR_EXCEPTIONS))

    def test_lint_green_on_consistent_policy(self) -> None:
        results = validate_global_skill_contract(_consistent_policy())
        self.assertEqual(self._statuses(results), ["pass"], results[0].details)
        self.assertEqual(
            set(results[0].details["global_skills"]),
            _derived_global_skills(_consistent_policy()),
        )

    def test_global_allowlist_snapshot_is_optional(self) -> None:
        policy = _consistent_policy(include_snapshot=False)
        results = validate_global_skill_contract(policy)
        self.assertEqual(self._statuses(results), ["pass"], results[0].details)
        self.assertFalse(results[0].details["global_allowlist_present"])
        self.assertEqual(
            set(results[0].details["global_skills"]),
            _derived_global_skills(policy),
        )

    def test_lint_red_when_allowlist_has_extra_skill(self) -> None:
        """A skill in the snapshot but granted by no allow_global rule = drift."""
        policy = _consistent_policy()
        policy["global_allowlist"].append("rogue-skill")
        results = validate_global_skill_contract(policy)
        self.assertEqual(self._statuses(results), ["fail"], results)
        self.assertEqual(results[0].code, GLOBAL_SKILL_CONTRACT_CODE)
        self.assertIn("rogue-skill", results[0].details["in_allowlist_only"])
        self.assertIn("rogue-skill", results[0].message + str(results[0].details))
        self.assertIn("This list is derived; edit rules instead", results[0].message)

    def test_lint_red_when_rule_grants_skill_missing_from_allowlist(self) -> None:
        """A skill granted by an allow_global rule but absent from the snapshot = drift."""
        policy = _consistent_policy()
        policy["rules"][1]["skills"].append("smuggled-global")
        results = validate_global_skill_contract(policy)
        self.assertEqual(self._statuses(results), ["fail"], results)
        self.assertIn("smuggled-global", results[0].details["in_rules_only"])

    def test_lint_failure_names_drifted_skills_and_fix(self) -> None:
        policy = _consistent_policy()
        policy["global_allowlist"].append("only-in-allowlist")
        policy["rules"][0]["skills"].append("only-in-rule")
        results = validate_global_skill_contract(policy, policy_path="/fake/skill-scope.yaml")
        self.assertEqual(results[0].status, "fail")
        self.assertIn("only-in-allowlist", results[0].details["in_allowlist_only"])
        self.assertIn("only-in-rule", results[0].details["in_rules_only"])
        # Failure message points at the file and states the fix.
        self.assertIn("/fake/skill-scope.yaml", results[0].message)
        self.assertIn("This list is derived; edit rules instead", results[0].message)

    def test_empty_policy_is_pass(self) -> None:
        results = validate_global_skill_contract({"rules": []})
        self.assertEqual(self._statuses(results), ["pass"], results)

    def test_allow_global_rule_authored_with_patterns_is_seen(self) -> None:
        """BUG 2 regression: the runtime reads a rule's skills via
        ``skills or patterns or names``. A rule that grants ``allow_global`` via a
        ``patterns:`` list (no ``skills:`` key) must be visible to this lint, or it
        falsely fails ("in allowlist but no rule grants them"). The allowlist below
        lists exactly the patterns-authored global, so a synonym-aware lint is GREEN."""
        policy = {
            "global_allowlist": ["smart", "sbp"],
            "rules": [
                # allow_global granted via `patterns:` instead of `skills:`.
                {"id": "dispatcher-global", "patterns": ["smart", "sbp"], "allow_global": True},
            ],
        }
        results = validate_global_skill_contract(policy)
        self.assertEqual(
            self._statuses(results),
            ["pass"],
            f"patterns-authored allow_global rule was invisible: {results[0].details}",
        )
        self.assertEqual(set(results[0].details["global_skills"]), {"smart", "sbp"})

    def test_allow_global_rule_authored_with_names_is_seen(self) -> None:
        """Same as above for the third synonym, ``names:``."""
        policy = {
            "global_allowlist": ["smart"],
            "rules": [
                {"id": "dispatcher-global", "names": ["smart"], "allow_global": True},
            ],
        }
        results = validate_global_skill_contract(policy)
        self.assertEqual(self._statuses(results), ["pass"], results[0].details)

    def test_patterns_authored_global_still_detects_real_drift(self) -> None:
        """Synonym-awareness must not blunt the lint: a patterns-granted skill
        missing from the allowlist is still real drift."""
        policy = {
            "global_allowlist": ["smart"],
            "rules": [
                {"id": "g", "patterns": ["smart", "smuggled"], "allow_global": True},
            ],
        }
        results = validate_global_skill_contract(policy)
        self.assertEqual(self._statuses(results), ["fail"], results)
        self.assertIn("smuggled", results[0].details["in_rules_only"])


if __name__ == "__main__":
    unittest.main()
