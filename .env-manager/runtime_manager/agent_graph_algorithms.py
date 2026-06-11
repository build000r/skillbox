"""Deterministic graph algorithms for the agent operations brain."""
from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .agent_graph import GRAPH_SCHEMA_VERSION, AgentGraph

ALGORITHMS_SCHEMA_VERSION = "2026-06-11+agent_ops_brain.algorithms"
DEFAULT_BLOCKER_EDGE_KINDS = ("depends_on", "blocked_by")


@dataclass(frozen=True)
class NormalizedEdge:
    source: str
    target: str
    kind: str
    attrs: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "attrs": dict(sorted(self.attrs.items())),
        }


@dataclass(frozen=True)
class NormalizedGraph:
    nodes: Mapping[str, Mapping[str, Any]]
    edges: tuple[NormalizedEdge, ...]


def normalize_graph(graph: AgentGraph | Mapping[str, Any]) -> NormalizedGraph:
    """Normalize an AgentGraph or graph payload into sorted node/edge maps."""
    payload = graph.to_payload() if isinstance(graph, AgentGraph) else graph
    nodes: dict[str, Mapping[str, Any]] = {}
    for node in payload.get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        node_id = str(node.get("id") or "").strip()
        if node_id:
            nodes[node_id] = dict(node)

    edges: list[NormalizedEdge] = []
    for edge in payload.get("edges") or []:
        if not isinstance(edge, Mapping):
            continue
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        kind = str(edge.get("kind") or "").strip()
        if not source or not target or not kind:
            continue
        attrs = edge.get("attrs")
        edges.append(
            NormalizedEdge(
                source=source,
                target=target,
                kind=kind,
                attrs=dict(attrs) if isinstance(attrs, Mapping) else {},
            )
        )
        nodes.setdefault(source, {"id": source, "kind": "unknown", "attrs": {}})
        nodes.setdefault(target, {"id": target, "kind": "unknown", "attrs": {}})

    return NormalizedGraph(
        nodes=dict(sorted(nodes.items())),
        edges=tuple(sorted(edges, key=lambda item: (item.source, item.kind, item.target))),
    )


def _edge_kinds(edge_kinds: Iterable[str] | None) -> set[str] | None:
    if edge_kinds is None:
        return None
    return {str(kind) for kind in edge_kinds if str(kind)}


def _filtered_edges(graph: NormalizedGraph, edge_kinds: Iterable[str] | None) -> list[NormalizedEdge]:
    allowed = _edge_kinds(edge_kinds)
    if allowed is None:
        return list(graph.edges)
    return [edge for edge in graph.edges if edge.kind in allowed]


def _outgoing(
    graph: NormalizedGraph,
    edge_kinds: Iterable[str] | None = None,
    *,
    reverse: bool = False,
) -> dict[str, list[tuple[str, NormalizedEdge]]]:
    adjacency: dict[str, list[tuple[str, NormalizedEdge]]] = {node_id: [] for node_id in graph.nodes}
    for edge in _filtered_edges(graph, edge_kinds):
        source, target = (edge.target, edge.source) if reverse else (edge.source, edge.target)
        adjacency.setdefault(source, []).append((target, edge))
        adjacency.setdefault(target, [])
    for node_edges in adjacency.values():
        node_edges.sort(key=lambda item: (item[0], item[1].kind, item[1].source, item[1].target))
    return dict(sorted(adjacency.items()))


def _edge_weight(edge: NormalizedEdge) -> float:
    for key in ("weight", "cost", "distance"):
        raw_value = edge.attrs.get(key)
        if isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, (int, float)):
            value = float(raw_value)
            return value if value >= 0 else 1.0
        if isinstance(raw_value, str):
            try:
                value = float(raw_value)
            except ValueError:
                continue
            return value if value >= 0 else 1.0
    return 1.0


