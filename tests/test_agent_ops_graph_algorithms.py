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


def _payload(nodes: list[str], edges: list[dict[str, object]]) -> dict[str, object]:
    return {
        "nodes": [{"id": node_id, "kind": "test", "label": node_id, "attrs": {}} for node_id in nodes],
        "edges": edges,
    }


def _edge(source: str, target: str, kind: str = "depends_on", **attrs: object) -> dict[str, object]:
    return {"source": source, "target": target, "kind": kind, "attrs": attrs}


class AgentGraphAlgorithmTests(unittest.TestCase):
    def test_empty_graph_is_serializable_and_successful(self) -> None:
        result = ALGO.analyze_graph({"nodes": [], "edges": []})

        self.assertTrue(result["ok"])
        self.assertEqual(result["node_count"], 0)
        self.assertEqual(result["topology"]["order"], [])
        self.assertEqual(result["cycles"]["cycles"], [])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_disconnected_graph_keeps_stable_topological_layers(self) -> None:
        graph = _payload(
            ["task:build", "task:prepare", "service:api", "service:db", "repo:docs"],
            [
                _edge("task:build", "task:prepare"),
                _edge("service:api", "service:db"),
            ],
        )

        result = ALGO.topological_layers(graph)

        self.assertTrue(result["ok"])
        self.assertEqual(result["layers"][0], ["repo:docs", "service:db", "task:prepare"])
        self.assertEqual(result["layers"][1], ["service:api", "task:build"])

    def test_cyclic_graph_returns_scc_and_cycle_evidence(self) -> None:
        graph = _payload(
            ["service:a", "service:b", "service:c"],
            [
                _edge("service:a", "service:b"),
                _edge("service:b", "service:a"),
                _edge("service:c", "service:b"),
            ],
        )

        topo = ALGO.topological_layers(graph)
        scc = ALGO.strongly_connected_components(graph)
        cycles = ALGO.cycle_evidence(graph)

        self.assertFalse(topo["ok"])
        self.assertEqual(topo["cycle_nodes"], ["service:a", "service:b"])
        self.assertFalse(scc["ok"])
        self.assertIn(["service:a", "service:b"], scc["cyclic_components"])
        self.assertFalse(cycles["ok"])
        self.assertEqual(cycles["cycles"][0]["nodes"], ["service:a", "service:b"])
        self.assertIn("mutually depend", cycles["cycles"][0]["reason"])

    def test_weighted_shortest_path_prefers_low_cost_route(self) -> None:
        graph = _payload(
            ["service:a", "service:b", "service:c", "service:d"],
            [
                _edge("service:a", "service:b", "calls", weight=9),
                _edge("service:a", "service:c", "calls", weight=2),
                _edge("service:c", "service:b", "calls", weight=2),
                _edge("service:b", "service:d", "calls", weight=1),
                _edge("service:c", "service:d", "calls", weight=10),
            ],
        )

        result = ALGO.shortest_path(graph, "service:a", "service:d", edge_kinds=("calls",))

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], ["service:a", "service:c", "service:b", "service:d"])
        self.assertEqual(result["cost"], 5.0)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_blocked_graph_reports_blast_radius_and_min_unblock_reasons(self) -> None:
        graph = _payload(
            ["task:test", "task:build", "task:compile", "repo:app", "service:api"],
            [
                _edge("task:compile", "repo:app"),
                _edge("task:build", "task:compile"),
                _edge("task:test", "task:build"),
                _edge("service:api", "task:build"),
            ],
        )

        blast = ALGO.blast_radius(graph, "task:compile")
        unblock = ALGO.min_unblock_set(graph, blocked_nodes=["task:test", "service:api"])

        self.assertTrue(blast["ok"])
        self.assertEqual(
            [item["node_id"] for item in blast["affected"]],
            ["task:build", "service:api", "task:test"],
        )
        self.assertTrue(all(item["reason"] for item in blast["affected"]))
        self.assertTrue(unblock["ok"])
        self.assertEqual(unblock["selected"][0]["node_id"], "task:build")
        self.assertEqual(unblock["selected"][0]["coverage"], 2)
        self.assertIn("blocked target", unblock["selected"][0]["reason"])
        self.assertEqual(json.loads(json.dumps(unblock)), unblock)


if __name__ == "__main__":
    unittest.main()
