"""Dependency-free cycle evidence helpers for runtime dependency graphs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


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


def normalize_graph(graph: Any) -> NormalizedGraph:
    """Normalize a graph payload or object with to_payload into node/edge maps."""
    if isinstance(graph, NormalizedGraph):
        return graph
    payload = graph.to_payload() if hasattr(graph, "to_payload") else graph
    nodes: dict[str, Mapping[str, Any]] = {}
    for node in (payload or {}).get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        node_id = str(node.get("id") or "").strip()
        if node_id:
            nodes[node_id] = dict(node)

    edges: list[NormalizedEdge] = []
    for edge in (payload or {}).get("edges") or []:
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
) -> dict[str, list[tuple[str, NormalizedEdge]]]:
    adjacency: dict[str, list[tuple[str, NormalizedEdge]]] = {node_id: [] for node_id in graph.nodes}
    for edge in _filtered_edges(graph, edge_kinds):
        adjacency.setdefault(edge.source, []).append((edge.target, edge))
        adjacency.setdefault(edge.target, [])
    for node_edges in adjacency.values():
        node_edges.sort(key=lambda item: (item[0], item[1].kind, item[1].source, item[1].target))
    return dict(sorted(adjacency.items()))


def strongly_connected_components(
    graph: Any,
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
    graph: Any,
    *,
    edge_kinds: Iterable[str] | None = ("depends_on",),
) -> dict[str, Any]:
    """Return compact cycle evidence derived from SCCs."""
    normalized = normalize_graph(graph)
    scc = strongly_connected_components(normalized, edge_kinds=edge_kinds)
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