def topological_layers(
    graph: AgentGraph | Mapping[str, Any],
    *,
    edge_kinds: Iterable[str] = ("depends_on",),
) -> dict[str, Any]:
    """Return dependency-first topological order and cycle evidence.

    A ``depends_on`` edge is stored as dependent -> dependency, so Kahn's graph
    is reversed for topological ordering: dependencies appear before dependents.
    """
    normalized = normalize_graph(graph)
    dependency_edges = _filtered_edges(normalized, edge_kinds)
    dependents: dict[str, set[str]] = {node_id: set() for node_id in normalized.nodes}
    indegree: dict[str, int] = {node_id: 0 for node_id in normalized.nodes}
    for edge in dependency_edges:
        dependents.setdefault(edge.target, set()).add(edge.source)
        dependents.setdefault(edge.source, set())
        indegree[edge.source] = indegree.get(edge.source, 0) + 1
        indegree.setdefault(edge.target, 0)

    ready = sorted(node_id for node_id, count in indegree.items() if count == 0)
    order: list[str] = []
    layers: list[list[str]] = []
    while ready:
        layer = ready
        layers.append(layer)
        next_ready: list[str] = []
        for node_id in layer:
            order.append(node_id)
            for dependent in sorted(dependents.get(node_id, ())):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    next_ready.append(dependent)
        ready = sorted(next_ready)

    residual_nodes = sorted(node_id for node_id, count in indegree.items() if count > 0)
    scc = strongly_connected_components(
        normalized_to_payload(normalized),
        edge_kinds=edge_kinds,
    )
    cycle_nodes = sorted(
        {
            node_id
            for component in scc["cyclic_components"]
            for node_id in component
        }
    )
    return {
        "ok": not residual_nodes,
        "edge_kinds": sorted(_edge_kinds(edge_kinds) or ()),
        "order": order,
        "layers": layers,
        "cycle_nodes": cycle_nodes,
        "blocked_by_cycle_nodes": residual_nodes,
        "reason": (
            "topological order completed"
            if not residual_nodes
            else f"{len(residual_nodes)} node(s) could not be topologically ordered"
        ),
    }


def strongly_connected_components(
    graph: AgentGraph | Mapping[str, Any],
    *,
    edge_kinds: Iterable[str] | None = ("depends_on",),
) -> dict[str, Any]:
    """Return Tarjan SCCs with deterministic traversal and cycle evidence."""
    normalized = normalize_graph(graph)
    adjacency = _outgoing(normalized, edge_kinds)
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indexes[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)

        for target, _edge in adjacency.get(node_id, ()):
            if target not in indexes:
                visit(target)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[target])
            elif target in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indexes[target])

        if lowlinks[node_id] == indexes[node_id]:
            component: list[str] = []
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node_id:
                    break
            components.append(sorted(component))

    for node_id in sorted(normalized.nodes):
        if node_id not in indexes:
            visit(node_id)

    self_loop_nodes = {
        edge.source for edge in _filtered_edges(normalized, edge_kinds) if edge.source == edge.target
    }
    cyclic_components = [
        component
        for component in components
        if len(component) > 1 or (component and component[0] in self_loop_nodes)
    ]
    components.sort(key=lambda component: (component[0] if component else "", len(component)))
    cyclic_components.sort(key=lambda component: (component[0] if component else "", len(component)))
    return {
        "ok": not cyclic_components,
        "edge_kinds": sorted(_edge_kinds(edge_kinds) or []),
        "components": components,
        "cyclic_components": cyclic_components,
        "reason": (
            "no strongly connected dependency cycles"
            if not cyclic_components
            else f"{len(cyclic_components)} cyclic component(s) detected"
        ),
    }


