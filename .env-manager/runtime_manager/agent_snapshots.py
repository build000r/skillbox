"""Snapshot, diff, and replay support for the agent operations brain."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

# Redaction primitives come from the single shared table. ``redact_diagnostic_text``
# is the thin alias kept on agent_adapters; ``_is_secret_key`` reuses the shared
# key matcher so this surface never carries its own pattern copy. The snapshot
# variant adds its OWN deterministic sorting/stringifying of mapping keys (needed
# for stable snapshot hashes), so it stays a local recursion rather than calling
# the generic ``redact_value``.
from .agent_adapters import REDACTION_MARKER, redact_diagnostic_text
from .shared import is_secret_key as _is_secret_key

SNAPSHOT_SCHEMA_VERSION = "2026-06-11+agent_ops_brain.snapshot"
SNAPSHOT_DIR = Path(".skillbox-state") / "snapshots" / "agent_ops"


def redact_snapshot_value(value: Any, *, key: str = "") -> Any:
    """Recursively redact secret-looking keys and string assignments.

    Like the shared ``redact_value`` but additionally sorts and stringifies
    mapping keys so committed snapshots hash deterministically.
    """
    if _is_secret_key(key):
        return REDACTION_MARKER
    if isinstance(value, str):
        return redact_diagnostic_text(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_snapshot_value(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return [redact_snapshot_value(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [redact_snapshot_value(item, key=key) for item in value]
    return value


def _sorted_dict_items(items: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    def sort_key(item: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(item.get(key) or "") for key in keys)

    return sorted(items, key=sort_key)


def _normalize_status(status: Mapping[str, Any] | None) -> dict[str, Any]:
    if not status:
        return {}
    services = [
        {
            key: service[key]
            for key in ("id", "name", "state", "status", "health")
            if isinstance(service, Mapping) and key in service
        }
        for service in status.get("services") or []
        if isinstance(service, Mapping)
    ]
    tasks = [
        {
            key: task[key]
            for key in ("id", "name", "state", "status")
            if isinstance(task, Mapping) and key in task
        }
        for task in status.get("tasks") or []
        if isinstance(task, Mapping)
    ]
    repos = [
        {
            key: repo[key]
            for key in ("id", "name", "present", "path")
            if isinstance(repo, Mapping) and key in repo
        }
        for repo in status.get("repos") or []
        if isinstance(repo, Mapping)
    ]
    return redact_snapshot_value(
        {
            "active_clients": sorted(str(item) for item in status.get("active_clients") or []),
            "active_profiles": sorted(str(item) for item in status.get("active_profiles") or []),
            "blocked_services": sorted(str(item) for item in status.get("blocked_services") or []),
            "services": _sorted_dict_items(services, "id", "name"),
            "tasks": _sorted_dict_items(tasks, "id", "name"),
            "repos": _sorted_dict_items(repos, "id", "name"),
        }
    )


def _normalize_doctor(doctor: Any) -> dict[str, Any]:
    if not doctor:
        return {"checks": []}
    checks = doctor.get("checks") if isinstance(doctor, Mapping) else doctor
    normalized: list[dict[str, Any]] = []
    for check in checks or []:
        if isinstance(check, Mapping):
            normalized.append(
                redact_snapshot_value(
                    {
                        key: check[key]
                        for key in ("code", "status", "message", "details")
                        if key in check
                    }
                )
            )
        else:
            normalized.append(
                {
                    "code": str(getattr(check, "code", "")),
                    "status": str(getattr(check, "status", "")),
                    "message": redact_diagnostic_text(str(getattr(check, "message", ""))),
                }
            )
    return {"checks": _sorted_dict_items(normalized, "code")}


def _normalize_graph(graph: Mapping[str, Any] | None) -> dict[str, Any]:
    if not graph:
        return {"nodes": [], "edges": [], "warnings": []}
    nodes = [
        redact_snapshot_value(
            {
                key: node[key]
                for key in ("id", "kind", "label", "attrs", "provenance")
                if isinstance(node, Mapping) and key in node
            }
        )
        for node in graph.get("nodes") or []
        if isinstance(node, Mapping)
    ]
    edges = [
        redact_snapshot_value(
            {
                key: edge[key]
                for key in ("source", "target", "kind", "attrs", "provenance")
                if isinstance(edge, Mapping) and key in edge
            }
        )
        for edge in graph.get("edges") or []
        if isinstance(edge, Mapping)
    ]
    warnings = [
        redact_snapshot_value(warning)
        for warning in graph.get("warnings") or []
        if isinstance(warning, Mapping)
    ]
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": _sorted_dict_items(nodes, "id"),
        "edges": _sorted_dict_items(edges, "source", "kind", "target"),
        "warnings": _sorted_dict_items(warnings, "code", "message"),
    }


def _normalize_evidence(evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    if not evidence:
        return {}
    sections = evidence.get("sections") if isinstance(evidence.get("sections"), Mapping) else {}
    return redact_snapshot_value(
        {
            "overall": evidence.get("overall"),
            "blocked_conditions": sorted(str(item) for item in evidence.get("blocked_conditions") or []),
            "next_actions": list(evidence.get("next_actions") or []),
            "sections": sections,
        }
    )


def normalize_snapshot_inputs(
    *,
    status: Mapping[str, Any] | None = None,
    doctor: Any = None,
    evidence: Mapping[str, Any] | None = None,
    graph: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": _normalize_status(status),
        "doctor": _normalize_doctor(doctor),
        "evidence": _normalize_evidence(evidence),
        "graph": _normalize_graph(graph),
    }


def _stable_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def create_snapshot_payload(
    *,
    status: Mapping[str, Any] | None = None,
    doctor: Any = None,
    evidence: Mapping[str, Any] | None = None,
    graph: Mapping[str, Any] | None = None,
    label: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create a normalized, redacted, deterministic snapshot payload."""
    inputs = normalize_snapshot_inputs(status=status, doctor=doctor, evidence=evidence, graph=graph)
    core = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "label": label or "agent-ops-snapshot",
        "created_at": created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": inputs,
    }
    snapshot_id = _stable_hash(core)
    return {
        "ok": True,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        **core,
    }


