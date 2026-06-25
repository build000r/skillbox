from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import agent_graph_engine as ENGINE  # noqa: E402


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
        self.assertIn("allowed", payload["error"]["details"])
        self.assertTrue(all(action.startswith("python3 .env-manager/manage.py ") for action in payload["next_actions"]))

    def test_unknown_node_returns_structured_unknown_node(self) -> None:
        payload = ENGINE.graph_command_payload(
            _fixture_graph(),
            algorithm="blast-radius",
            node_id="service:missing",
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "UNKNOWN_NODE")
        self.assertEqual(payload["error"]["details"]["node_id"], "service:missing")


if __name__ == "__main__":
    unittest.main()
