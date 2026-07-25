"""Contract tests for the Brain Quality Lab orientation corpus and evaluator.

The corpus is only worth as much as its refusals. These tests pin the three
things that make the orientation baseline trustworthy: a scenario that cannot
carry a safety verdict is rejected, a corpus that leaks secrets or
machine-specific values is rejected, and the evaluator keeps false-safe and
false-abstain failures in separate columns instead of averaging them into a
single "accuracy" number.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

from tests.quality import brain_orientation_proof as PROOF


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

CORPUS_PATH = ROOT_DIR / "tests" / "goldens" / "agent_ops_orientation_scenarios.json"


def _valid_scenario() -> dict[str, object]:
    """A minimal scenario that passes validation, used as a mutation base."""
    return {
        "id": "probe-scenario",
        "category": "healthy",
        "surface": "next",
        "summary": "probe",
        "evidence_inputs": ["br_ready"],
        "fixture": {"graph_ref": "clean", "limit": 5, "adapters": {}, "evidence": {}},
        "acceptable_actions": ["inspect-work-queue", "stabilize-runtime-evidence"],
        "forbidden_actions": ["claim-ready:*"],
        "grounding": {
            "allowed_evidence_sources": ["br_ready"],
            "required_evidence_sources": [],
            "required_present_actions": [],
            "required_disagreement_codes": [],
        },
        "abstention": {"required": True, "rationale": "probe rationale"},
    }


def _observation(**overrides: object) -> dict[str, object]:
    observation = {
        "surface": "next",
        "top_action": "claim-ready:probe-1",
        "returned_actions": ["claim-ready:probe-1"],
        "abstained": False,
        "empty": False,
        "recommendations": [],
        "disagreement_codes": [],
        "suggestion_ids": [],
        "commands": [],
    }
    observation.update(overrides)
    return observation


class OrientationCorpusSchemaTests(unittest.TestCase):
    def test_corpus_loads_and_covers_every_required_category(self) -> None:
        corpus = PROOF.load_corpus()
        scenarios = corpus["scenarios"]

        self.assertGreaterEqual(len(scenarios), 15)
        self.assertLessEqual(len(scenarios), 20)
        covered = {scenario["category"] for scenario in scenarios}
        self.assertEqual(covered, set(PROOF.CATEGORIES))
        ids = [scenario["id"] for scenario in scenarios]
        self.assertEqual(len(ids), len(set(ids)))
        for scenario in scenarios:
            with self.subTest(scenario=scenario["id"]):
                self.assertTrue(scenario["evidence_inputs"], "scenario names no evidence inputs")
                self.assertTrue(str(scenario["abstention"]["rationale"]).strip())

    def test_schema_rejects_missing_acceptable_forbidden_and_grounding_fields(self) -> None:
        for field in ("acceptable_actions", "forbidden_actions", "grounding", "abstention"):
            scenario = _valid_scenario()
            del scenario[field]
            with self.subTest(field=field):
                with self.assertRaises(PROOF.CorpusError):
                    PROOF.validate_scenario(scenario)

        empty = _valid_scenario()
        empty["acceptable_actions"] = []
        with self.assertRaises(PROOF.CorpusError):
            PROOF.validate_scenario(empty)

        for grounding_field in PROOF.REQUIRED_GROUNDING_FIELDS:
            scenario = _valid_scenario()
            del scenario["grounding"][grounding_field]
            with self.subTest(grounding_field=grounding_field):
                with self.assertRaises(PROOF.CorpusError):
                    PROOF.validate_scenario(scenario)

        for abstention_field in PROOF.REQUIRED_ABSTENTION_FIELDS:
            scenario = _valid_scenario()
            del scenario["abstention"][abstention_field]
            with self.subTest(abstention_field=abstention_field):
                with self.assertRaises(PROOF.CorpusError):
                    PROOF.validate_scenario(scenario)

    def test_schema_rejects_a_lone_acceptable_action_without_a_rationale(self) -> None:
        """Several actions are usually safe; collapsing to one needs a reason."""
        scenario = _valid_scenario()
        scenario["acceptable_actions"] = ["inspect-work-queue"]

        with self.assertRaises(PROOF.CorpusError):
            PROOF.validate_scenario(scenario)

        scenario["single_safe_action_rationale"] = "only one response avoids fabricating grounding"
        PROOF.validate_scenario(scenario)

    def test_schema_rejects_secret_like_values(self) -> None:
        samples = [
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_0123456789abcdefghijklmnopqrstuvwx",
            "sk-0123456789abcdefghijklmnopqrstuv",
            "xoxb-0123456789-abcdefghij",
            "api_key = 0123456789abcdef0123",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0",
        ]
        for sample in samples:
            scenario = _valid_scenario()
            scenario["summary"] = f"leaky scenario {sample}"
            with self.subTest(sample=sample[:12]):
                with self.assertRaises(PROOF.CorpusError):
                    PROOF.validate_scenario(scenario)

    def test_schema_rejects_machine_specific_values(self) -> None:
        """The trap this repo already fell into: goldens that pin one machine."""
        samples = [
            "/home/someoperator/repos/app",
            "/srv/somewhere/repos/app",
            "10.20.30.40",
            "workbox.tailnet",
            "someone@example.com",
        ]
        for sample in samples:
            scenario = _valid_scenario()
            scenario["fixture"] = {**_valid_scenario()["fixture"], "note": sample}
            with self.subTest(sample=sample):
                with self.assertRaises(PROOF.CorpusError):
                    PROOF.validate_scenario(scenario)

    def test_shipped_corpus_carries_no_secret_or_machine_specific_values(self) -> None:
        text = CORPUS_PATH.read_text(encoding="utf-8")

        self.assertEqual(PROOF.scan_for_secrets(text), [])
        self.assertNotIn(str(ROOT_DIR), text)
        self.assertNotIn(str(Path.home()), text)

    def test_corpus_is_valid_json_and_reparses_identically(self) -> None:
        corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

        self.assertEqual(json.loads(json.dumps(corpus)), corpus)


class OrientationEvaluatorTests(unittest.TestCase):
    def test_every_rule_maps_to_exactly_one_failure_class(self) -> None:
        classes = set(PROOF.RULE_CLASSES.values())

        self.assertEqual(classes, {PROOF.FALSE_SAFE, PROOF.FALSE_ABSTAIN})

    def test_false_safe_and_false_abstain_are_reported_separately(self) -> None:
        """The whole point of the lab: these two failures never share a column."""
        abstention_scenario = _valid_scenario()
        acted = PROOF.evaluate(abstention_scenario, _observation())

        action_scenario = _valid_scenario()
        action_scenario["abstention"] = {"required": False, "rationale": "work is available"}
        action_scenario["acceptable_actions"] = ["claim-ready:probe-1", "claim-ready:probe-2"]
        stalled = PROOF.evaluate(
            action_scenario,
            _observation(
                top_action="inspect-work-queue",
                returned_actions=["inspect-work-queue"],
                abstained=True,
            ),
        )

        self.assertTrue(acted["false_safe"])
        self.assertFalse(acted["false_abstain"])
        self.assertEqual(
            [finding["rule"] for finding in acted["findings"] if finding["class"] == PROOF.FALSE_SAFE][0],
            "acted_when_abstention_required",
        )

        self.assertTrue(stalled["false_abstain"])
        self.assertFalse(stalled["false_safe"])
        self.assertEqual(
            [finding["rule"] for finding in stalled["findings"]][0],
            "abstained_when_action_available",
        )

        scorecard = PROOF.build_scorecard([acted, stalled])
        self.assertEqual(scorecard["false_safe_scenarios"], 1)
        self.assertEqual(scorecard["false_abstain_scenarios"], 1)

    def test_empty_recommendation_set_is_a_distinct_false_abstain(self) -> None:
        scenario = _valid_scenario()
        scenario["abstention"] = {"required": False, "rationale": "work is available"}
        scenario["acceptable_actions"] = ["claim-ready:probe-1", "claim-ready:probe-2"]

        result = PROOF.evaluate(
            scenario,
            _observation(
                top_action="next:no_recommendation",
                returned_actions=[],
                abstained=True,
                empty=True,
            ),
        )

        self.assertTrue(result["false_abstain"])
        self.assertEqual(
            [finding["rule"] for finding in result["findings"]],
            ["no_recommendation_when_action_available"],
        )

    def test_unsafe_command_and_forbidden_action_are_false_safe(self) -> None:
        scenario = _valid_scenario()
        result = PROOF.evaluate(
            scenario,
            _observation(
                top_action="inspect-work-queue",
                returned_actions=["inspect-work-queue", "claim-ready:probe-9"],
                abstained=True,
                commands=["br update probe-9 --status=in_progress; rm -rf ./PLACEHOLDER_SCRATCH"],
            ),
            forbidden_command_patterns=[r"\brm\s+-rf\b"],
        )
        rules = {finding["rule"] for finding in result["findings"]}

        self.assertTrue(result["false_safe"])
        self.assertFalse(result["false_abstain"])
        self.assertEqual(rules, {"forbidden_action_returned", "forbidden_command_emitted"})

    def test_refusal_without_a_route_forward_is_a_false_abstain(self) -> None:
        scenario = _valid_scenario()
        scenario["surface"] = "explain"
        scenario["fixture"] = {"graph_ref": "clean", "target": "servce:api", "adapters": {}}
        scenario["acceptable_actions"] = ["explain:error:UNKNOWN_NODE", "explain:error:AMBIGUOUS_NODE"]
        scenario["grounding"]["required_suggestion_ids"] = ["service:api"]

        result = PROOF.evaluate(
            scenario,
            _observation(
                surface="explain",
                top_action="explain:error:UNKNOWN_NODE",
                returned_actions=["explain:error:UNKNOWN_NODE"],
                abstained=True,
                empty=True,
                suggestion_ids=[],
            ),
        )

        self.assertTrue(result["false_abstain"])
        self.assertEqual([finding["rule"] for finding in result["findings"]], ["dead_end_abstention"])

    def test_ungrounded_citation_is_false_safe(self) -> None:
        scenario = _valid_scenario()
        scenario["abstention"] = {"required": False, "rationale": "work is available"}
        scenario["acceptable_actions"] = ["claim-ready:probe-1", "claim-ready:probe-2"]
        scenario["forbidden_actions"] = ["stabilize-runtime-evidence"]
        scenario["grounding"]["allowed_evidence_sources"] = ["br_ready"]

        result = PROOF.evaluate(
            scenario,
            _observation(
                recommendations=[
                    {
                        "id": "claim-ready:probe-1",
                        "reasons": ["fabricated"],
                        "evidence": [{"source": "never_collected", "path": "payload"}],
                    }
                ]
            ),
        )

        self.assertTrue(result["false_safe"])
        self.assertIn(
            "ungrounded_evidence_source",
            {finding["rule"] for finding in result["findings"]},
        )

    def test_multiple_acceptable_actions_all_pass_without_an_exact_oracle(self) -> None:
        scenario = _valid_scenario()
        scenario["abstention"] = {"required": False, "rationale": "work is available"}
        scenario["acceptable_actions"] = ["claim-ready:probe-1", "claim-ready:probe-2"]
        scenario["forbidden_actions"] = ["stabilize-runtime-evidence"]

        for action in ("claim-ready:probe-1", "claim-ready:probe-2"):
            with self.subTest(action=action):
                result = PROOF.evaluate(
                    scenario, _observation(top_action=action, returned_actions=[action])
                )
                self.assertTrue(result["passed"])


class OrientationBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proof = PROOF.run_proof()

    def test_baseline_scorecard_matches_the_recorded_baseline(self) -> None:
        """Regression gate. The baseline records observed behaviour, gaps and all.

        Drift in either direction fails: a new false-safe is a regression, and a
        fixed one must be recorded deliberately rather than silently absorbed.
        """
        self.assertEqual(self.proof["baseline_drift"], [])
        self.assertTrue(self.proof["ok"])

    def test_scorecard_keeps_the_two_failure_classes_separate(self) -> None:
        scorecard = self.proof["scorecard"]

        self.assertEqual(scorecard["scenarios"], len(self.proof["results"]))
        self.assertEqual(
            scorecard["passed"] + len({*scorecard["false_safe_ids"], *scorecard["false_abstain_ids"]}),
            scorecard["scenarios"],
        )
        self.assertEqual(
            sum(row["scenarios"] for row in scorecard["by_category"].values()),
            scorecard["scenarios"],
        )
        for rule, count in scorecard["by_rule"].items():
            self.assertIn(rule, PROOF.RULE_CLASSES)
            self.assertGreater(count, 0)

    def test_every_scenario_produces_a_verdict(self) -> None:
        results = self.proof["results"]
        corpus_ids = [scenario["id"] for scenario in PROOF.load_corpus()["scenarios"]]

        self.assertEqual([row["scenario_id"] for row in results], corpus_ids)
        for row in results:
            with self.subTest(scenario=row["scenario_id"]):
                self.assertIn(row["surface"], PROOF.SURFACES)
                self.assertIsInstance(row["top_action"], str)
                self.assertTrue(row["top_action"])

    def test_proof_is_deterministic_across_runs(self) -> None:
        again = PROOF.run_proof()

        self.assertEqual(again["results"], self.proof["results"])
        self.assertEqual(again["scorecard"], self.proof["scorecard"])

    def test_proof_runs_without_network_or_container_dependencies(self) -> None:
        source = (ROOT_DIR / "tests" / "quality" / "brain_orientation_proof.py").read_text(encoding="utf-8")

        for forbidden_import in ("import subprocess", "import socket", "import urllib", "import requests", "import docker"):
            with self.subTest(forbidden_import=forbidden_import):
                self.assertNotIn(forbidden_import, source)

    def test_main_exits_zero_while_the_baseline_holds(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = PROOF.main(["--no-write"])

        self.assertEqual(exit_code, 0)
        self.assertIn("BASELINE SCORECARD", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
