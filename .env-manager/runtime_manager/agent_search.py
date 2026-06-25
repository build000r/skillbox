"""Local deterministic search for the agent operations brain."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .agent_decisions import MAX_FUZZY_SUGGESTIONS, fuzzy_suggestions
from .agent_graph import AgentGraph
from .agent_graph_algorithms import normalize_graph
from .agent_cli_hints import manage_py_command
from .agent_timing import attach_elapsed, timer_start
from .command_registry import default_registry

SEARCH_SCHEMA_VERSION = "2026-06-11+agent_ops_brain.search"
DEFAULT_DOC_PATHS = ("README.md", "AGENTS.md")
MAX_DOC_BYTES = 200_000

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def _error_payload(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "schema_version": SEARCH_SCHEMA_VERSION,
        "error": {
            "code": code,
            "type": code.lower(),
            "message": message,
            "recoverable": True,
        },
        "examples": [
            manage_py_command("search", "graph", "command", "--format", "json"),
            manage_py_command("search", "--kind", "bead", "mcp", "--format", "json"),
            manage_py_command("search", "--source", "docs", "runtime", "--format", "json"),
        ],
    }
    if details:
        payload["error"]["details"] = details
    return payload


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_text(nested)}" for key, nested in sorted(value.items()))
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        return " ".join(_text(item) for item in value)
    return str(value or "")


def _snippet(text: str, query_tokens: list[str], *, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    lower = compact.lower()
    first = min((lower.find(token) for token in query_tokens if token in lower), default=-1)
    if first < 0:
        return compact[:limit]
    start = max(0, first - 40)
    end = min(len(compact), start + limit)
    prefix = "..." if start else ""
    suffix = "..." if end < len(compact) else ""
    return prefix + compact[start:end] + suffix


def _hit(
    *,
    hit_id: str,
    kind: str,
    source: str,
    title: str,
    body: str,
    next_action: str,
    query_tokens: list[str],
    base_score: int = 0,
) -> dict[str, Any] | None:
    searchable = f"{hit_id} {kind} {source} {title} {body}".lower()
    if not all(token in searchable for token in query_tokens):
        return None
    score = base_score
    id_lower = hit_id.lower()
    title_lower = title.lower()
    body_lower = body.lower()
    for token in query_tokens:
        if token == id_lower or token in id_lower:
            score += 70
        if token in title_lower:
            score += 45
        if token in body_lower:
            score += 15
    return {
        "id": hit_id,
        "kind": kind,
        "source": source,
        "score": score,
        "title": title,
        "snippet": _snippet(body or title or hit_id, query_tokens),
        "next_action": next_action,
    }


def _registry_hits(query_tokens: list[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for spec in default_registry():
        body = " ".join(
            [
                spec.summary,
                " ".join(spec.examples),
                " ".join(spec.graph_nodes),
                " ".join(spec.validations),
            ]
        )
        hit = _hit(
            hit_id=spec.id,
            kind="command",
            source="registry",
            title=spec.summary,
            body=body,
            next_action=spec.examples[0] if spec.examples else manage_py_command("explain", spec.id, "--format", "json"),
            query_tokens=query_tokens,
            base_score=120 if spec.tier == 1 else 80,
        )
        if hit:
            hits.append(hit)
    return hits


def _graph_hits(graph: AgentGraph | Mapping[str, Any] | None, query_tokens: list[str]) -> list[dict[str, Any]]:
    if graph is None:
        return []
    normalized = normalize_graph(graph.to_payload() if isinstance(graph, AgentGraph) else graph)
    hits: list[dict[str, Any]] = []
    for node_id, node in normalized.nodes.items():
        attrs = node.get("attrs") if isinstance(node, Mapping) else {}
        title = str(node.get("label") or node_id)
        body = f"{node.get('kind', 'unknown')} {title} {_text(attrs)}"
        kind = str(node.get("kind") or "unknown")
        hit = _hit(
            hit_id=node_id,
            kind=kind,
            source="graph",
            title=title,
            body=body,
            next_action=manage_py_command("explain", node_id, "--format", "json"),
            query_tokens=query_tokens,
            base_score=90,
        )
        if hit:
            hits.append(hit)
    return hits


def _payload_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("issues", "items", "ready", "recommendations"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, Mapping)]
        if "id" in value:
            return [dict(value)]
    return []


def _adapter_payload(adapters: Mapping[str, Any] | None, name: str) -> Any:
    adapter = adapters.get(name) if adapters else None
    return adapter.get("payload") if isinstance(adapter, Mapping) else None


def _bead_hits(adapters: Mapping[str, Any] | None, query_tokens: list[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for source in ("br_ready", "br_open"):
        for item in _payload_items(_adapter_payload(adapters, source)):
            bead_id = str(item.get("id") or item.get("issue_id") or "").strip()
            if not bead_id:
                continue
            title = str(item.get("title") or bead_id)
            body = _text(item)
            hit = _hit(
                hit_id=bead_id,
                kind="bead",
                source=source,
                title=title,
                body=body,
                next_action=f"br show {bead_id} --json",
                query_tokens=query_tokens,
                base_score=105 if source == "br_ready" else 95,
            )
            if hit:
                hits.append(hit)
    return hits


def _evidence_hits(
    evidence: Mapping[str, Any] | None,
    adapters: Mapping[str, Any] | None,
    query_tokens: list[str],
) -> list[dict[str, Any]]:
    payload = evidence
    if payload is None:
        adapter_payload = _adapter_payload(adapters, "evidence")
        payload = adapter_payload if isinstance(adapter_payload, Mapping) else None
    if not payload:
        return []
    hits: list[dict[str, Any]] = []
    blocked = " ".join(str(item) for item in payload.get("blocked_conditions") or [])
    overall = str(payload.get("overall") or "")
    sections = payload.get("sections") if isinstance(payload.get("sections"), Mapping) else {}
    evidence_items = [
        ("evidence:overall", "Runtime evidence overall", f"{overall} {blocked}"),
        ("evidence:blocked_conditions", "Runtime blocked conditions", blocked),
    ]
    for section_name, section in sorted(sections.items()):
        evidence_items.append((f"evidence:{section_name}", f"Evidence section {section_name}", _text(section)))
    for hit_id, title, body in evidence_items:
        hit = _hit(
            hit_id=hit_id,
            kind="evidence",
            source="evidence",
            title=title,
            body=body,
            next_action=manage_py_command("next", "--format", "json"),
            query_tokens=query_tokens,
            base_score=85,
        )
        if hit:
            hits.append(hit)
    return hits


def _read_doc(path: Path) -> tuple[str | None, dict[str, Any] | None]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, {"code": "MISSING_SOURCE", "message": f"doc source is missing: {path}", "path": str(path)}
    except OSError as exc:
        return None, {"code": "UNREADABLE_SOURCE", "message": str(exc), "path": str(path)}
    if len(data) > MAX_DOC_BYTES:
        data = data[:MAX_DOC_BYTES]
    return data.decode("utf-8", errors="replace"), None


def _doc_hits(
    query_tokens: list[str],
    *,
    root_dir: Path | None = None,
    docs: Mapping[str, str] | None = None,
    doc_paths: Iterable[str | Path] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    hits: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    doc_sources: dict[str, str] = dict(docs or {})
    if root_dir is not None:
        for raw_path in doc_paths or DEFAULT_DOC_PATHS:
            path = Path(raw_path)
            if not path.is_absolute():
                path = root_dir / path
            text, warning = _read_doc(path)
            if warning:
                warnings.append(warning)
                continue
            if text is not None:
                label = str(Path(raw_path))
                doc_sources.setdefault(label, text)

    for label, text in sorted(doc_sources.items()):
        hit = _hit(
            hit_id=f"doc:{label}",
            kind="doc",
            source="docs",
            title=label,
            body=text,
            next_action=manage_py_command("search", "--source", "docs", label, "--format", "json"),
            query_tokens=query_tokens,
            base_score=70,
        )
        if hit:
            hits.append(hit)
    return hits, warnings


def _search_corpus_ids(
    graph: AgentGraph | Mapping[str, Any] | None,
) -> list[str]:
    ids: set[str] = set()
    for spec in default_registry():
        ids.add(spec.id)
    if graph is not None:
        normalized = normalize_graph(graph.to_payload() if isinstance(graph, AgentGraph) else graph)
        ids.update(normalized.nodes.keys())
    return sorted(ids)


def _empty_search_suggestions(
    query_tokens: list[str],
    *,
    graph: AgentGraph | Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    corpus = _search_corpus_ids(graph)
    query = " ".join(query_tokens)
    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()

    for node_id in corpus:
        lowered = node_id.lower()
        if not any(token in lowered for token in query_tokens):
            continue
        if node_id in seen:
            continue
        seen.add(node_id)
        kind = node_id.split(":", 1)[0] if ":" in node_id else "command"
        suggestions.append({"id": node_id, "kind": kind, "score": 0.75})

    for item in fuzzy_suggestions(query, corpus, cutoff=0.45):
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        suggestions.append(item)

    suggestions.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("id"))))
    return suggestions[:MAX_FUZZY_SUGGESTIONS]


def _group_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for hit in hits:
        groups.setdefault(str(hit["source"]), []).append(hit)
    return [
        {"source": source, "count": len(source_hits), "hits": source_hits}
        for source, source_hits in sorted(groups.items())
    ]


def search_payload(
    query: str,
    *,
    graph: AgentGraph | Mapping[str, Any] | None = None,
    adapters: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    root_dir: Path | None = None,
    docs: Mapping[str, str] | None = None,
    doc_paths: Iterable[str | Path] | None = None,
    source_filter: Iterable[str] | None = None,
    kind_filter: Iterable[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search command registry, graph nodes, docs, Beads, and evidence."""
    start = timer_start()
    query_tokens = _tokens(query)
    if not query_tokens:
        return attach_elapsed(_error_payload("INVALID_ARGUMENT", "search query must not be empty"), start)

    hits: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    hits.extend(_registry_hits(query_tokens))
    hits.extend(_graph_hits(graph, query_tokens))
    hits.extend(_bead_hits(adapters, query_tokens))
    hits.extend(_evidence_hits(evidence, adapters, query_tokens))
    doc_hits, doc_warnings = _doc_hits(query_tokens, root_dir=root_dir, docs=docs, doc_paths=doc_paths)
    hits.extend(doc_hits)
    warnings.extend(doc_warnings)

    allowed_sources = {str(source) for source in source_filter or [] if str(source)}
    allowed_kinds = {str(kind) for kind in kind_filter or [] if str(kind)}
    if allowed_sources:
        available_sources = {str(hit["source"]) for hit in hits}
        for source in sorted(allowed_sources - available_sources):
            warnings.append({"code": "MISSING_SOURCE", "message": f"source has no hits or is unavailable: {source}", "source": source})
        hits = [hit for hit in hits if hit["source"] in allowed_sources]
    if allowed_kinds:
        hits = [hit for hit in hits if hit["kind"] in allowed_kinds]

    hits.sort(key=lambda hit: (-int(hit["score"]), str(hit["source"]), str(hit["id"])))
    limited = hits[: max(0, int(limit))]
    suggestions = _empty_search_suggestions(query_tokens, graph=graph) if not limited else []
    next_actions = [limited[0]["next_action"]] if limited else [
        manage_py_command("search", suggestion["id"], "--format", "json")
        for suggestion in suggestions[:3]
    ]
    if not next_actions:
        next_actions = [
            manage_py_command("search", "graph", "--format", "json"),
            manage_py_command("next", "--format", "json"),
        ]
    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": SEARCH_SCHEMA_VERSION,
        "query": query,
        "tokens": query_tokens,
        "count": len(limited),
        "total_count": len(hits),
        "hits": limited,
        "groups": _group_hits(limited),
        "warnings": warnings,
        "next_actions": next_actions,
    }
    if suggestions:
        payload["suggestions"] = suggestions
    return attach_elapsed(payload, start)


__all__ = [
    "SEARCH_SCHEMA_VERSION",
    "DEFAULT_DOC_PATHS",
    "search_payload",
]
