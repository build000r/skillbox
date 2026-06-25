from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import agent_decisions as DECISIONS  # noqa: E402
from runtime_manager.text_renderers import explain_brain_text_lines  # noqa: E402


def _assert_elapsed_meta(testcase: unittest.TestCase, payload: dict[str, object]) -> None:
    meta = payload.get("meta")
    testcase.assertIsInstance(meta, dict)
    elapsed = meta.get("elapsed_ms") if isinstance(meta, dict) else None
    testcase.assertIsInstance(elapsed, (int, float))
    testcase.assertNotIsInstance(elapsed, bool)
    testcase.assertGreaterEqual(float(elapsed), 0.0)


def _assert_error_envelope(testcase: unittest.TestCase, payload: dict[str, object], code: str) -> None:
    testcase.assertIs(payload["ok"], False)
    error = payload.get("error")
    testcase.assertIsInstance(error, dict)
    testcase.assertEqual(error["code"], code)
    testcase.assertEqual(error["type"], code)
    testcase.assertEqual(payload["error_code"], code)
    testcase.assertIn("deprecation", payload)


def _node(node_id: str, kind: str, label: str | None = None, **attrs: object) -> dict[str, object]:
    return {"id": node_id, "kind": kind, "label": label or node_id, "attrs": attrs}


def _edge(source: str, target: str, kind: str = "depends_on") -> dict[str, object]:
    return {"source": source, "target": target, "kind": kind, "attrs": {}}


def _graph(warnings: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "ok": not warnings,
        "nodes": [
            _node("service:api", "service"),
            _node("check:doctor", "check"),
            _node("skill:domain-planner", "skill"),
            _node("mcp_tool:skillbox_next", "mcp_tool"),
            _node("bead:ready-1", "bead", "Ready bead", status="open", priority=1),
            _node("command:brain.next", "command", "Recommend next actions"),
        ],
        "edges": [
            _edge("service:api", "check:doctor", "checked_by"),
            _edge("command:brain.next", "mcp_tool:skillbox_next", "exposes"),
        ],
        "warnings": warnings or [],
    }


def _evidence(overall: str = "green", blocked: list[str] | None = None) -> dict[str, object]:
    return {
        "kind": "runtime-evidence",
        "overall": overall,
        "blocked_conditions": blocked or [],
        "next_actions": ["doctor --format json", "status --format json"],
        "sections": {"doctor": {"status": "pass"}},
    }


