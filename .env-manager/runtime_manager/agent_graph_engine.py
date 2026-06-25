"""Graph command engine for the agent operations brain."""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .agent_decisions import MAX_FUZZY_SUGGESTIONS, fuzzy_suggestions, resolve_brain_target
from .agent_graph import AgentGraph
from .agent_cli_hints import manage_py_command
from .agent_graph_algorithms import (
    ALGORITHMS_SCHEMA_VERSION,
    analyze_graph,
    blast_radius,
    critical_path,
    cycle_evidence,
    min_unblock_set,
    normalize_graph,
    normalized_to_payload,
    shortest_path,
    strongly_connected_components,
    topological_layers,
)

GRAPH_ENGINE_SCHEMA_VERSION = "2026-06-11+agent_ops_brain.graph_engine"
GRAPH_OUTPUT_FORMATS = frozenset({"json", "text", "dot", "mermaid"})
GRAPH_ALGORITHMS = frozenset(
    {
        "all",
        "blast-radius",
        "critical-path",
        "cycles",
        "min-unblock",
        "scc",
        "shortest-path",
        "topology",
    }
)


def _error_payload(
    code: str,
    message: str,
    *,
    next_actions: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "schema_version": GRAPH_ENGINE_SCHEMA_VERSION,
        "error": {
            "code": code,
            "type": code.lower(),
            "message": message,
            "recoverable": True,
        },
    }
    if details:
        payload["error"]["details"] = details
    if next_actions:
        payload["next_actions"] = next_actions
    return payload