def cycle_evidence(
    graph: AgentGraph | Mapping[str, Any],
    *,
    edge_kinds: Iterable[str] | None = ("depends_on",),
) -> dict[str, Any]:
    """Return compact cycle evidence derived from SCCs."""
    normalized = normalize_graph(graph)
    scc = strongly_connected_components(normalized_to_payload(normalized), edge_kinds=edge_kinds)
    cyclic_components = scc["cyclic_components"]
    edges_by_component: list[dict[str, Any]] = []
    allowed = _edge_kinds(edge_kinds)
    for component in cyclic_components:
        members = set(component)
        component_edges = [
            edge.to_payload()
            for edge in normalized.edges
            if edge.source in members
            and edge.target in members
            and (allowed is None or edge.kind in allowed)
        ]
        edges_by_component.append(
            {
                "nodes": component,
                "edges": component_edges,
                "reason": f"{len(component)} node(s) mutually depend on each other",
            }
        )
    return {
        "ok": not cyclic_components,
        "edge_kinds": sorted(_edge_kinds(edge_kinds) or []),
        "cycles": edges_by_component,
        "reason": scc["reason"],
    }


def shortest_path(
    graph: AgentGraph | Mapping[str, Any],
    source: str,
    target: str,
    *,
    edge_kinds: Iterable[str] | None = None,
    reverse: bool = False,
) -> dict[str, Any]:
    """Return the deterministic weighted shortest path between two nodes."""
    normalized = normalize_graph(graph)
    source = str(source).strip()
    target = str(target).strip()
    if source not in normalized.nodes or target not in normalized.nodes:
        missing = [node_id for node_id in (source, target) if node_id not in normalized.nodes]
        return {
            "ok": False,
            "source": source,
            "target": target,
            "path": [],
            "cost": None,
            "missing": missing,
            "reason": f"missing node(s): {', '.join(missing)}",
        }

    adjacency = _outgoing(normalized, edge_kinds, reverse=reverse)
    queue: list[tuple[float, int, str, list[str], list[NormalizedEdge]]] = [(0.0, 0, source, [source], [])]
    best: dict[str, float] = {source: 0.0}
    sequence = 1
    while queue:
        cost, _seq, node_id, path, path_edges = heapq.heappop(queue)
        if cost > best.get(node_id, float("inf")):
            continue
        if node_id == target:
            return {
                "ok": True,
                "source": source,
                "target": target,
                "path": path,
                "edge_count": len(path_edges),
                "cost": cost,
                "edges": [edge.to_payload() for edge in path_edges],
                "reason": f"found path with cost {cost:g}",
            }
        for next_node, edge in adjacency.get(node_id, ()):
            next_cost = cost + _edge_weight(edge)
            if next_cost < best.get(next_node, float("inf")):
                best[next_node] = next_cost
                heapq.heappush(
                    queue,
                    (next_cost, sequence, next_node, path + [next_node], path_edges + [edge]),
                )
                sequence += 1

    return {
        "ok": False,
        "source": source,
        "target": target,
        "path": [],
        "cost": None,
        "missing": [],
        "reason": f"no path from {source} to {target}",
    }


def blast_radius(
    graph: AgentGraph | Mapping[str, Any],
    node_id: str,
    *,
    edge_kinds: Iterable[str] = DEFAULT_BLOCKER_EDGE_KINDS,
    max_depth: int | None = None,
) -> dict[str, Any]:
    """Return nodes that are downstream of a blocker/dependency."""
    normalized = normalize_graph(graph)
    node_id = str(node_id).strip()
    if node_id not in normalized.nodes:
        return {
            "ok": False,
            "node_id": node_id,
            "affected_count": 0,
            "affected": [],
            "reason": f"missing node: {node_id}",
        }

    adjacency = _outgoing(normalized, edge_kinds, reverse=True)
    visited = {node_id}
    queue: deque[tuple[str, int, list[str]]] = deque([(node_id, 0, [node_id])])
    affected: list[dict[str, Any]] = []
    while queue:
        current, depth, path = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for dependent, edge in adjacency.get(current, ()):
            if dependent in visited:
                continue
            visited.add(dependent)
            next_path = path + [dependent]
            affected.append(
                {
                    "node_id": dependent,
                    "distance": depth + 1,
                    "via": edge.to_payload(),
                    "path": next_path,
                    "reason": f"{dependent} depends on {node_id}",
                }
            )
            queue.append((dependent, depth + 1, next_path))

    affected.sort(key=lambda item: (item["distance"], item["node_id"]))
    return {
        "ok": True,
        "node_id": node_id,
        "edge_kinds": sorted(_edge_kinds(edge_kinds) or ()),
        "affected_count": len(affected),
        "affected": affected,
        "reason": f"{len(affected)} downstream node(s) depend on {node_id}",
    }