class AgentDecisionTests(unittest.TestCase):
    def test_next_ranks_ready_work_with_reasons_commands_validations_and_evidence(self) -> None:
        adapters = {
            "br_ready": {
                "ok": True,
                "payload": [{"id": "ready-1", "title": "Ship graph command", "priority": 1}],
                "warnings": [],
            },
            "bv_triage": {
                "ok": True,
                "payload": {"recommendations": [{"id": "ready-1", "claim_command": "br update ready-1 --status=in_progress"}]},
                "warnings": [],
            },
        }

        payload = DECISIONS.next_action_payload(_graph(), adapters=adapters, evidence=_evidence())

        self.assertTrue(payload["ok"])
        first = payload["recommendations"][0]
        self.assertEqual(first["id"], "claim-ready:ready-1")
        self.assertGreater(first["score"], 700)
        self.assertTrue(first["reasons"])
        self.assertEqual(first["commands"], ["br update ready-1 --status=in_progress"])
        self.assertIn("br show ready-1 --json", first["validations"])
        self.assertEqual(first["evidence"][0]["source"], "br_ready")
        self.assertEqual(payload["disagreements"], [])
        _assert_elapsed_meta(self, payload)
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_next_handles_no_ready_and_blocked_work(self) -> None:
        adapters = {
            "br_ready": {"ok": True, "payload": [], "warnings": []},
            "br_open": {
                "ok": True,
                "payload": [
                    {
                        "id": "blocked-1",
                        "title": "Blocked issue",
                        "dependencies": [{"id": "dep-1", "status": "open"}],
                    }
                ],
                "warnings": [],
            },
        }

        payload = DECISIONS.next_action_payload(_graph(), adapters=adapters, evidence=_evidence())
        ids = [item["id"] for item in payload["recommendations"]]

        self.assertIn("clear-blockers:blocked-1", ids)
        self.assertIn("inspect-work-queue", ids)

    def test_next_prioritizes_environment_blockers_and_broken_model(self) -> None:
        adapters = {
            "evidence": {
                "ok": False,
                "status": "degraded",
                "warnings": [{"code": "EVIDENCE_COLLECTION_FAILED", "message": "bad runtime model"}],
            }
        }

        payload = DECISIONS.next_action_payload(
            _graph(warnings=[{"code": "SERVICE_DEPENDENCY_CYCLE", "message": "cycle"}]),
            adapters=adapters,
            evidence=_evidence("red", ["doctor: 1 check(s) failing: runtime"]),
        )

        self.assertEqual(payload["recommendations"][0]["id"], "stabilize-runtime-evidence")
        ids = [item["id"] for item in payload["recommendations"]]
        self.assertIn("inspect-runtime-graph", ids)
        self.assertIn("repair-adapter:evidence", ids)

    def test_next_surfaces_bv_br_disagreement(self) -> None:
        adapters = {
            "br_ready": {"ok": True, "payload": [{"id": "ready-1", "priority": 2}], "warnings": []},
            "bv_triage": {
                "ok": True,
                "payload": {
                    "recommendations": [
                        {"id": "blocked-2", "claim_command": "br update blocked-2 --status=in_progress"}
                    ]
                },
                "warnings": [],
            },
        }

        payload = DECISIONS.next_action_payload(_graph(), adapters=adapters, evidence=_evidence())

        self.assertEqual(payload["disagreements"][0]["code"], "BV_READY_DISAGREEMENT")
        self.assertEqual(payload["disagreements"][0]["issue_id"], "blocked-2")

    def test_explain_handles_declared_node_kinds_and_commands(self) -> None:
        adapters = {
            "br_ready": {
                "ok": True,
                "payload": [{"id": "ready-1", "title": "Ready bead", "priority": 1}],
                "warnings": [],
            }
        }
        for target in (
            "check:doctor",
            "service:api",
            "skill:domain-planner",
            "mcp_tool:skillbox_next",
            "bead:ready-1",
            "command:brain.next",
            "brain.next",
            "next",
            "snap",
        ):
            with self.subTest(target=target):
                payload = DECISIONS.explain_payload(_graph(), target, adapters=adapters)
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["summary"])
                self.assertIn("relationships", payload)
                _assert_elapsed_meta(self, payload)
        bead = DECISIONS.explain_payload(_graph(), "bead:ready-1", adapters=adapters)
        self.assertTrue(any(item["source"] == "br_ready" for item in bead["evidence"]))

    def test_explain_unknown_node_is_structured(self) -> None:
        payload = DECISIONS.explain_payload(_graph(), "service:missing")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "UNKNOWN_NODE")
        _assert_error_envelope(self, payload, "UNKNOWN_NODE")
        _assert_elapsed_meta(self, payload)

    def test_explain_prefixed_command_falls_back_to_registry_without_graph_node(self) -> None:
        payload = DECISIONS.explain_payload({"nodes": [], "edges": []}, "command:brain.next")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "brain.next")
        self.assertEqual(payload["kind"], "command")

    def test_explain_bare_pulse_resolves_with_resolved_from(self) -> None:
        graph = _graph()
        graph["nodes"].append(_node("service:pulse", "service", "Pulse daemon"))

        payload = DECISIONS.explain_payload(graph, "pulse")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "service:pulse")
        self.assertEqual(payload["resolved_from"], "pulse")

    def test_explain_typo_pluse_returns_unknown_with_pulse_suggestion(self) -> None:
        graph = _graph()
        graph["nodes"].append(_node("service:pulse", "service", "Pulse daemon"))

        payload = DECISIONS.explain_payload(graph, "pluse")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "UNKNOWN_NODE")
        _assert_error_envelope(self, payload, "UNKNOWN_NODE")
        suggestion_ids = [item["id"] for item in payload.get("suggestions") or []]
        self.assertIn("service:pulse", suggestion_ids)
        context_suggestion_ids = [item["id"] for item in payload["error"]["context"]["suggestions"]]
        self.assertIn("service:pulse", context_suggestion_ids)

    def test_explain_ambiguous_bare_word_lists_candidates_without_guessing(self) -> None:
        graph = _graph()
        graph["nodes"].append(_node("check:api", "check", "API check"))

        payload = DECISIONS.explain_payload(graph, "api")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "AMBIGUOUS_NODE")
        _assert_error_envelope(self, payload, "AMBIGUOUS_NODE")
        candidate_ids = [item["id"] for item in payload["error"]["details"]["candidates"]]
        self.assertEqual(sorted(candidate_ids), ["check:api", "service:api"])
        context_ids = [item["id"] for item in payload["error"]["context"]["candidates"]]
        self.assertEqual(sorted(context_ids), ["check:api", "service:api"])

    def test_explain_text_renderer_prints_copy_pasteable_suggestions(self) -> None:
        graph = _graph()
        graph["nodes"].append(_node("service:pulse", "service", "Pulse daemon"))
        payload = DECISIONS.explain_payload(graph, "pluse")
        lines = explain_brain_text_lines(payload)

        self.assertTrue(any("service:pulse" in line for line in lines))
        self.assertTrue(any("manage.py explain service:pulse" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