def _graph_payload(graph: AgentGraph | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(graph, AgentGraph):
        payload = graph.to_payload()
    else:
        payload = dict(graph)
    normalized = normalize_graph(payload)
    normalized_payload = normalized_to_payload(normalized)
    warnings = payload.get("warnings") if isinstance(payload, Mapping) else []
    normalized_payload.update(
        {
            "ok": bool(payload.get("ok", True)) if isinstance(payload, Mapping) else True,
            "node_count": len(normalized.nodes),
            "edge_count": len(normalized.edges),
            "node_kinds": sorted(
                {
                    str(node.get("kind") or "unknown")
                    for node in normalized.nodes.values()
                    if isinstance(node, Mapping)
                }
            ),
            "warnings": list(warnings) if isinstance(warnings, list) else [],
        }
    )
    return normalized_payload


def _resolve_graph_node_id(
    graph_payload: Mapping[str, Any],
    node_id: str,
    role: str,
) -> tuple[dict[str, Any] | None, str | None]:
    node_id = str(node_id or "").strip()
    if not node_id:
        return (
            _error_payload(
                "INVALID_ARGUMENT",
                f"{role} is required",
                next_actions=[
                    manage_py_command("graph", "--algorithm", "blast-radius", "--node", "<node-id>", "--format", "json")
                ],
            ),
            None,
        )
    resolution = resolve_brain_target(node_id, graph_payload)
    if resolution["status"] == "ambiguous":
        candidates = resolution.get("candidates") or []
        candidate_ids = [str(item.get("id") or "") for item in candidates if str(item.get("id") or "")]
        return (
            _error_payload(
                "AMBIGUOUS_NODE",
                f"ambiguous graph {role}: {resolution.get('target') or node_id}",
                next_actions=[
                    manage_py_command("graph", "--algorithm", "blast-radius", "--node", candidate_id, "--format", "json")
                    for candidate_id in candidate_ids[:3]
                ],
                details={
                    "node_id": node_id,
                    "role": role,
                    "target": resolution.get("target") or node_id,
                    "candidates": candidates,
                },
            ),
            None,
        )
    if resolution["status"] != "resolved":
        suggestions = resolution.get("suggestions") or []
        return (
            _error_payload(
                "UNKNOWN_NODE",
                f"unknown graph node: {node_id}",
                next_actions=[
                    manage_py_command("graph", "--algorithm", "blast-radius", "--node", item["id"], "--format", "json")
                    for item in suggestions[:3]
                ],
                details={"node_id": node_id, "role": role, "suggestions": suggestions},
            ),
            None,
        )
    return None, str(resolution["id"])


def _algorithm_payload(
    graph_payload: Mapping[str, Any],
    algorithm: str,
    *,
    node_id: str | None = None,
    source: str | None = None,
    target: str | None = None,
    blocked_nodes: Iterable[str] | None = None,
) -> dict[str, Any]:
    if algorithm not in GRAPH_ALGORITHMS:
        import difflib

        algorithm_suggestions = difflib.get_close_matches(
            str(algorithm or ""),
            sorted(GRAPH_ALGORITHMS),
            n=MAX_FUZZY_SUGGESTIONS,
            cutoff=0.5,
        )
        next_actions = [
            manage_py_command("graph", "--algorithm", suggestion, "--format", "json")
            for suggestion in algorithm_suggestions[:3]
        ]
        if not next_actions:
            next_actions = [
                manage_py_command("graph", "--algorithm", "all", "--format", "json"),
                manage_py_command("graph", "--algorithm", "cycles", "--format", "json"),
            ]
        return _error_payload(
            "INVALID_ARGUMENT",
            f"unknown graph algorithm: {algorithm}",
            next_actions=next_actions,
            details={
                "allowed": sorted(GRAPH_ALGORITHMS),
                "algorithm": algorithm,
                "suggestions": algorithm_suggestions,
            },
        )
    if algorithm == "all":
        return analyze_graph(graph_payload, blocked_nodes=blocked_nodes)
    if algorithm == "topology":
        return topological_layers(graph_payload)
    if algorithm == "cycles":
        return cycle_evidence(graph_payload)
    if algorithm == "scc":
        return strongly_connected_components(graph_payload)
    if algorithm == "min-unblock":
        return min_unblock_set(graph_payload, blocked_nodes=blocked_nodes)
    if algorithm == "critical-path":
        return critical_path(graph_payload)
    if algorithm == "blast-radius":
        error, resolved_node = _resolve_graph_node_id(graph_payload, str(node_id or ""), "node")
        if error:
            return error
        return blast_radius(graph_payload, str(resolved_node))
    if algorithm == "shortest-path":
        error, resolved_source = _resolve_graph_node_id(graph_payload, str(source or ""), "source")
        if error:
            return error
        error, resolved_target = _resolve_graph_node_id(graph_payload, str(target or ""), "target")
        if error:
            return error
        return shortest_path(graph_payload, str(resolved_source), str(resolved_target))
    raise AssertionError(f"unhandled graph algorithm: {algorithm}")


def graph_command_payload(
    graph: AgentGraph | Mapping[str, Any],
    *,
    algorithm: str | None = None,
    node_id: str | None = None,
    source: str | None = None,
    target: str | None = None,
    blocked_nodes: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build the graph command JSON payload, optionally with algorithms."""
    graph_payload = _graph_payload(graph)
    payload: dict[str, Any] = {
        "ok": bool(graph_payload.get("ok", True)),
        "schema_version": GRAPH_ENGINE_SCHEMA_VERSION,
        "graph": graph_payload,
        "next_actions": [
            manage_py_command("graph", "--algorithm", "all", "--format", "json"),
            manage_py_command("next", "--format", "json"),
        ],
    }
    if algorithm:
        algorithm_payload = _algorithm_payload(
            graph_payload,
            algorithm,
            node_id=node_id,
            source=source,
            target=target,
            blocked_nodes=blocked_nodes,
        )
        if "error" in algorithm_payload:
            return algorithm_payload
        payload["algorithm"] = {
            "name": algorithm,
            "schema_version": ALGORITHMS_SCHEMA_VERSION,
            "result": algorithm_payload,
        }
        payload["ok"] = payload["ok"] and bool(algorithm_payload.get("ok", True))
    return payload


def _quote_dot(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def graph_to_dot(graph_payload: Mapping[str, Any]) -> str:
    graph = _graph_payload(graph_payload)
    lines = ["digraph skillbox_agent_graph {"]
    lines.append("  rankdir=LR;")
    for node in graph.get("nodes") or []:
        node_id = str(node.get("id") or "")
        label = str(node.get("label") or node_id)
        kind = str(node.get("kind") or "unknown")
        lines.append(f"  {_quote_dot(node_id)} [label={_quote_dot(label)}, kind={_quote_dot(kind)}];")
    for edge in graph.get("edges") or []:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        kind = str(edge.get("kind") or "")
        lines.append(f"  {_quote_dot(source)} -> {_quote_dot(target)} [label={_quote_dot(kind)}];")
    lines.append("}")
    return "\n".join(lines)


_MERMAID_ID_PATTERN = re.compile(r"[^A-Za-z0-9_]")


def _mermaid_node_ids(graph_payload: Mapping[str, Any]) -> dict[str, str]:
    node_ids = [
        str(node.get("id") or "")
        for node in graph_payload.get("nodes") or []
        if isinstance(node, Mapping) and str(node.get("id") or "")
    ]
    mapping: dict[str, str] = {}
    for index, node_id in enumerate(sorted(node_ids)):
        stem = _MERMAID_ID_PATTERN.sub("_", node_id).strip("_") or "node"
        if not stem[0].isalpha():
            stem = "n_" + stem
        candidate = stem
        if candidate in mapping.values():
            candidate = f"{stem}_{index}"
        mapping[node_id] = candidate
    return mapping


def _quote_mermaid_label(value: str) -> str:
    return value.replace('"', '\\"').replace("\n", " ")


def graph_to_mermaid(graph_payload: Mapping[str, Any]) -> str:
    graph = _graph_payload(graph_payload)
    mapping = _mermaid_node_ids(graph)
    lines = ["flowchart TD"]
    for node in graph.get("nodes") or []:
        node_id = str(node.get("id") or "")
        label = str(node.get("label") or node_id)
        lines.append(f'  {mapping[node_id]}["{_quote_mermaid_label(label)}"]')
    for edge in graph.get("edges") or []:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        kind = str(edge.get("kind") or "")
        if source in mapping and target in mapping:
            lines.append(f'  {mapping[source]} -- "{_quote_mermaid_label(kind)}" --> {mapping[target]}')
    return "\n".join(lines)


def graph_text_summary(payload: Mapping[str, Any]) -> str:
    graph = payload.get("graph") if "graph" in payload else payload
    node_count = int(graph.get("node_count") or len(graph.get("nodes") or []))
    edge_count = int(graph.get("edge_count") or len(graph.get("edges") or []))
    warning_count = len(graph.get("warnings") or [])
    lines = [f"graph: {node_count} nodes, {edge_count} edges, {warning_count} warnings"]
    algorithm = payload.get("algorithm") if isinstance(payload, Mapping) else None
    if isinstance(algorithm, Mapping):
        result = algorithm.get("result") if isinstance(algorithm.get("result"), Mapping) else {}
        lines.append(f"algorithm: {algorithm.get('name')} ok={bool(result.get('ok', True))}")
        reason = str(result.get("reason") or "")
        if reason:
            lines.append(f"reason: {reason}")
    return "\n".join(lines)


def render_graph_payload(payload: Mapping[str, Any], output_format: str) -> str:
    """Render a graph command payload as text, DOT, or Mermaid."""
    if output_format not in GRAPH_OUTPUT_FORMATS:
        raise ValueError(f"unknown graph output format: {output_format}")
    if output_format == "json":
        raise ValueError("json graph output should be emitted as the raw payload")
    graph = payload.get("graph") if "graph" in payload else payload
    if output_format == "text":
        return graph_text_summary(payload)
    if output_format == "dot":
        return graph_to_dot(graph)
    if output_format == "mermaid":
        return graph_to_mermaid(graph)
    raise AssertionError(f"unhandled graph output format: {output_format}")


__all__ = [
    "GRAPH_ENGINE_SCHEMA_VERSION",
    "GRAPH_OUTPUT_FORMATS",
    "GRAPH_ALGORITHMS",
    "graph_command_payload",
    "graph_to_dot",
    "graph_to_mermaid",
    "graph_text_summary",
    "render_graph_payload",
]