def _infer_blocked_nodes(normalized: NormalizedGraph) -> list[str]:
    blocked_statuses = {
        "blocked",
        "fail",
        "failed",
        "missing",
        "timeout",
        "unavailable",
        "operator_required",
    }
    blocked: list[str] = []
    for node_id, node in normalized.nodes.items():
        attrs = node.get("attrs") if isinstance(node, Mapping) else None
        status = ""
        if isinstance(attrs, Mapping):
            status = str(attrs.get("status") or attrs.get("state") or "").strip().lower()
        if status in blocked_statuses:
            blocked.append(node_id)
    return sorted(blocked)


def _dependency_closure(
    normalized: NormalizedGraph,
    node_id: str,
    *,
    edge_kinds: Iterable[str],
) -> set[str]:
    adjacency = _outgoing(normalized, edge_kinds)
    seen: set[str] = set()
    stack = [node_id]
    while stack:
        current = stack.pop()
        for blocker, _edge in reversed(adjacency.get(current, ())):
            if blocker in seen:
                continue
            seen.add(blocker)
            stack.append(blocker)
    seen.discard(node_id)
    return seen


def _dependency_distances(
    normalized: NormalizedGraph,
    node_id: str,
    *,
    edge_kinds: Iterable[str],
) -> dict[str, int]:
    adjacency = _outgoing(normalized, edge_kinds)
    distances: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque([(node_id, 0)])
    while queue:
        current, distance = queue.popleft()
        for blocker, _edge in adjacency.get(current, ()):
            if blocker in distances:
                continue
            distances[blocker] = distance + 1
            queue.append((blocker, distance + 1))
    distances.pop(node_id, None)
    return distances


def min_unblock_set(
    graph: AgentGraph | Mapping[str, Any],
    *,
    blocked_nodes: Iterable[str] | None = None,
    edge_kinds: Iterable[str] = DEFAULT_BLOCKER_EDGE_KINDS,
) -> dict[str, Any]:
    """Return a deterministic greedy set of blockers to clear first."""
    normalized = normalize_graph(graph)
    requested = (
        sorted({str(node_id).strip() for node_id in blocked_nodes if str(node_id).strip()})
        if blocked_nodes is not None
        else _infer_blocked_nodes(normalized)
    )
    missing = [node_id for node_id in requested if node_id not in normalized.nodes]
    blocked = [node_id for node_id in requested if node_id in normalized.nodes]

    distances_by_target: dict[str, dict[str, int]] = {
        node_id: _dependency_distances(normalized, node_id, edge_kinds=edge_kinds)
        for node_id in blocked
    }
    blockers_by_target: dict[str, set[str]] = {
        node_id: set(distances)
        for node_id, distances in distances_by_target.items()
    }
    uncovered = {node_id for node_id, blockers in blockers_by_target.items() if blockers}
    selected: list[dict[str, Any]] = []
    while uncovered:
        coverage_by_blocker: dict[str, list[str]] = {}
        for target_id in sorted(uncovered):
            for blocker_id in sorted(blockers_by_target[target_id]):
                coverage_by_blocker.setdefault(blocker_id, []).append(target_id)
        if not coverage_by_blocker:
            break
        best_blocker, best_targets = min(
            coverage_by_blocker.items(),
            key=lambda item: (
                -len(item[1]),
                sum(distances_by_target[target_id].get(item[0], 999999) for target_id in item[1]),
                item[0],
            ),
        )
        best_targets = sorted(best_targets)
        selected.append(
            {
                "node_id": best_blocker,
                "unblocks": best_targets,
                "coverage": len(best_targets),
                "reason": f"{best_blocker} is on blocker paths for {len(best_targets)} blocked target(s)",
            }
        )
        uncovered.difference_update(best_targets)

    leaf_blocked = sorted(node_id for node_id, blockers in blockers_by_target.items() if not blockers)
    return {
        "ok": not missing,
        "blocked_nodes": blocked,
        "missing": missing,
        "selected": selected,
        "leaf_blocked": leaf_blocked,
        "unresolved": sorted(uncovered),
        "reason": (
            "no blocked nodes supplied or inferred"
            if not requested
            else f"selected {len(selected)} unblock candidate(s) for {len(blocked)} blocked node(s)"
        ),
    }


