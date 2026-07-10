from __future__ import annotations

import random
import sys
import unittest
from collections import deque
from pathlib import Path
from typing import Any

from tests.helpers import make_runtime_model


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import agent_graph_algorithms as ALGO  # noqa: E402


SEED = 20260704
CASES = 50


def _edge(source: str, target: str, kind: str = "depends_on") -> dict[str, Any]:
    return {"source": source, "target": target, "kind": kind, "attrs": {}}


def _payload(nodes: list[str], edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "nodes": [{"id": node_id, "kind": "test", "label": node_id, "attrs": {}} for node_id in nodes],
        "edges": edges,
    }


def _runtime_fixture_nodes() -> tuple[str, ...]:
    model = make_runtime_model()
    nodes: list[str] = []
    nodes.extend(f"service:{service['id']}" for service in model["services"])
    nodes.extend(f"task:{task['id']}" for task in model["tasks"])
    nodes.extend(f"repo:{repo['id']}" for repo in model["repos"])
    nodes.extend(f"check:{check['id']}" for check in model["checks"])
    return tuple(nodes)


RUNTIME_FIXTURE_NODES = _runtime_fixture_nodes()


def _random_nodes(rng: random.Random, *, min_count: int = 4, max_count: int = 12) -> list[str]:
    count = rng.randint(min_count, max_count)
    fixture_count = min(len(RUNTIME_FIXTURE_NODES), max(1, count // 3))
    nodes = list(RUNTIME_FIXTURE_NODES[:fixture_count])
    nodes.extend(f"node:{index:02d}" for index in range(count - fixture_count))
    rng.shuffle(nodes)
    return nodes


def _random_dag(rng: random.Random) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    nodes = _random_nodes(rng)
    edges: list[dict[str, Any]] = []
    probability = rng.uniform(0.15, 0.45)
    for source_index, source in enumerate(nodes):
        for target in nodes[:source_index]:
            if rng.random() < probability:
                edges.append(_edge(source, target))
    return _payload(nodes, edges), nodes, edges


def _plant_cycle(nodes: list[str], edges: list[dict[str, Any]]) -> tuple[dict[str, Any], set[str]]:
    planted = nodes[:3]
    cycle_edges = [
        _edge(planted[0], planted[1]),
        _edge(planted[1], planted[2]),
        _edge(planted[2], planted[0]),
    ]
    return _payload(nodes, edges + cycle_edges), set(planted)


def _plant_merge(nodes: list[str], edges: list[dict[str, Any]]) -> tuple[dict[str, Any], set[str]]:
    planted = nodes[:2]
    merge_edges = [_edge(planted[0], planted[1]), _edge(planted[1], planted[0])]
    return _payload(nodes, edges + merge_edges), set(planted)


def _flatten(layers: list[list[str]]) -> list[str]:
    return [node_id for layer in layers for node_id in layer]


def _dag_depth(nodes: list[str], edges: list[dict[str, Any]]) -> int:
    dependents: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        dependents[edge["target"]].append(edge["source"])
    distances = {node_id: 1 for node_id in nodes}
    for node_id in nodes:
        for dependent in dependents[node_id]:
            distances[dependent] = max(distances[dependent], distances[node_id] + 1)
    return max(distances.values(), default=0)


def _dependency_closure(
    graph: dict[str, Any],
    start: str,
    *,
    removed: set[str] | None = None,
) -> set[str]:
    removed = removed or set()
    adjacency: dict[str, list[str]] = {node["id"]: [] for node in graph["nodes"]}
    for edge in graph["edges"]:
        if edge["source"] in removed or edge["target"] in removed:
            continue
        adjacency.setdefault(edge["source"], []).append(edge["target"])
    seen: set[str] = set()
    stack = list(adjacency.get(start, ()))
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        stack.extend(adjacency.get(node_id, ()))
    return seen


def _dependent_descendants(graph: dict[str, Any], start: str) -> set[str]:
    reverse_adjacency: dict[str, list[str]] = {node["id"]: [] for node in graph["nodes"]}
    for edge in graph["edges"]:
        reverse_adjacency.setdefault(edge["target"], []).append(edge["source"])
    seen: set[str] = set()
    queue = deque(reverse_adjacency.get(start, ()))
    while queue:
        node_id = queue.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)
        queue.extend(reverse_adjacency.get(node_id, ()))
    return seen


def _random_unblock_graph(rng: random.Random) -> tuple[dict[str, Any], list[str], set[str]]:
    blocker_count = rng.randint(1, 5)
    target_count = rng.randint(1, 8)
    noise_count = rng.randint(0, 4)
    blockers = [f"blocker:{index}" for index in range(blocker_count)]
    targets = [f"target:{index}" for index in range(target_count)]
    noise = [f"noise:{index}" for index in range(noise_count)]
    edges = [_edge(target, rng.choice(blockers)) for target in targets]
    for index in range(1, len(noise)):
        if rng.random() < 0.5:
            edges.append(_edge(noise[index], noise[index - 1]))
    return _payload(blockers + targets + noise, edges), targets, set(blockers)


class AgentOpsGraphPropertyTests(unittest.TestCase):
    def test_topological_layers_are_valid_for_random_dags(self) -> None:
        rng = random.Random(SEED)
        for case in range(CASES):
            with self.subTest(case=case):
                graph, nodes, edges = _random_dag(rng)
                result = ALGO.topological_layers(graph)
                flat_layers = _flatten(result["layers"])
                position = {node_id: index for index, node_id in enumerate(result["order"])}

                self.assertTrue(result["ok"])
                self.assertEqual(result["order"], flat_layers)
                self.assertEqual(len(result["order"]), len(set(result["order"])))
                self.assertEqual(set(result["order"]), set(nodes))
                for edge in edges:
                    self.assertLess(position[edge["target"]], position[edge["source"]])

    def test_cycle_evidence_matches_random_dags_and_planted_cycles(self) -> None:
        rng = random.Random(SEED + 1)
        for case in range(CASES):
            with self.subTest(case=case):
                dag, nodes, edges = _random_dag(rng)
                dag_result = ALGO.cycle_evidence(dag)
                cyclic_graph, planted = _plant_cycle(nodes, edges)
                cyclic_result = ALGO.cycle_evidence(cyclic_graph)

                self.assertTrue(dag_result["ok"])
                self.assertEqual(dag_result["cycles"], [])
                self.assertFalse(cyclic_result["ok"])
                self.assertTrue(
                    any(planted.issubset(set(cycle["nodes"])) for cycle in cyclic_result["cycles"])
                )

    def test_sccs_are_singletons_for_dags_and_detect_planted_merges(self) -> None:
        rng = random.Random(SEED + 2)
        for case in range(CASES):
            with self.subTest(case=case):
                dag, nodes, edges = _random_dag(rng)
                dag_result = ALGO.strongly_connected_components(dag)
                merged_graph, planted = _plant_merge(nodes, edges)
                merged_result = ALGO.strongly_connected_components(merged_graph)

                self.assertTrue(dag_result["ok"])
                self.assertEqual(len(dag_result["components"]), len(nodes))
                self.assertTrue(all(len(component) == 1 for component in dag_result["components"]))
                self.assertFalse(merged_result["ok"])
                self.assertTrue(
                    any(planted.issubset(set(component)) for component in merged_result["cyclic_components"])
                )

    def test_critical_path_is_bounded_by_random_dag_depth(self) -> None:
        rng = random.Random(SEED + 3)
        for case in range(CASES):
            with self.subTest(case=case):
                graph, nodes, edges = _random_dag(rng)
                result = ALGO.critical_path(graph)

                self.assertTrue(result["ok"])
                self.assertLessEqual(result["length"], _dag_depth(nodes, edges))
                self.assertTrue(set(result["path"]).issubset(set(nodes)))

    def test_min_unblock_set_clears_blockers_for_random_targets(self) -> None:
        rng = random.Random(SEED + 4)
        for case in range(CASES):
            with self.subTest(case=case):
                graph, targets, blockers = _random_unblock_graph(rng)
                result = ALGO.min_unblock_set(graph, blocked_nodes=targets)
                selected = {item["node_id"] for item in result["selected"]}

                self.assertTrue(result["ok"])
                self.assertEqual(result["unresolved"], [])
                for target in targets:
                    remaining = _dependency_closure(graph, target, removed=selected)
                    self.assertFalse(remaining.intersection(blockers - selected))

    def test_blast_radius_is_subset_of_random_graph_descendants(self) -> None:
        rng = random.Random(SEED + 5)
        for case in range(CASES):
            with self.subTest(case=case):
                graph, nodes, _edges = _random_dag(rng)
                node_id = rng.choice(nodes)
                result = ALGO.blast_radius(graph, node_id)
                affected = {item["node_id"] for item in result["affected"]}

                self.assertTrue(result["ok"])
                self.assertTrue(affected.issubset(_dependent_descendants(graph, node_id)))
                self.assertTrue(affected.issubset(set(nodes)))


if __name__ == "__main__":
    unittest.main()
