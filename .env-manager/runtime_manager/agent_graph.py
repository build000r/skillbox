"""Typed runtime graph for the agent operations brain."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .agent_adapters import collect_agent_adapter_evidence
from .command_registry import default_registry
from .runtime_ops import (
    order_service_ids,
    order_task_ids,
    service_bootstrap_task_ids,
    service_dependency_graph,
    task_dependency_graph,
)
from .workflows import requested_mcp_servers

GRAPH_SCHEMA_VERSION = "2026-06-11+agent_ops_brain.graph"

PHASE_A_NODE_KINDS = frozenset(
    {
        "client",
        "profile",
        "repo",
        "artifact",
        "skill",
        "mcp_tool",
        "service",
        "task",
        "check",
        "command",
        "bead",
        "evidence",
        "snapshot",
    }
)


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    label: str
    provenance: str
    attrs: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "provenance": self.provenance,
            "attrs": dict(sorted(self.attrs.items())),
        }


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str
    provenance: str
    attrs: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "provenance": self.provenance,
            "attrs": dict(sorted(self.attrs.items())),
        }


@dataclass(frozen=True)
class AgentGraph:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    warnings: tuple[dict[str, Any], ...]

    def to_payload(self) -> dict[str, Any]:
        nodes = sorted(self.nodes, key=lambda node: node.id)
        edges = sorted(self.edges, key=lambda edge: (edge.source, edge.kind, edge.target))
        return {
            "ok": not self.warnings,
            "schema_version": GRAPH_SCHEMA_VERSION,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "node_kinds": sorted({node.kind for node in nodes}),
            "nodes": [node.to_payload() for node in nodes],
            "edges": [edge.to_payload() for edge in edges],
            "warnings": list(self.warnings),
        }


def graph_node_id(kind: str, raw_id: Any) -> str:
    return f"{kind}:{str(raw_id).strip()}"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _item_id(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


class _GraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[tuple[str, str, str, str], GraphEdge] = {}
        self.warnings: list[dict[str, Any]] = []

    def warn(self, code: str, message: str, **attrs: Any) -> None:
        warning: dict[str, Any] = {"code": code, "message": message}
        warning.update({key: value for key, value in attrs.items() if value is not None})
        self.warnings.append(warning)

    def add_node(
        self,
        kind: str,
        raw_id: Any,
        *,
        label: str | None = None,
        provenance: str,
        attrs: dict[str, Any] | None = None,
    ) -> str:
        clean_id = str(raw_id).strip()
        if not clean_id:
            self.warn("MISSING_NODE_ID", f"skipping {kind} node with missing id", provenance=provenance)
            return ""
        node_id = graph_node_id(kind, clean_id)
        if node_id not in self.nodes:
            self.nodes[node_id] = GraphNode(
                id=node_id,
                kind=kind,
                label=label or clean_id,
                provenance=provenance,
                attrs=attrs or {},
            )
        return node_id

    def add_edge(
        self,
        source: str,
        target: str,
        kind: str,
        *,
        provenance: str,
        attrs: dict[str, Any] | None = None,
    ) -> None:
        if not source or not target:
            return
        key = (source, target, kind, provenance)
        self.edges[key] = GraphEdge(source, target, kind, provenance, attrs or {})

    def finish(self) -> AgentGraph:
        return AgentGraph(
            nodes=tuple(self.nodes.values()),
            edges=tuple(self.edges.values()),
            warnings=tuple(self.warnings),
        )


def _add_profile_edges(builder: _GraphBuilder, item_node: str, item: dict[str, Any], kind: str) -> None:
    for profile in _string_list(item.get("profiles")):
        profile_node = builder.add_node("profile", profile, provenance="runtime_model.profiles")
        builder.add_edge(profile_node, item_node, "declares", provenance=f"runtime_model.{kind}.profiles")


def _add_model_collection(
    builder: _GraphBuilder,
    model: dict[str, Any],
    collection: str,
    kind: str,
    *,
    id_keys: tuple[str, ...] = ("id", "name"),
) -> None:
    for item in model.get(collection) or []:
        if not isinstance(item, dict):
            continue
        item_id = _item_id(item, *id_keys)
        node = builder.add_node(
            kind,
            item_id,
            label=str(item.get("label") or item.get("name") or item_id),
            provenance=f"runtime_model.{collection}",
            attrs={
                key: item[key]
                for key in ("kind", "path", "host_path", "state", "required")
                if key in item
            },
        )
        _add_profile_edges(builder, node, item, collection)


def _add_clients_and_profiles(builder: _GraphBuilder, model: dict[str, Any]) -> None:
    for client in model.get("clients") or []:
        if isinstance(client, dict):
            client_id = _item_id(client, "id", "name")
            builder.add_node(
                "client",
                client_id,
                label=str(client.get("label") or client_id),
                provenance="runtime_model.clients",
                attrs={"active": client_id in set(_string_list(model.get("active_clients")))},
            )
    for client_id in _string_list(model.get("active_clients")):
        builder.add_node("client", client_id, provenance="runtime_model.active_clients", attrs={"active": True})

    profiles_seen: set[str] = set()
    for profile in model.get("profiles") or []:
        if isinstance(profile, dict):
            profile_id = _item_id(profile, "id", "name")
            profiles_seen.add(profile_id)
            builder.add_node(
                "profile",
                profile_id,
                label=str(profile.get("label") or profile_id),
                provenance="runtime_model.profiles",
                attrs={"active": profile_id in set(_string_list(model.get("active_profiles")))},
            )
    for profile_id in _string_list(model.get("active_profiles")):
        if profile_id not in profiles_seen:
            builder.add_node(
                "profile",
                profile_id,
                provenance="runtime_model.active_profiles",
                attrs={"active": True},
            )


def _annotate_service_ports(builder: _GraphBuilder, model: dict[str, Any]) -> None:
    """Cheap port-attr enrichment: copy declared port/bind_scope onto service nodes.

    Reuses the port registry view so the graph agrees with `manage.py ports`
    and the doctor. Skips silently if the registry cannot be built.
    """
    try:
        from .port_registry import build_port_registry

        entries = build_port_registry(model)
    except Exception:
        return
    by_service: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("owner_kind") != "service" or entry.get("port") is None:
            continue
        by_service.setdefault(str(entry["owner_id"]), entry)
    for service_id, entry in by_service.items():
        node = builder.nodes.get(graph_node_id("service", service_id))
        if node is not None and "port" not in node.attrs:
            node.attrs["port"] = entry["port"]
            node.attrs["bind_scope"] = entry["bind_scope"]


def _add_services_and_tasks(builder: _GraphBuilder, model: dict[str, Any]) -> None:
    _add_model_collection(builder, model, "services", "service")
    _annotate_service_ports(builder, model)
    _add_model_collection(builder, model, "tasks", "task")

    for service_id, dependency_ids in service_dependency_graph(model).items():
        service_node = graph_node_id("service", service_id)
        for dependency_id in dependency_ids:
            dependency_node = graph_node_id("service", dependency_id)
            if dependency_node not in builder.nodes:
                dependency_node = builder.add_node(
                    "artifact",
                    dependency_id,
                    provenance="runtime_ops.service_dependency_graph",
                    attrs={"implicit": True},
                )
            builder.add_edge(
                service_node,
                dependency_node,
                "depends_on",
                provenance="runtime_ops.service_dependency_graph",
            )
    for service in model.get("services") or []:
        if not isinstance(service, dict):
            continue
        service_id = _item_id(service, "id", "name")
        service_node = graph_node_id("service", service_id)
        for task_id in service_bootstrap_task_ids(service):
            builder.add_edge(
                service_node,
                builder.add_node("task", task_id, provenance="runtime_ops.service_bootstrap_task_ids"),
                "depends_on",
                provenance="runtime_ops.service_bootstrap_task_ids",
            )
        repo_id = str(service.get("repo") or "").strip()
        if repo_id:
            builder.add_edge(
                service_node,
                builder.add_node("repo", repo_id, provenance="runtime_model.services.repo"),
                "configured_by",
                provenance="runtime_model.services.repo",
            )
        artifact_id = str(service.get("artifact") or "").strip()
        if artifact_id:
            builder.add_edge(
                service_node,
                builder.add_node("artifact", artifact_id, provenance="runtime_model.services.artifact"),
                "consumes",
                provenance="runtime_model.services.artifact",
            )

    for task_id, dependency_ids in task_dependency_graph(model).items():
        task_node = graph_node_id("task", task_id)
        for dependency_id in dependency_ids:
            builder.add_edge(
                task_node,
                builder.add_node("task", dependency_id, provenance="runtime_ops.task_dependency_graph"),
                "depends_on",
                provenance="runtime_ops.task_dependency_graph",
            )
    for task in model.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = _item_id(task, "id", "name")
        repo_id = str(task.get("repo") or "").strip()
        if repo_id:
            builder.add_edge(
                graph_node_id("task", task_id),
                builder.add_node("repo", repo_id, provenance="runtime_model.tasks.repo"),
                "configured_by",
                provenance="runtime_model.tasks.repo",
            )


def _add_checks_skills_mcp(builder: _GraphBuilder, model: dict[str, Any]) -> None:
    _add_model_collection(builder, model, "repos", "repo")
    _add_model_collection(builder, model, "artifacts", "artifact")
    _add_model_collection(builder, model, "checks", "check", id_keys=("id", "code", "name"))
    for check in model.get("checks") or []:
        if not isinstance(check, dict):
            continue
        check_id = _item_id(check, "id", "code", "name")
        repo_id = str(check.get("repo") or "").strip()
        if repo_id:
            builder.add_edge(
                graph_node_id("check", check_id),
                builder.add_node("repo", repo_id, provenance="runtime_model.checks.repo"),
                "configured_by",
                provenance="runtime_model.checks.repo",
            )

    skill_items = []
    for key in ("skills", "skill_repos", "skillsets"):
        skill_items.extend(item for item in (model.get(key) or []) if isinstance(item, dict))
    for skill in skill_items:
        skill_id = _item_id(skill, "id", "name", "repo")
        node = builder.add_node(
            "skill",
            skill_id,
            label=str(skill.get("label") or skill.get("name") or skill_id),
            provenance="runtime_model.skills",
            attrs={key: skill[key] for key in ("kind", "path", "repo", "state") if key in skill},
        )
        _add_profile_edges(builder, node, skill, "skills")

    for request in requested_mcp_servers(model):
        if not isinstance(request, dict):
            continue
        name = str(request.get("name") or "").strip()
        if name:
            builder.add_node(
                "mcp_tool",
                name,
                provenance="workflows.requested_mcp_servers",
                attrs={key: request[key] for key in ("service_id", "command") if key in request},
            )


def _add_commands(builder: _GraphBuilder) -> None:
    for spec in default_registry():
        command_node = builder.add_node(
            "command",
            spec.id,
            label=spec.summary,
            provenance="command_registry.default_registry",
            attrs={
                "tier": spec.tier,
                "surface": list(spec.surface),
                "risk": spec.risk,
                "side_effect": spec.side_effect,
                "entrypoint": spec.entrypoint,
            },
        )
        if spec.mcp_tool:
            mcp_node = builder.add_node(
                "mcp_tool",
                spec.mcp_tool,
                provenance="command_registry.default_registry",
            )
            builder.add_edge(command_node, mcp_node, "exposes", provenance="command_registry.default_registry")
        for node_kind in spec.graph_nodes:
            if node_kind in {"command", "mcp_tool"}:
                continue
            graph_kind_node = builder.add_node(
                "evidence",
                f"graph-node-kind:{node_kind}",
                label=f"graph node kind {node_kind}",
                provenance="command_registry.graph_nodes",
                attrs={"node_kind": node_kind},
            )
            builder.add_edge(command_node, graph_kind_node, "mentions", provenance="command_registry.graph_nodes")


def _adapter_payload_items(adapter: dict[str, Any]) -> list[dict[str, Any]]:
    payload = adapter.get("payload")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("issues", "items", "ready", "recommendations"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if "id" in payload:
            return [payload]
    return []


def _add_adapter_evidence(builder: _GraphBuilder, adapters: dict[str, Any] | None) -> None:
    if not adapters:
        return
    evidence_node = builder.add_node(
        "evidence",
        "adapters",
        label="adapter evidence",
        provenance="agent_adapters",
        attrs={"adapter_count": len(adapters)},
    )
    for adapter_name, adapter in sorted(adapters.items()):
        if not isinstance(adapter, dict):
            continue
        adapter_node = builder.add_node(
            "evidence",
            f"adapter:{adapter_name}",
            label=adapter_name,
            provenance=f"agent_adapters.{adapter_name}",
            attrs={
                "source": adapter.get("source"),
                "status": adapter.get("status"),
                "ok": adapter.get("ok"),
            },
        )
        builder.add_edge(evidence_node, adapter_node, "contains", provenance="agent_adapters")
        for warning in adapter.get("warnings") or []:
            if isinstance(warning, dict):
                builder.warn(
                    str(warning.get("code") or "ADAPTER_WARNING"),
                    str(warning.get("message") or "adapter warning"),
                    source=adapter.get("source"),
                    adapter=adapter_name,
                )
        if adapter_name.startswith("br"):
            for item in _adapter_payload_items(adapter):
                bead_id = _item_id(item, "id")
                bead_node = builder.add_node(
                    "bead",
                    bead_id,
                    label=str(item.get("title") or bead_id),
                    provenance=f"agent_adapters.{adapter_name}",
                    attrs={key: item[key] for key in ("status", "priority", "issue_type") if key in item},
                )
                builder.add_edge(adapter_node, bead_node, "observed_by", provenance=f"agent_adapters.{adapter_name}")


def _add_cycle_warnings(builder: _GraphBuilder, model: dict[str, Any]) -> None:
    service_ids = {
        str(service.get("id") or "").strip()
        for service in model.get("services") or []
        if isinstance(service, dict) and str(service.get("id") or "").strip()
    }
    task_ids = {
        str(task.get("id") or "").strip()
        for task in model.get("tasks") or []
        if isinstance(task, dict) and str(task.get("id") or "").strip()
    }
    try:
        if service_ids:
            order_service_ids(model, service_ids)
    except RuntimeError as exc:
        builder.warn("SERVICE_DEPENDENCY_CYCLE", str(exc), source="runtime_ops.order_service_ids")
    try:
        if task_ids:
            order_task_ids(model, task_ids)
    except RuntimeError as exc:
        builder.warn("TASK_DEPENDENCY_CYCLE", str(exc), source="runtime_ops.order_task_ids")


def build_agent_graph(
    model: dict[str, Any],
    *,
    adapters: dict[str, Any] | None = None,
) -> AgentGraph:
    builder = _GraphBuilder()
    _add_clients_and_profiles(builder, model)
    _add_checks_skills_mcp(builder, model)
    _add_services_and_tasks(builder, model)
    _add_commands(builder)
    builder.add_node("evidence", "runtime", label="runtime evidence", provenance="runtime_model")
    builder.add_node("snapshot", "current", label="current observation", provenance="agent_graph")
    _add_adapter_evidence(builder, adapters)
    _add_cycle_warnings(builder, model)
    return builder.finish()


def build_agent_graph_payload(
    model: dict[str, Any],
    *,
    adapters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_agent_graph(model, adapters=adapters).to_payload()


def collect_and_build_agent_graph(
    root_dir: Path,
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    ntm_session: str | None = None,
) -> AgentGraph:
    adapters = collect_agent_adapter_evidence(
        root_dir,
        model=model,
        cwd=cwd,
        ntm_session=ntm_session,
    )
    return build_agent_graph(model, adapters=adapters.get("adapters") or {})


__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "PHASE_A_NODE_KINDS",
    "GraphNode",
    "GraphEdge",
    "AgentGraph",
    "graph_node_id",
    "build_agent_graph",
    "build_agent_graph_payload",
    "collect_and_build_agent_graph",
]
