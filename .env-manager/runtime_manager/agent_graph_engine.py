"""Graph command engine for the agent operations brain."""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .agent_decisions import MAX_FUZZY_SUGGESTIONS, resolve_brain_target
from .agent_graph import AgentGraph
from .agent_cli_hints import manage_py_command
from .agent_errors import brain_error_payload
from .agent_timing import attach_elapsed, timer_start
from .agent_graph_algorithms import (
    ALGORITHMS,
    ALGORITHMS_SCHEMA_VERSION,
    normalize_graph,
    normalized_to_payload,
)

GRAPH_ENGINE_SCHEMA_VERSION = "2026-06-11+agent_ops_brain.graph_engine"
GRAPH_OUTPUT_FORMATS = frozenset({"json", "text", "dot", "mermaid"})
GRAPH_ALGORITHMS = ALGORITHMS.keys()


def _graph_algorithm_names() -> list[str]:
    return sorted(ALGORITHMS)


def _error_payload(
    code: str,
    message: str,
    *,
    next_actions: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return brain_error_payload(
        GRAPH_ENGINE_SCHEMA_VERSION,
        code,
        message,
        context=details,
        next_actions=next_actions,
    )


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
    algorithm = str(algorithm or "").strip()
    if algorithm not in ALGORITHMS:
        import difflib

        algorithm_suggestions = difflib.get_close_matches(
            algorithm,
            _graph_algorithm_names(),
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
                "allowed": _graph_algorithm_names(),
                "algorithm": algorithm,
                "suggestions": algorithm_suggestions,
            },
        )
    spec = ALGORITHMS[algorithm]
    params: dict[str, Any] = {}
    schema = spec.params_schema if isinstance(spec.params_schema, Mapping) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    required = {str(item) for item in schema.get("required") or ()}
    raw_node_params = {
        "node_id": (node_id, "node"),
        "source": (source, "source"),
        "target": (target, "target"),
    }
    for param_name, (raw_value, role) in raw_node_params.items():
        property_schema = properties.get(param_name)
        has_value = bool(str(raw_value or "").strip())
        should_resolve = (
            param_name in required
            or (
                has_value
                and isinstance(property_schema, Mapping)
                and bool(property_schema.get("x-resolve-node"))
            )
        )
        if not should_resolve:
            continue
        error, resolved_node = _resolve_graph_node_id(graph_payload, str(raw_value or ""), role)
        if error:
            return error
        params[param_name] = str(resolved_node)
    if "blocked_nodes" in properties and blocked_nodes is not None:
        params["blocked_nodes"] = blocked_nodes
    return spec.run(graph_payload, **params)


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
    start = timer_start()
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
            return attach_elapsed(algorithm_payload, start)
        payload["algorithm"] = {
            "name": algorithm,
            "schema_version": ALGORITHMS_SCHEMA_VERSION,
            "result": algorithm_payload,
        }
        payload["ok"] = payload["ok"] and bool(algorithm_payload.get("ok", True))
    return attach_elapsed(payload, start)


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
        reason = str(result.get("reason") or result.get("summary_line") or "")
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
