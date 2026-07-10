from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import agent_graph_algorithms as ALGO  # noqa: E402
from runtime_manager import agent_graph_engine as ENGINE  # noqa: E402


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


def _node(node_id: str, kind: str = "test", label: str | None = None) -> dict[str, object]:
    return {"id": node_id, "kind": kind, "label": label or node_id, "attrs": {}}


def _edge(source: str, target: str, kind: str = "depends_on", **attrs: object) -> dict[str, object]:
    return {"source": source, "target": target, "kind": kind, "attrs": attrs}


def _fixture_graph() -> dict[str, object]:
    return {
        "ok": True,
        "nodes": [
            _node("repo:app", "repo"),
            _node("task:compile", "task"),
            _node("task:build", "task"),
            _node("task:test", "task"),
            _node("service:api", "service"),
        ],
        "edges": [
            _edge("task:compile", "repo:app"),
            _edge("task:build", "task:compile"),
            _edge("task:test", "task:build"),
            _edge("service:api", "task:build"),
        ],
        "warnings": [{"code": "ADAPTER_TIMEOUT", "message": "bv timed out"}],
    }


class AgentGraphEngineTests(unittest.TestCase):
    def test_graph_command_payload_includes_graph_and_optional_algorithm(self) -> None:
        payload = ENGINE.graph_command_payload(_fixture_graph(), algorithm="all")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["graph"]["node_count"], 5)
        self.assertEqual(payload["graph"]["edge_count"], 4)
        self.assertEqual(payload["graph"]["warnings"][0]["code"], "ADAPTER_TIMEOUT")
        self.assertEqual(payload["algorithm"]["name"], "all")
        self.assertIn("critical_path", payload["algorithm"]["result"])
        self.assertTrue(all(action.startswith("python3 .env-manager/manage.py ") for action in payload["next_actions"]))
        _assert_elapsed_meta(self, payload)
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_cycles_algorithm_uses_shared_cycle_evidence(self) -> None:
        graph = {
            "nodes": [_node("service:a"), _node("service:b")],
            "edges": [_edge("service:a", "service:b"), _edge("service:b", "service:a")],
        }

        payload = ENGINE.graph_command_payload(graph, algorithm="cycles")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["algorithm"]["result"]["cycles"][0]["nodes"], ["service:a", "service:b"])

    def test_critical_path_and_min_unblock_are_stable(self) -> None:
        critical = ENGINE.graph_command_payload(_fixture_graph(), algorithm="critical-path")
        unblock = ENGINE.graph_command_payload(
            _fixture_graph(),
            algorithm="min-unblock",
            blocked_nodes=["task:test", "service:api"],
        )

        self.assertEqual(
            critical["algorithm"]["result"]["path"],
            ["repo:app", "task:compile", "task:build", "service:api"],
        )
        self.assertEqual(unblock["algorithm"]["result"]["selected"][0]["node_id"], "task:build")
        self.assertIn("blocked target", unblock["algorithm"]["result"]["selected"][0]["reason"])

    def test_dot_and_mermaid_serializers_are_deterministic(self) -> None:
        payload = ENGINE.graph_command_payload(_fixture_graph())
        dot = ENGINE.render_graph_payload(payload, "dot")
        mermaid = ENGINE.render_graph_payload(payload, "mermaid")
        text = ENGINE.render_graph_payload(payload, "text")

        self.assertTrue(dot.startswith("digraph skillbox_agent_graph"))
        self.assertIn('"task:build" -> "task:compile" [label="depends_on"];', dot)
        self.assertTrue(mermaid.startswith("flowchart TD"))
        self.assertIn('task_build -- "depends_on" --> task_compile', mermaid)
        self.assertEqual(text, "graph: 5 nodes, 4 edges, 1 warnings")

    def test_invalid_algorithm_returns_structured_invalid_argument(self) -> None:
        payload = ENGINE.graph_command_payload(_fixture_graph(), algorithm="pagerank")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        _assert_error_envelope(self, payload, "INVALID_ARGUMENT")
        self.assertIn("allowed", payload["error"]["details"])
        self.assertIn("allowed", payload["error"]["context"])
        self.assertTrue(all(action.startswith("python3 .env-manager/manage.py ") for action in payload["next_actions"]))
        _assert_elapsed_meta(self, payload)

    def test_unknown_node_returns_structured_unknown_node(self) -> None:
        payload = ENGINE.graph_command_payload(
            _fixture_graph(),
            algorithm="blast-radius",
            node_id="service:missing",
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "UNKNOWN_NODE")
        _assert_error_envelope(self, payload, "UNKNOWN_NODE")
        self.assertEqual(payload["error"]["details"]["node_id"], "service:missing")
        self.assertEqual(payload["error"]["context"]["node_id"], "service:missing")

    def test_invalid_algorithm_typo_suggests_critical_path(self) -> None:
        payload = ENGINE.graph_command_payload(_fixture_graph(), algorithm="critcal-path")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        _assert_error_envelope(self, payload, "INVALID_ARGUMENT")
        self.assertIn("critical-path", payload["error"]["details"]["suggestions"])
        self.assertTrue(
            any("critical-path" in action for action in payload.get("next_actions") or [])
        )

    def test_registered_toy_algorithm_runs_without_engine_edit(self) -> None:
        original_algorithms = dict(ALGO.ALGORITHMS)

        def run_toy(graph: dict[str, object], **_params: object) -> dict[str, object]:
            normalized = ALGO.normalize_graph(graph)
            return ALGO.algorithm_result(
                "toy-registry",
                f"counted {len(normalized.nodes)} node(s)",
                {"ok": True, "node_count": len(normalized.nodes)},
            )

        try:
            ALGO.register_algorithm(
                ALGO.AlgorithmSpec(
                    name="toy-registry",
                    summary="Toy algorithm used to prove registry-backed dispatch.",
                    run=run_toy,
                    params_schema={"type": "object", "properties": {}, "required": []},
                )
            )

            payload = ENGINE.graph_command_payload(_fixture_graph(), algorithm="toy-registry")

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["algorithm"]["name"], "toy-registry")
            self.assertEqual(payload["algorithm"]["result"]["algorithm"], "toy-registry")
            self.assertEqual(payload["algorithm"]["result"]["summary_line"], "counted 5 node(s)")
            self.assertEqual(payload["algorithm"]["result"]["data"]["node_count"], 5)
            self.assertEqual(payload["algorithm"]["result"]["node_count"], 5)
        finally:
            ALGO.ALGORITHMS.clear()
            ALGO.ALGORITHMS.update(original_algorithms)


if __name__ == "__main__":
    unittest.main()
