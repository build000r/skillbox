"""Lint coverage for the global skill contract (repos-sbp-canon-hdl.2).

The operator's ``skill-scope.yaml`` declares the always-global skill surface in
two hand-synced places: the flat ``global_allowlist`` list and every rule with
``allow_global: true``. ``validate_global_skill_contract`` asserts those two
lists describe the same set so they cannot silently drift apart. These tests:

* prove the lint is GREEN against the committed public contract fixture,
* prove the lint is GREEN on an in-memory consistent policy, and
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


# The 14 operator skills the global-trim decision landed on (policy-estate
# oh1.1): the 2 dispatcher-core skills + the 12 named operator exceptions.
DISPATCHER_CORE = ["smart", "sbp"]
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
ALL_GLOBALS = DISPATCHER_CORE + OPERATOR_EXCEPTIONS


def _consistent_policy() -> dict:
    """A policy where global_allowlist == union of allow_global rules."""
    return {
        "global_allowlist": list(ALL_GLOBALS),
        "rules": [
            {"id": "dispatcher-global", "skills": list(DISPATCHER_CORE), "allow_global": True},
            {
                "id": "operator-global-exceptions",
                "skills": list(OPERATOR_EXCEPTIONS),
                "allow_global": True,
            },
            # A task-skill rule with no allow_global must be ignored by the union.
            {"id": "task-on-demand", "skills": ["cass", "describe"], "default": "off"},
        ],
    }


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
        self.assertEqual(
            set(results[0].details["global_skills"]),
            set(ALL_GLOBALS),
        )

    def test_lint_green_on_consistent_policy(self) -> None:
        results = validate_global_skill_contract(_consistent_policy())
        self.assertEqual(self._statuses(results), ["pass"], results[0].details)
        self.assertEqual(set(results[0].details["global_skills"]), set(ALL_GLOBALS))

    def test_lint_red_when_allowlist_has_extra_skill(self) -> None:
        """A skill in global_allowlist but granted by no allow_global rule = drift."""
        policy = _consistent_policy()
        policy["global_allowlist"].append("rogue-skill")
        results = validate_global_skill_contract(policy)
        self.assertEqual(self._statuses(results), ["fail"], results)
        self.assertEqual(results[0].code, GLOBAL_SKILL_CONTRACT_CODE)
        self.assertIn("rogue-skill", results[0].details["in_allowlist_only"])
        self.assertIn("rogue-skill", results[0].message + str(results[0].details))

    def test_lint_red_when_rule_grants_skill_missing_from_allowlist(self) -> None:
        """A skill granted by an allow_global rule but absent from global_allowlist = drift."""
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
        self.assertIn("together", results[0].message)

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