def snapshot_storage_dir(root_dir: Path) -> Path:
    return root_dir / SNAPSHOT_DIR


def save_snapshot(root_dir: Path, payload: Mapping[str, Any]) -> Path:
    directory = snapshot_storage_dir(root_dir)
    directory.mkdir(parents=True, exist_ok=True)
    snapshot_id = str(payload.get("snapshot_id") or _stable_hash(payload))
    path = directory / f"{snapshot_id}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_snapshot(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _mapping_section(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    return value if isinstance(value, Mapping) else {}


def _iter_mappings(items: Any) -> list[Mapping[str, Any]]:
    return [item for item in items or [] if isinstance(item, Mapping)]


def _first_text(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _status_entities(status: Mapping[str, Any]) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    for service in _iter_mappings(status.get("services")):
        entities[f"status.service:{_first_text(service, 'id', 'name')}"] = service
    for task in _iter_mappings(status.get("tasks")):
        entities[f"status.task:{_first_text(task, 'id', 'name')}"] = task
    return entities


def _doctor_entities(doctor: Mapping[str, Any]) -> dict[str, Any]:
    return {f"doctor.check:{check.get('code')}": check for check in _iter_mappings(doctor.get("checks"))}


def _evidence_entities(evidence: Mapping[str, Any]) -> dict[str, Any]:
    if not evidence:
        return {}
    entities: dict[str, Any] = {"evidence.overall": evidence.get("overall")}
    for condition in evidence.get("blocked_conditions") or []:
        entities[f"evidence.blocked:{condition}"] = condition
    return entities


def _graph_entities(graph: Mapping[str, Any]) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    for node in _iter_mappings(graph.get("nodes")):
        entities[f"graph.node:{node.get('id')}"] = node
    for edge in _iter_mappings(graph.get("edges")):
        key = f"{edge.get('source')}|{edge.get('kind')}|{edge.get('target')}"
        entities[f"graph.edge:{key}"] = edge
    for warning in _iter_mappings(graph.get("warnings")):
        entities[f"graph.warning:{warning.get('code')}:{warning.get('message')}"] = warning
    return entities


def _entity_map(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    inputs = _mapping_section(snapshot, "inputs")
    entities: dict[str, Any] = {}
    entities.update(_status_entities(_mapping_section(inputs, "status")))
    entities.update(_doctor_entities(_mapping_section(inputs, "doctor")))
    entities.update(_evidence_entities(_mapping_section(inputs, "evidence")))
    entities.update(_graph_entities(_mapping_section(inputs, "graph")))
    return dict(sorted(entities.items()))


def _status_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("status") or value.get("state") or "").strip().lower()
    return str(value or "").strip().lower()


def _severity(entity: str, before: Any, after: Any, change: str) -> str:
    after_status = _status_value(after)
    before_status = _status_value(before)
    if entity.startswith("doctor.check") and after_status == "fail":
        return "high"
    if entity == "evidence.overall" and str(after).lower() == "red":
        return "high"
    if entity.startswith("evidence.blocked"):
        return "medium" if change == "added" else "low"
    if entity.startswith("status.service") and after_status in {"stopped", "not-running", "down", "fail", "failed"}:
        return "high"
    if entity.startswith("status.service") and before_status != after_status:
        return "medium"
    if entity.startswith("graph.warning") and change == "added":
        return "medium"
    if entity.startswith("graph.node") or entity.startswith("graph.edge"):
        return "low"
    return "medium" if change == "modified" else "low"


def diff_snapshots(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    before_entities = _entity_map(before)
    after_entities = _entity_map(after)
    changes: list[dict[str, Any]] = []
    for entity in sorted(set(before_entities) | set(after_entities)):
        if entity not in before_entities:
            change = "added"
            before_value = None
            after_value = after_entities[entity]
        elif entity not in after_entities:
            change = "removed"
            before_value = before_entities[entity]
            after_value = None
        elif before_entities[entity] != after_entities[entity]:
            change = "modified"
            before_value = before_entities[entity]
            after_value = after_entities[entity]
        else:
            continue
        changes.append(
            {
                "entity": entity,
                "change": change,
                "severity": _severity(entity, before_value, after_value, change),
                "before": before_value,
                "after": after_value,
            }
        )
    severity_order = {"high": 0, "medium": 1, "low": 2}
    changes.sort(key=lambda item: (severity_order.get(str(item["severity"]), 9), str(item["entity"])))
    return {
        "ok": True,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "before": before.get("snapshot_id"),
        "after": after.get("snapshot_id"),
        "change_count": len(changes),
        "changes": changes,
        "severity_counts": {
            severity: sum(1 for item in changes if item["severity"] == severity)
            for severity in ("high", "medium", "low")
        },
    }


def replay_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Replay a committed snapshot fixture without touching live services."""
    inputs = snapshot.get("inputs") if isinstance(snapshot.get("inputs"), Mapping) else {}
    status = inputs.get("status") if isinstance(inputs.get("status"), Mapping) else {}
    doctor = inputs.get("doctor") if isinstance(inputs.get("doctor"), Mapping) else {}
    evidence = inputs.get("evidence") if isinstance(inputs.get("evidence"), Mapping) else {}
    graph = inputs.get("graph") if isinstance(inputs.get("graph"), Mapping) else {}
    return {
        "ok": True,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot.get("snapshot_id"),
        "label": snapshot.get("label"),
        "summary": {
            "services": len(status.get("services") or []),
            "doctor_checks": len(doctor.get("checks") or []),
            "overall": evidence.get("overall"),
            "blocked_conditions": len(evidence.get("blocked_conditions") or []),
            "graph_nodes": len(graph.get("nodes") or []),
            "graph_edges": len(graph.get("edges") or []),
            "graph_warnings": len(graph.get("warnings") or []),
        },
        "next_actions": ["snap diff <before> <after>", "brain.graph --format json"],
    }


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "SNAPSHOT_DIR",
    "redact_snapshot_value",
    "normalize_snapshot_inputs",
    "create_snapshot_payload",
    "snapshot_storage_dir",
    "save_snapshot",
    "load_snapshot",
    "diff_snapshots",
    "replay_snapshot",
]
