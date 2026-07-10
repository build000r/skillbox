"""Recommendation and explain engines for the agent operations brain."""
from __future__ import annotations

import difflib
import re
from typing import Any, Iterable, Mapping

from .agent_graph import AgentGraph
from .agent_graph_algorithms import blast_radius, normalize_graph, normalized_to_payload
from .agent_cli_hints import manage_py_command
from .agent_errors import brain_error_payload
from .agent_timing import attach_elapsed, timer_start
from .command_registry import CommandSpec, default_registry

DECISIONS_SCHEMA_VERSION = "2026-06-11+agent_ops_brain.decisions"
MAX_FUZZY_SUGGESTIONS = 5
FUZZY_CUTOFF = 0.6
BRAIN_TARGET_PREFIXES = ("service", "check", "skill", "mcp_tool", "bead", "command")
BRAIN_COMMAND_TARGET_ALIASES = {
    "capabilities": "runtime.capabilities",
    "next": "brain.next",
    "graph": "brain.graph",
    "explain": "brain.explain",
    "search": "brain.search",
    "snap": "brain.snap",
}


def _error_payload(
    code: str,
    message: str,
    *,
    suggestions: list[dict[str, Any]] | None = None,
    next_actions: list[str] | None = None,
    **details: Any,
) -> dict[str, Any]:
    return brain_error_payload(
        DECISIONS_SCHEMA_VERSION,
        code,
        message,
        context=details or None,
        suggestions=suggestions[:MAX_FUZZY_SUGGESTIONS] if suggestions else None,
        next_actions=next_actions,
    )


def _kind_from_node_id(node_id: str) -> str:
    return node_id.split(":", 1)[0] if ":" in node_id else "command"


def _is_bare_brain_target(target: str) -> bool:
    text = str(target or "").strip()
    aliased = BRAIN_COMMAND_TARGET_ALIASES.get(text, text)
    return ":" not in aliased and "." not in aliased


def _brain_target_corpus(graph_payload: Mapping[str, Any], registry: Mapping[str, CommandSpec] | None = None) -> list[str]:
    nodes = _node_lookup(graph_payload)
    reg = registry if registry is not None else _registry_by_id()
    return sorted(set(nodes.keys()) | {f"command:{spec_id}" for spec_id in reg})


def _bare_word_candidates(
    word: str,
    graph_payload: Mapping[str, Any],
    registry: Mapping[str, CommandSpec],
) -> list[str]:
    nodes = _node_lookup(graph_payload)
    found: list[str] = []
    if word in nodes:
        found.append(word)
    if word in registry:
        found.append(f"command:{word}")
    for prefix in BRAIN_TARGET_PREFIXES:
        candidate = f"{prefix}:{word}"
        if candidate in nodes:
            found.append(candidate)
    return _dedupe_strings(found)


def fuzzy_suggestions(
    query: str,
    candidate_ids: Iterable[str],
    *,
    limit: int = MAX_FUZZY_SUGGESTIONS,
    cutoff: float = FUZZY_CUTOFF,
) -> list[dict[str, Any]]:
    """Rank near-miss ids with difflib; never auto-resolve, only suggest."""
    query_norm = str(query or "").strip().lower()
    if not query_norm:
        return []

    unique = sorted({str(candidate).strip() for candidate in candidate_ids if str(candidate).strip()})
    match_keys: list[str] = []
    key_to_id: dict[str, str] = {}
    for node_id in unique:
        lowered = node_id.lower()
        match_keys.append(lowered)
        key_to_id[lowered] = node_id
        if ":" in node_id:
            slug = node_id.split(":", 1)[1].lower()
            match_keys.append(slug)
            key_to_id.setdefault(slug, node_id)

    ordered_keys = sorted(set(match_keys))
    matches = difflib.get_close_matches(query_norm, ordered_keys, n=limit, cutoff=cutoff)
    suggestions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for key in matches:
        node_id = key_to_id.get(key, key)
        if node_id in seen_ids:
            continue
        seen_ids.add(node_id)
        score = difflib.SequenceMatcher(None, query_norm, key).ratio()
        suggestions.append(
            {
                "id": node_id,
                "kind": _kind_from_node_id(node_id),
                "score": round(score, 3),
            }
        )
    return suggestions[:limit]