def critical_path(
    graph: AgentGraph | Mapping[str, Any],
    *,
    edge_kinds: Iterable[str] = ("depends_on",),
) -> dict[str, Any]:
    """Return the longest dependency-first path in an acyclic graph."""
    normalized = normalize_graph(graph)
    topo = topological_layers(normalized_to_payload(normalized), edge_kinds=edge_kinds)
    if not topo["ok"]:
        return {
            "ok": False,
            "path": [],
            "length": 0,
            "cycle_nodes": topo["cycle_nodes"],
            "blocked_by_cycle_nodes": topo["blocked_by_cycle_nodes"],
            "reason": "critical path is undefined while dependency cycles exist",
        }

    dependents = _outgoing(normalized, edge_kinds, reverse=True)
    distance: dict[str, int] = {node_id: 1 for node_id in normalized.nodes}
    predecessor: dict[str, str | None] = {node_id: None for node_id in normalized.nodes}
    for node_id in topo["order"]:
        for dependent, _edge in dependents.get(node_id, ()):
            candidate = distance[node_id] + 1
            current = distance.get(dependent, 1)
            if candidate > current or (
                candidate == current and node_id < str(predecessor.get(dependent) or "\uffff")
            ):
                distance[dependent] = candidate
                predecessor[dependent] = node_id

    if not distance:
        return {"ok": True, "path": [], "length": 0, "reason": "graph is empty"}

    endpoint = min(
        distance,
        key=lambda node_id: (-distance[node_id], node_id),
    )
    path: list[str] = []
    current: str | None = endpoint
    while current is not None:
        path.append(current)
        current = predecessor.get(current)
    path.reverse()
    return {
        "ok": True,
        "path": path,
        "length": len(path),
        "reason": f"longest dependency chain contains {len(path)} node(s)",
    }


def normalized_to_payload(graph: NormalizedGraph) -> dict[str, Any]:
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "nodes": [dict(node) for _node_id, node in sorted(graph.nodes.items())],
        "edges": [edge.to_payload() for edge in graph.edges],
    }


def analyze_graph(
    graph: AgentGraph | Mapping[str, Any],
    *,
    blocked_nodes: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return the default Phase A algorithm bundle for CLI/MCP consumers."""
    normalized = normalize_graph(graph)
    payload = normalized_to_payload(normalized)
    scc = strongly_connected_components(payload)
    cycles = cycle_evidence(payload)
    topo = topological_layers(payload)
    unblock = min_unblock_set(payload, blocked_nodes=blocked_nodes)
    critical = critical_path(payload)
    return {
        "ok": topo["ok"] and scc["ok"] and cycles["ok"] and unblock["ok"] and critical["ok"],
        "schema_version": ALGORITHMS_SCHEMA_VERSION,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "node_count": len(normalized.nodes),
        "edge_count": len(normalized.edges),
        "topology": topo,
        "scc": scc,
        "cycles": cycles,
        "critical_path": critical,
        "min_unblock_set": unblock,
    }


__all__ = [
    "ALGORITHMS_SCHEMA_VERSION",
    "DEFAULT_BLOCKER_EDGE_KINDS",
    "NormalizedEdge",
    "NormalizedGraph",
    "normalize_graph",
    "normalized_to_payload",
    "topological_layers",
    "strongly_connected_components",
    "cycle_evidence",
    "shortest_path",
    "blast_radius",
    "min_unblock_set",
    "critical_path",
    "analyze_graph",
]