def resolve_brain_target(target: str, graph_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve explain/graph targets: exact, unique bare word, ambiguous, or unknown."""
    raw = str(target or "").strip()
    if not raw:
        return {"status": "unknown", "target": raw, "suggestions": []}

    normalized = BRAIN_COMMAND_TARGET_ALIASES.get(raw, raw)
    nodes = _node_lookup(graph_payload)
    registry = _registry_by_id()
    corpus = _brain_target_corpus(graph_payload, registry)

    if _is_bare_brain_target(raw):
        candidates = _bare_word_candidates(normalized, graph_payload, registry)
        if len(candidates) == 1:
            return {
                "status": "resolved",
                "id": candidates[0],
                "resolved_from": normalized,
            }
        if len(candidates) > 1:
            return {
                "status": "ambiguous",
                "target": normalized,
                "candidates": [
                    {"id": candidate, "kind": _kind_from_node_id(candidate)}
                    for candidate in candidates
                ],
            }
        return {
            "status": "unknown",
            "target": normalized,
            "suggestions": fuzzy_suggestions(normalized, corpus),
        }

    if normalized in nodes:
        return {"status": "resolved", "id": normalized, "resolved_from": None}
    if normalized.startswith("command:") and normalized.split(":", 1)[1] in registry:
        return {"status": "resolved", "id": normalized, "resolved_from": None}
    if normalized in registry:
        return {"status": "resolved", "id": f"command:{normalized}", "resolved_from": None}

    return {
        "status": "unknown",
        "target": normalized,
        "suggestions": fuzzy_suggestions(normalized, corpus),
    }


def _graph_payload(graph: AgentGraph | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(graph, AgentGraph):
        return graph.to_payload()
    normalized = normalize_graph(graph)
    payload = normalized_to_payload(normalized)
    warnings = graph.get("warnings") if isinstance(graph, Mapping) else []
    payload.update(
        {
            "ok": bool(graph.get("ok", True)) if isinstance(graph, Mapping) else True,
            "warnings": list(warnings) if isinstance(warnings, list) else [],
            "node_count": len(normalized.nodes),
            "edge_count": len(normalized.edges),
        }
    )
    return payload


def _adapter(adapters: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if not adapters:
        return {}
    adapter = adapters.get(name)
    return adapter if isinstance(adapter, Mapping) else {}


def _payload_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("issues", "items", "ready", "recommendations", "top_picks"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, Mapping)]
        if "id" in value:
            return [dict(value)]
    return []


def _adapter_items(adapters: Mapping[str, Any] | None, name: str) -> list[dict[str, Any]]:
    return _payload_items(_adapter(adapters, name).get("payload"))


_BR_CLAIM_RE = re.compile(r"\bbr\s+update\s+([A-Za-z0-9_.-]+)\s+--status[ =]in_progress\b")


def _issue_id(item: Mapping[str, Any]) -> str:
    for key in ("id", "issue_id", "bead_id"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    command = str(item.get("claim_command") or item.get("command") or "")
    match = _BR_CLAIM_RE.search(command)
    return match.group(1) if match else ""


def _priority_value(value: Any) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return 2
    return min(4, max(0, priority))


def _dedupe_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)
    return ordered


def _evidence_payload(adapters: Mapping[str, Any] | None, evidence: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if evidence:
        return evidence
    adapter = _adapter(adapters, "evidence")
    payload = adapter.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _bv_recommendation_ids(adapters: Mapping[str, Any] | None) -> set[str]:
    payload = _adapter(adapters, "bv_triage").get("payload")
    ids: set[str] = set()
    if isinstance(payload, Mapping):
        quick_ref = payload.get("quick_ref")
        if isinstance(quick_ref, Mapping):
            for item in quick_ref.get("top_picks") or []:
                if isinstance(item, Mapping):
                    ids.add(_issue_id(item))
                elif isinstance(item, str):
                    ids.add(item.strip())
        for item in _payload_items(payload.get("recommendations")):
            ids.add(_issue_id(item))
        for item in _payload_items(payload):
            ids.add(_issue_id(item))
    return {issue_id for issue_id in ids if issue_id}


def _bv_claimable_ids(adapters: Mapping[str, Any] | None) -> set[str]:
    payload = _adapter(adapters, "bv_triage").get("payload")
    claimable: set[str] = set()
    if isinstance(payload, Mapping):
        for item in _payload_items(payload.get("recommendations")) + _payload_items(payload):
            claim = str(item.get("claim_command") or "")
            issue_id = _issue_id(item)
            if issue_id and claim:
                claimable.add(issue_id)
    return claimable


def _runtime_blocker_recommendation(evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    overall = str(evidence.get("overall") or "").strip()
    blocked_conditions = [str(item) for item in evidence.get("blocked_conditions") or [] if str(item).strip()]
    if not overall and not blocked_conditions:
        return None
    if overall == "green" and not blocked_conditions:
        return None

    sections = evidence.get("sections") if isinstance(evidence.get("sections"), Mapping) else {}
    next_actions = _dedupe_strings(evidence.get("next_actions") or [])
    score = 980 if overall == "red" else 830 if overall == "yellow" else 760
    reason_prefix = "runtime evidence is red" if overall == "red" else "runtime evidence is not green"
    commands = next_actions[:4] or ["doctor --format json", "status --format json"]
    validations = ["python3 .env-manager/manage.py evidence --format json"]
    if "doctor" in sections:
        validations.append("python3 .env-manager/manage.py doctor --format json")
    return {
        "id": "stabilize-runtime-evidence",
        "title": "Stabilize runtime evidence before broader agent work",
        "score": score,
        "risk": "low",
        "side_effect": "none",
        "reasons": [reason_prefix, *blocked_conditions[:5]],
        "commands": commands,
        "validations": validations,
        "evidence": [{"source": "runtime-evidence", "path": "blocked_conditions"}],
    }


def _adapter_warning_recommendations(adapters: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for adapter_name, adapter in sorted((adapters or {}).items()):
        if not isinstance(adapter, Mapping) or bool(adapter.get("ok", True)):
            continue
        warnings = [warning for warning in adapter.get("warnings") or [] if isinstance(warning, Mapping)]
        code = str(warnings[0].get("code") or "ADAPTER_DEGRADED") if warnings else "ADAPTER_DEGRADED"
        recommendations.append(
            {
                "id": f"repair-adapter:{adapter_name}",
                "title": f"Repair or account for degraded {adapter_name} evidence",
                "score": 610,
                "risk": "low",
                "side_effect": "none",
                "reasons": [f"{adapter_name} adapter is {adapter.get('status', 'degraded')}", code],
                "commands": [str(" ".join(adapter.get("command") or []))] if adapter.get("command") else [],
                "validations": ["python3 .env-manager/manage.py capabilities --json"],
                "evidence": [{"source": adapter_name, "path": "warnings"}],
            }
        )
    return recommendations


def _ready_work_recommendations(
    adapters: Mapping[str, Any] | None,
    *,
    bv_ids: set[str],
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for item in _adapter_items(adapters, "br_ready"):
        issue_id = _issue_id(item)
        if not issue_id:
            continue
        priority = _priority_value(item.get("priority"))
        score = 700 + (4 - priority) * 35 + (90 if issue_id in bv_ids else 0)
        reasons = [
            f"BR reports {issue_id} as ready",
            f"priority P{priority}",
        ]
        if issue_id in bv_ids:
            reasons.append("BV also ranks this issue")
        recommendations.append(
            {
                "id": f"claim-ready:{issue_id}",
                "title": str(item.get("title") or issue_id),
                "score": score,
                "risk": "low",
                "side_effect": "local_write",
                "reasons": reasons,
                "commands": [f"br update {issue_id} --status=in_progress"],
                "validations": [f"br show {issue_id} --json", "br ready --json"],
                "evidence": [{"source": "br_ready", "path": f"payload[id={issue_id}]"}],
            }
        )
    return recommendations


def _blocked_work_recommendations(adapters: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    ready_ids = {_issue_id(item) for item in _adapter_items(adapters, "br_ready")}
    recommendations: list[dict[str, Any]] = []
    for item in _adapter_items(adapters, "br_open"):
        issue_id = _issue_id(item)
        if not issue_id or issue_id in ready_ids:
            continue
        dependencies = [
            dep for dep in item.get("dependencies") or []
            if isinstance(dep, Mapping) and str(dep.get("status") or "") != "closed"
        ]
        if not dependencies:
            continue
        blocker_ids = _dedupe_strings(dep.get("id") for dep in dependencies)
        recommendations.append(
            {
                "id": f"clear-blockers:{issue_id}",
                "title": f"Clear blockers for {issue_id}",
                "score": 640,
                "risk": "low",
                "side_effect": "none",
                "reasons": [
                    f"{issue_id} is open but not ready",
                    f"blocked by {', '.join(blocker_ids)}",
                ],
                "commands": [f"br show {issue_id} --json", *(f"br show {bid} --json" for bid in blocker_ids[:3])],
                "validations": ["br ready --json", "br dep cycles"],
                "evidence": [{"source": "br_open", "path": f"payload[id={issue_id}].dependencies"}],
            }
        )
    return recommendations


def _no_ready_recommendation(adapters: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if _adapter_items(adapters, "br_ready"):
        return None
    br_adapter = _adapter(adapters, "br_ready")
    if br_adapter and not bool(br_adapter.get("ok", True)):
        return None
    reason = "BR returned no ready work" if br_adapter else "BR ready adapter was not collected"
    return {
        "id": "inspect-work-queue",
        "title": "Inspect the work queue before choosing a new task",
        "score": 430,
        "risk": "low",
        "side_effect": "none",
        "reasons": [reason],
        "commands": ["br list --status=open --json", "bv --robot-triage --format json"],
        "validations": ["br dep cycles"],
        "evidence": [{"source": "br_ready", "path": "payload"}],
    }


def _load_guard_recommendation(adapters: Mapping[str, Any] | None) -> dict[str, Any] | None:
    payload = _adapter(adapters, "ntm_activity").get("payload")
    if not isinstance(payload, Mapping):
        return None
    load_guard = str(payload.get("load_guard") or payload.get("load") or "").strip().lower()
    if load_guard not in {"no-go", "no_go", "blocked"}:
        return None
    return {
        "id": "respect-load-guard",
        "title": "Do not spawn additional workers while load guard is no-go",
        "score": 920,
        "risk": "low",
        "side_effect": "none",
        "reasons": [f"NTM reports load guard {load_guard}"],
        "commands": ["ntm activity <session> --json"],
        "validations": ["pgrep -af 'unittest discover|full-suite.log|sbp skills --issues'"],
        "evidence": [{"source": "ntm_activity", "path": "payload.load_guard"}],
    }


def _disagreements(adapters: Mapping[str, Any] | None, ready_ids: set[str], bv_ids: set[str]) -> list[dict[str, Any]]:
    disagreements: list[dict[str, Any]] = []
    claimable = _bv_claimable_ids(adapters)
    for issue_id in sorted(claimable - ready_ids):
        disagreements.append(
            {
                "code": "BV_READY_DISAGREEMENT",
                "message": f"BV exposes a claim command for {issue_id}, but BR ready does not list it",
                "issue_id": issue_id,
                "next_action": f"br show {issue_id} --json",
            }
        )
    for issue_id in sorted(ready_ids - bv_ids):
        if bv_ids:
            disagreements.append(
                {
                    "code": "BR_BV_RANKING_GAP",
                    "message": f"BR lists {issue_id} as ready, but BV did not rank it in the parsed recommendations",
                    "issue_id": issue_id,
                    "next_action": "bv --robot-triage --format json",
                }
            )
    return disagreements


def next_action_payload(
    graph: AgentGraph | Mapping[str, Any],
    *,
    adapters: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Rank next actions from graph, evidence, Beads/BV, SBP, and load state."""
    start = timer_start()
    graph_payload = _graph_payload(graph)
    evidence_payload = _evidence_payload(adapters, evidence)
    ready_ids = {_issue_id(item) for item in _adapter_items(adapters, "br_ready") if _issue_id(item)}
    bv_ids = _bv_recommendation_ids(adapters)
    recommendations: list[dict[str, Any]] = []

    load_guard = _load_guard_recommendation(adapters)
    if load_guard:
        recommendations.append(load_guard)
    runtime_blocker = _runtime_blocker_recommendation(evidence_payload)
    if runtime_blocker:
        recommendations.append(runtime_blocker)
    if not graph_payload.get("ok", True) or graph_payload.get("warnings"):
        recommendations.append(
            {
                "id": "inspect-runtime-graph",
                "title": "Inspect runtime graph warnings before relying on graph decisions",
                "score": 720,
                "risk": "low",
                "side_effect": "none",
                "reasons": [f"graph has {len(graph_payload.get('warnings') or [])} warning(s)"],
                "commands": [manage_py_command("graph", "--format", "json")],
                "validations": ["python3 .env-manager/manage.py doctor --format json"],
                "evidence": [{"source": "graph", "path": "warnings"}],
            }
        )
    recommendations.extend(_ready_work_recommendations(adapters, bv_ids=bv_ids))
    recommendations.extend(_blocked_work_recommendations(adapters))
    recommendations.extend(_adapter_warning_recommendations(adapters))
    no_ready = _no_ready_recommendation(adapters)
    if no_ready:
        recommendations.append(no_ready)

    recommendations.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("id") or "")))
    limited = recommendations[: max(0, int(limit))]
    disagreements = _disagreements(adapters, ready_ids, bv_ids)
    return attach_elapsed({
        "ok": True,
        "schema_version": DECISIONS_SCHEMA_VERSION,
        "summary": {
            "ready_count": len(ready_ids),
            "recommendation_count": len(recommendations),
            "returned": len(limited),
            "blocked_condition_count": len(evidence_payload.get("blocked_conditions") or []),
            "graph_warning_count": len(graph_payload.get("warnings") or []),
        },
        "recommendations": limited,
        "disagreements": disagreements,
        "warnings": [
            warning
            for adapter in (adapters or {}).values()
            if isinstance(adapter, Mapping)
            for warning in (adapter.get("warnings") or [])
            if isinstance(warning, Mapping)
        ],
        "next_actions": limited[0]["commands"] if limited else [
            "br ready --json",
            manage_py_command("graph", "--format", "json"),
        ],
    }, start)


def _registry_by_id() -> dict[str, CommandSpec]:
    return {spec.id: spec for spec in default_registry()}


def _node_lookup(graph_payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(node.get("id") or ""): node
        for node in graph_payload.get("nodes") or []
        if isinstance(node, Mapping) and str(node.get("id") or "")
    }


def _edges_for_node(graph_payload: Mapping[str, Any], node_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    incoming: list[dict[str, Any]] = []
    outgoing: list[dict[str, Any]] = []
    for edge in graph_payload.get("edges") or []:
        if not isinstance(edge, Mapping):
            continue
        edge_payload = dict(edge)
        if edge.get("source") == node_id:
            outgoing.append(edge_payload)
        if edge.get("target") == node_id:
            incoming.append(edge_payload)
    incoming.sort(key=lambda edge: (str(edge.get("source")), str(edge.get("kind")), str(edge.get("target"))))
    outgoing.sort(key=lambda edge: (str(edge.get("source")), str(edge.get("kind")), str(edge.get("target"))))
    return incoming, outgoing


def _related_command_specs(kind: str, raw_id: str) -> list[CommandSpec]:
    specs: list[CommandSpec] = []
    for spec in default_registry():
        if kind == "command" and spec.id == raw_id:
            specs.append(spec)
        elif kind == "mcp_tool" and spec.mcp_tool == raw_id:
            specs.append(spec)
        elif kind in spec.graph_nodes:
            specs.append(spec)
    return sorted(specs, key=lambda spec: spec.id)


def _bead_evidence(target_id: str, adapters: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    bead_id = target_id.split(":", 1)[1] if target_id.startswith("bead:") else target_id
    evidence: list[dict[str, Any]] = []
    for adapter_name in ("br_ready", "br_open"):
        for item in _adapter_items(adapters, adapter_name):
            if _issue_id(item) == bead_id:
                evidence.append({"source": adapter_name, "path": f"payload[id={bead_id}]", "item": item})
    return evidence


def explain_payload(
    graph: AgentGraph | Mapping[str, Any],
    target: str,
    *,
    adapters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Explain a graph node or registered command."""
    start = timer_start()

    def _with_elapsed(payload: dict[str, Any]) -> dict[str, Any]:
        return attach_elapsed(payload, start)

    graph_payload = _graph_payload(graph)
    nodes = _node_lookup(graph_payload)
    registry = _registry_by_id()
    resolution = resolve_brain_target(target, graph_payload)

    if resolution["status"] == "ambiguous":
        candidates = resolution.get("candidates") or []
        candidate_ids = [str(item.get("id") or "") for item in candidates if str(item.get("id") or "")]
        return _with_elapsed(_error_payload(
            "AMBIGUOUS_NODE",
            f"ambiguous explain target: {resolution.get('target') or target}",
            target=resolution.get("target") or target,
            candidates=candidates,
            suggestions=[{"id": cid, "kind": _kind_from_node_id(cid), "score": 1.0} for cid in candidate_ids[:MAX_FUZZY_SUGGESTIONS]],
            next_actions=[
                manage_py_command("explain", candidate_id, "--format", "json")
                for candidate_id in candidate_ids[:3]
            ],
        ))

    if resolution["status"] != "resolved":
        suggestions = resolution.get("suggestions") or []
        return _with_elapsed(_error_payload(
            "UNKNOWN_NODE",
            f"unknown explain target: {target}",
            target=resolution.get("target") or target,
            suggestions=suggestions,
            next_actions=[
                manage_py_command("explain", item["id"], "--format", "json")
                for item in suggestions[:3]
            ],
        ))

    target_id = str(resolution["id"])
    resolved_from = resolution.get("resolved_from")

    if target_id not in nodes:
        command_id = target_id.split(":", 1)[1] if target_id.startswith("command:") else target_id
        if command_id in registry:
            spec = registry[command_id]
            payload = {
                "ok": True,
                "schema_version": DECISIONS_SCHEMA_VERSION,
                "target": command_id,
                "kind": "command",
                "summary": spec.summary,
                "node": None,
                "relationships": {"incoming": [], "outgoing": []},
                "commands": [spec.to_payload()],
                "evidence": [{"source": "command_registry", "path": f"capabilities[id={command_id}]"}],
                "next_actions": list(spec.examples),
            }
            if resolved_from:
                payload["resolved_from"] = resolved_from
            return _with_elapsed(payload)
        suggestions = fuzzy_suggestions(str(target), _brain_target_corpus(graph_payload, registry))
        return _with_elapsed(_error_payload(
            "UNKNOWN_NODE",
            f"unknown explain target: {target}",
            target=target,
            suggestions=suggestions,
            next_actions=[
                manage_py_command("explain", item["id"], "--format", "json")
                for item in suggestions[:3]
            ],
        ))

    node = nodes[target_id]
    kind = str(node.get("kind") or "unknown")
    raw_id = target_id.split(":", 1)[1] if ":" in target_id else target_id
    incoming, outgoing = _edges_for_node(graph_payload, target_id)
    related_specs = _related_command_specs(kind, raw_id)
    evidence = [{"source": "graph", "path": f"nodes[id={target_id}]"}]
    if kind == "bead":
        evidence.extend(_bead_evidence(target_id, adapters))
    impact = blast_radius(graph_payload, target_id) if kind in {"service", "task", "bead"} else None
    payload = {
        "ok": True,
        "schema_version": DECISIONS_SCHEMA_VERSION,
        "target": target_id,
        "kind": kind,
        "summary": str(node.get("label") or target_id),
        "node": dict(node),
        "relationships": {
            "incoming": incoming,
            "outgoing": outgoing,
            "incoming_count": len(incoming),
            "outgoing_count": len(outgoing),
        },
        "commands": [spec.to_payload() for spec in related_specs[:8]],
        "impact": impact,
        "evidence": evidence,
        "next_actions": _dedupe_strings(
            example
            for spec in related_specs[:3]
            for example in spec.examples[:1]
        ),
    }
    if resolved_from:
        payload["resolved_from"] = resolved_from
    return _with_elapsed(payload)


__all__ = [
    "DECISIONS_SCHEMA_VERSION",
    "MAX_FUZZY_SUGGESTIONS",
    "FUZZY_CUTOFF",
    "fuzzy_suggestions",
    "resolve_brain_target",
    "next_action_payload",
    "explain_payload",
]
