"""Shared command registry and capabilities ABI for the agent ops brain.

Describes agent-facing commands and tools once: id, surfaces, typed input and
output schemas, side-effect class, risk class, entrypoint, scoping, examples,
validations, and graph node references, so CLI help, ``capabilities`` output,
MCP tool declarations, generated docs, and golden tests can all consume one
source of truth instead of re-declaring every surface.

Registry tiers:

- Tier 1: full ABI for the new agent_ops_brain commands and their in-box MCP
  mirrors. Requires non-empty examples, validations, and graph node refs.
- Tier 2: descriptive coverage for existing CLI commands, Make targets,
  wrapper surfaces, and MCP tools. Optional fields may be omitted.
- Tier 3 (generated legacy MCP declarations) is a follow-on and is not
  represented here.

This module is standard-library only and imports nothing from the rest of
``runtime_manager``; consumers merge :func:`registry_payload` additively under
a ``registry`` key without touching existing ``capabilities`` top-level keys.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

REGISTRY_ABI_VERSION = "2026-06-11+agent_ops_brain"

SURFACES = frozenset({"cli", "mcp", "make", "internal"})
SIDE_EFFECTS = frozenset({"none", "local_write", "service", "network", "destructive"})
RISKS = frozenset({"low", "medium", "high", "destructive"})
TIERS = frozenset({1, 2})
SCOPE_KINDS = frozenset({"client", "profile", "service", "task", "box", "cwd"})
OWNER_BINARIES = frozenset({"sbp", "sbo"})
KNOWN_ENTRYPOINTS = frozenset(
    {
        "manage.py",
        "04-reconcile.py",
        "box.py",
        "mcp_server.py",
        "pulse.py",
        "Makefile",
    }
)
# Minimum node kinds from the agent_ops_brain backend spec graph contract.
GRAPH_NODE_KINDS = frozenset(
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
        "box",
        "evidence",
        "doc",
        "log",
        "snapshot",
        "diff",
    }
)

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_MCP_TOOL_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_TYPE_PATTERN = re.compile(
    r"^(string|boolean|integer|number|object|enum\[[A-Za-z0-9_.|-]+\])(\[\])?\??$"
)

# Serialization key order is part of the contract; golden fixtures depend on it.
_PAYLOAD_KEY_ORDER = (
    "id",
    "tier",
    "surface",
    "summary",
    "inputs",
    "outputs",
    "side_effect",
    "risk",
    "entrypoint",
    "owner_binary",
    "mcp_tool",
    "scopes",
    "examples",
    "validations",
    "graph_nodes",
)
_OPTIONAL_PAYLOAD_KEYS = ("owner_binary", "mcp_tool", "scopes", "validations", "graph_nodes")


class RegistryValidationError(RuntimeError):
    """Raised when the seeded registry fails its own ABI validation."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("command registry validation failed:\n" + "\n".join(issues))
        self.issues = issues


@dataclass(frozen=True)
class CommandSpec:
    """One agent-facing command/tool entry in the shared registry ABI."""

    id: str
    tier: int
    surface: tuple[str, ...]
    summary: str
    inputs: Mapping[str, Any]
    outputs: Mapping[str, Any]
    side_effect: str
    risk: str
    entrypoint: str
    examples: tuple[str, ...]
    owner_binary: str | None = None
    mcp_tool: str | None = None
    scopes: tuple[str, ...] = ()
    validations: tuple[str, ...] = ()
    graph_nodes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Serialize with stable key order, omitting unset optional fields."""
        raw: dict[str, Any] = {
            "id": self.id,
            "tier": self.tier,
            "surface": list(self.surface),
            "summary": self.summary,
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "side_effect": self.side_effect,
            "risk": self.risk,
            "entrypoint": self.entrypoint,
            "owner_binary": self.owner_binary,
            "mcp_tool": self.mcp_tool,
            "scopes": list(self.scopes),
            "examples": list(self.examples),
            "validations": list(self.validations),
            "graph_nodes": list(self.graph_nodes),
        }
        payload: dict[str, Any] = {}
        for key in _PAYLOAD_KEY_ORDER:
            value = raw[key]
            if key in _OPTIONAL_PAYLOAD_KEYS and (value is None or value == []):
                continue
            payload[key] = value
        return payload


def spec_from_payload(payload: Mapping[str, Any]) -> CommandSpec:
    """Rebuild a spec from its serialized payload.

    Missing optional fields fall back to their defaults; unknown keys are
    ignored for forward compatibility with additive payload extensions.
    """
    surface = payload.get("surface", ())
    if isinstance(surface, str):
        surface = (surface,)
    return CommandSpec(
        id=str(payload.get("id", "")),
        tier=int(payload.get("tier", 2)),
        surface=tuple(surface),
        summary=str(payload.get("summary", "")),
        inputs=dict(payload.get("inputs") or {}),
        outputs=dict(payload.get("outputs") or {}),
        side_effect=str(payload.get("side_effect", "")),
        risk=str(payload.get("risk", "")),
        entrypoint=str(payload.get("entrypoint", "")),
        examples=tuple(payload.get("examples") or ()),
        owner_binary=payload.get("owner_binary"),
        mcp_tool=payload.get("mcp_tool"),
        scopes=tuple(payload.get("scopes") or ()),
        validations=tuple(payload.get("validations") or ()),
        graph_nodes=tuple(payload.get("graph_nodes") or ()),
    )


def _schema_issues(prefix: str, schema: Any) -> list[str]:
    issues: list[str] = []
    if not isinstance(schema, Mapping):
        return [f"{prefix} must be a mapping of field name to type declaration"]
    for key, value in schema.items():
        if not isinstance(key, str) or not key:
            issues.append(f"{prefix} has a non-string or empty field name: {key!r}")
            continue
        if isinstance(value, Mapping):
            issues.extend(_schema_issues(f"{prefix}.{key}", value))
        elif isinstance(value, str):
            if not _TYPE_PATTERN.match(value):
                issues.append(f"{prefix}.{key} has invalid type declaration {value!r}")
        else:
            issues.append(
                f"{prefix}.{key} must be a type string or nested mapping, got {type(value).__name__}"
            )
    return issues


def validate_spec(spec: CommandSpec) -> list[str]:
    """Return human-readable ABI issues for one spec; empty when valid."""
    issues: list[str] = []
    ref = spec.id or "<missing-id>"

    if not spec.id or not _ID_PATTERN.match(spec.id):
        issues.append(f"{ref}: id must match {_ID_PATTERN.pattern}")
    if spec.tier not in TIERS:
        issues.append(f"{ref}: tier must be one of {sorted(TIERS)}, got {spec.tier!r}")
    if not spec.surface:
        issues.append(f"{ref}: surface must declare at least one of {sorted(SURFACES)}")
    for surface in spec.surface:
        if surface not in SURFACES:
            issues.append(f"{ref}: unknown surface {surface!r}")
    if not spec.summary.strip() or "\n" in spec.summary:
        issues.append(f"{ref}: summary must be a non-empty single line")
    issues.extend(_schema_issues(f"{ref}: inputs", spec.inputs))
    issues.extend(_schema_issues(f"{ref}: outputs", spec.outputs))
    if spec.side_effect not in SIDE_EFFECTS:
        issues.append(f"{ref}: unknown side_effect {spec.side_effect!r}")
    if spec.risk not in RISKS:
        issues.append(f"{ref}: unknown risk {spec.risk!r}")
    if spec.side_effect == "destructive" and spec.risk != "destructive":
        issues.append(f"{ref}: destructive side_effect requires destructive risk")
    if spec.risk == "destructive" and spec.side_effect != "destructive":
        issues.append(f"{ref}: destructive risk requires destructive side_effect")
    if spec.side_effect == "none" and spec.risk not in {"low", "medium"}:
        issues.append(f"{ref}: side_effect none cannot carry {spec.risk!r} risk")
    if spec.entrypoint not in KNOWN_ENTRYPOINTS:
        issues.append(f"{ref}: unknown entrypoint {spec.entrypoint!r}")
    if spec.owner_binary is not None and spec.owner_binary not in OWNER_BINARIES:
        issues.append(f"{ref}: unknown owner_binary {spec.owner_binary!r}")
    if spec.mcp_tool is not None:
        if not _MCP_TOOL_PATTERN.match(spec.mcp_tool):
            issues.append(f"{ref}: mcp_tool must match {_MCP_TOOL_PATTERN.pattern}")
        if "mcp" not in spec.surface:
            issues.append(f"{ref}: mcp_tool is set but surface does not include mcp")
    for scope in spec.scopes:
        if scope not in SCOPE_KINDS:
            issues.append(f"{ref}: unknown scope {scope!r}")
    if not spec.examples or any(not e.strip() for e in spec.examples):
        issues.append(f"{ref}: examples must be non-empty strings and at least one is required")
    for node in spec.graph_nodes:
        if node not in GRAPH_NODE_KINDS:
            issues.append(f"{ref}: unknown graph node kind {node!r}")

    if spec.tier == 1:
        if not spec.inputs:
            issues.append(f"{ref}: tier 1 entries must declare an inputs schema")
        if not spec.outputs:
            issues.append(f"{ref}: tier 1 entries must declare an outputs schema")
        if not spec.validations:
            issues.append(f"{ref}: tier 1 entries must declare validations")
        if not spec.graph_nodes:
            issues.append(f"{ref}: tier 1 entries must declare graph_nodes")
        if "mcp" in spec.surface and not spec.mcp_tool:
            issues.append(f"{ref}: tier 1 mcp surfaces must declare mcp_tool")
    return issues


# Tier 1: agent_ops_brain commands from the shared contract. Completeness over
# these ids is enforced so downstream brain workers cannot lose registry cover.
REQUIRED_TIER1_IDS = frozenset(
    {
        "runtime.capabilities",
        "brain.next",
        "brain.graph",
        "brain.explain",
        "brain.search",
        "brain.snap",
    }
)
# Tier 2: existing surfaces this slice promises descriptive coverage for.
REQUIRED_TIER2_IDS = frozenset(
    {
        "runtime.status",
        "runtime.doctor",
        "runtime.structure_doctor",
        "runtime.render",
        "runtime.evidence",
        "runtime.skills",
        "runtime.mcp_audit",
        "runtime.pressure_report",
        "runtime.sync",
        "runtime.up",
        "runtime.down",
        "runtime.logs",
        "outer.render",
        "outer.doctor",
        "make.dev_sanity",
        "box.status",
        "box.up",
        "box.down",
    }
)
REQUIRED_COMMAND_IDS = REQUIRED_TIER1_IDS | REQUIRED_TIER2_IDS


def validate_registry(specs: Iterable[CommandSpec]) -> list[str]:
    """Validate every spec plus registry-level uniqueness and completeness."""
    specs = list(specs)
    issues: list[str] = []
    seen_ids: set[str] = set()
    seen_tools: dict[str, str] = {}
    for spec in specs:
        issues.extend(validate_spec(spec))
        if spec.id in seen_ids:
            issues.append(f"{spec.id}: duplicate command id")
        seen_ids.add(spec.id)
        if spec.mcp_tool:
            if spec.mcp_tool in seen_tools:
                issues.append(
                    f"{spec.id}: mcp_tool {spec.mcp_tool!r} already declared by {seen_tools[spec.mcp_tool]}"
                )
            else:
                seen_tools[spec.mcp_tool] = spec.id
    tier1_ids = {s.id for s in specs if s.tier == 1}
    for required in sorted(REQUIRED_TIER1_IDS):
        if required not in tier1_ids:
            issues.append(f"registry: missing required tier 1 entry {required!r}")
    for required in sorted(REQUIRED_TIER2_IDS):
        if required not in seen_ids:
            issues.append(f"registry: missing required tier 2 entry {required!r}")
    return issues


_REGISTRY_TEST = "python3 -m unittest tests.test_agent_ops_command_registry"
_FORMAT_JSON_TEXT = "enum[json|text]?"


def default_registry() -> tuple[CommandSpec, ...]:
    """Seeded registry: Tier 1 brain commands plus Tier 2 existing surfaces."""
    return (
        # ---- Tier 1: agent_ops_brain commands and their in-box MCP mirrors ----
        CommandSpec(
            id="runtime.capabilities",
            tier=1,
            surface=("cli", "mcp"),
            summary="List agent-facing commands and tools with risk, side effects, and output contracts.",
            inputs={"format": "enum[json]?", "compact": "boolean?"},
            outputs={
                "ok": "boolean",
                "contract_version": "string",
                "entrypoints": "string[]",
                "commands": "object[]",
                "registry": "object",
            },
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            owner_binary="sbp",
            mcp_tool="skillbox_capabilities",
            examples=("python3 .env-manager/manage.py capabilities --format json",),
            validations=(_REGISTRY_TEST,),
            graph_nodes=("command", "mcp_tool"),
        ),
        CommandSpec(
            id="brain.next",
            tier=1,
            surface=("cli", "mcp"),
            summary="Rank the highest-leverage next actions with reasons, claim commands, and validations.",
            inputs={
                "client": "string?",
                "profile": "string[]?",
                "limit": "integer?",
                "format": _FORMAT_JSON_TEXT,
            },
            outputs={
                "ok": "boolean",
                "context": "object",
                "recommendations": "object[]",
                "blockers": "object[]",
            },
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_next",
            scopes=("client", "profile"),
            examples=("python3 .env-manager/manage.py next --client skillbox --format json --limit 5",),
            validations=(_REGISTRY_TEST,),
            graph_nodes=("bead", "check", "command", "evidence", "service"),
        ),
        CommandSpec(
            id="brain.graph",
            tier=1,
            surface=("cli", "mcp"),
            summary="Expose the typed runtime graph and algorithm outputs such as cycles and critical path.",
            inputs={
                "algorithm": (
                    "enum[topology|cycles|scc|critical-path|blast-radius|"
                    "min-unblock|shortest-path|all]?"
                ),
                "node": "string?",
                "source": "string?",
                "target": "string?",
                "blocked-node": "string?",
                "client": "string?",
                "format": _FORMAT_JSON_TEXT,
            },
            outputs={"ok": "boolean", "nodes": "object[]", "edges": "object[]", "result": "object"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_graph",
            scopes=("client", "profile"),
            examples=("python3 .env-manager/manage.py graph --algorithm critical-path --format json",),
            validations=(_REGISTRY_TEST,),
            graph_nodes=(
                "client",
                "profile",
                "repo",
                "service",
                "task",
                "check",
                "skill",
                "mcp_tool",
                "command",
                "bead",
            ),
        ),
        CommandSpec(
            id="brain.explain",
            tier=1,
            surface=("cli", "mcp"),
            summary="Explain one graph node - command, check, service, bead, skill, or MCP tool - with evidence.",
            inputs={"target": "string", "format": _FORMAT_JSON_TEXT},
            outputs={
                "ok": "boolean",
                "node": "object",
                "summary": "string",
                "incoming": "string[]",
                "outgoing": "string[]",
                "evidence": "object[]",
                "next_actions": "object[]",
            },
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_explain",
            examples=("python3 .env-manager/manage.py explain check:runtime-doctor --format json",),
            validations=(_REGISTRY_TEST,),
            graph_nodes=("check", "service", "bead", "skill", "mcp_tool", "command", "evidence"),
        ),
        CommandSpec(
            id="brain.search",
            tier=1,
            surface=("cli", "mcp"),
            summary="Find commands, graph nodes, docs, Beads, checks, logs, and evidence by query.",
            inputs={"query": "string", "limit": "integer?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "results": "object[]"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_search",
            examples=("python3 .env-manager/manage.py search \"mcp parity\" --format json",),
            validations=(_REGISTRY_TEST,),
            graph_nodes=("command", "doc", "bead", "check", "log", "evidence"),
        ),
        CommandSpec(
            id="brain.snap",
            tier=1,
            surface=("cli", "mcp"),
            summary="Record, diff, and replay runtime observation snapshots inside declared snapshot paths.",
            inputs={
                "action": "enum[create|diff|replay]",
                "name": "string?",
                "from": "string?",
                "to": "string?",
                "fixture": "string?",
                "format": _FORMAT_JSON_TEXT,
            },
            outputs={"ok": "boolean", "snapshot": "object?", "diff": "object?"},
            side_effect="local_write",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_snap",
            examples=(
                "python3 .env-manager/manage.py snap create --name before-agent-ops --format json",
                "python3 .env-manager/manage.py snap diff --from before.json --to after.json --format json",
                "python3 .env-manager/manage.py snap replay tests/goldens/agent_ops_snapshot.json --format json",
            ),
            validations=(_REGISTRY_TEST,),
            graph_nodes=("snapshot", "diff", "evidence"),
        ),
        # ---- Tier 2: existing runtime CLI / MCP surfaces ----
        CommandSpec(
            id="runtime.status",
            tier=2,
            surface=("cli", "mcp"),
            summary="Summarize repo, artifact, skill, task, service, log, and health state for the active scope.",
            inputs={"client": "string?", "profile": "string[]?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "services": "object[]", "tasks": "object[]", "repos": "object[]"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_status",
            scopes=("client", "profile", "service"),
            examples=("python3 .env-manager/manage.py status --format json",),
            validations=("python3 -m unittest tests.test_cli_units",),
            graph_nodes=("service", "task", "repo", "check"),
        ),
        CommandSpec(
            id="runtime.doctor",
            tier=2,
            surface=("cli", "mcp"),
            summary="Validate the internal repos/skills/logs/check graph for the selected scope.",
            inputs={"client": "string?", "profile": "string[]?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "checks": "object[]", "next_actions": "string[]"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_doctor",
            scopes=("client", "profile"),
            examples=("python3 .env-manager/manage.py doctor --format json",),
            validations=("python3 -m unittest tests.test_runtime_manager",),
            graph_nodes=("check", "repo", "skill", "log"),
        ),
        CommandSpec(
            id="runtime.structure_doctor",
            tier=2,
            surface=("cli",),
            summary="One front door for all STRUCTURAL gates with INCO/FAIL/PASS; invokes the runtime doctor as a runtime gate.",
            inputs={"cwd": "string?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "gates": "object[]", "summary": "object", "exit_code": "integer"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            owner_binary="sbp",
            scopes=("cwd",),
            examples=("python3 .env-manager/manage.py structure-doctor --cwd \"$PWD\" --format json",),
            validations=("python3 -m pytest tests/ -k doctor",),
            graph_nodes=("check", "skill", "mcp_tool"),
        ),
        CommandSpec(
            id="runtime.cass_evidence",
            tier=2,
            surface=("cli",),
            summary=(
                "Measure skill invocations per repo from Cass, joined to policy; "
                "INCO when down. --proposals: read-only demote/promote candidates."
            ),
            inputs={
                "repo": "string?",
                "skill": "string?",
                "format": "enum[json|text]?",
                "proposals": "boolean?",
            },
            outputs={
                "command": "string",
                "cass_available": "boolean",
                "status": "string",
                "structural_signals": "string[]",
                "repos": "object[]",
                "demotions": "object[]",
                "promotions": "object[]",
                "applied": "boolean",
            },
            side_effect="network",
            risk="low",
            entrypoint="manage.py",
            owner_binary="sbp",
            scopes=("cwd",),
            examples=(
                "sbp evidence --repo /srv/skillbox/repos/example-app --format json",
                "sbp evidence --proposals --format json",
            ),
            validations=("cd skillbox-config && python3 -m pytest tests/ -k evidence",),
            graph_nodes=("evidence", "skill", "repo"),
        ),
        CommandSpec(
            id="runtime.render",
            tier=2,
            surface=("cli", "mcp"),
            summary="Print the resolved internal runtime graph.",
            inputs={"client": "string?", "profile": "string[]?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "model": "object"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_render",
            scopes=("client", "profile"),
            examples=("python3 .env-manager/manage.py render --format json",),
        ),
        CommandSpec(
            id="runtime.evidence",
            tier=2,
            surface=("cli",),
            summary="Read-only runtime evidence packet with stable keys and explicit blocked/gray conditions.",
            inputs={"cwd": "string?", "profile": "string[]?", "write": "boolean?", "format": "enum[text|json|md]?"},
            outputs={
                "kind": "string",
                "scope": "object",
                "sections": "object",
                "blocked_conditions": "object[]",
                "next_actions": "string[]",
                "overall": "string",
            },
            side_effect="local_write",
            risk="low",
            entrypoint="manage.py",
            scopes=("cwd", "profile"),
            examples=("python3 .env-manager/manage.py evidence --cwd \"$PWD\" --format json",),
            validations=("python3 -m unittest tests.test_runtime_evidence",),
            graph_nodes=("evidence", "check", "service", "skill", "mcp_tool"),
        ),
        CommandSpec(
            id="runtime.skills",
            tier=2,
            surface=("cli", "mcp"),
            summary="Show effective skill visibility across global, client, and project-local layers.",
            inputs={"client": "string?", "profile": "string[]?", "cwd": "string?", "issues_only": "boolean?"},
            outputs={"ok": "boolean", "skills": "object[]", "issues": "object[]"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            owner_binary="sbp",
            mcp_tool="skillbox_skills",
            scopes=("client", "profile", "cwd"),
            examples=("python3 .env-manager/manage.py skills --client personal --cwd \"$PWD\"",),
            validations=("python3 -m unittest tests.test_skill_visibility",),
            graph_nodes=("skill", "client", "profile"),
        ),
        CommandSpec(
            id="runtime.explain",
            tier=2,
            surface=("cli", "mcp"),
            summary="Explain skill visibility provenance for one skill at one cwd: layer, scope rule, losers, and ranked fixes.",
            inputs={
                "target": "string",
                "skill": "boolean?",
                "node": "boolean?",
                "cwd": "string?",
                "format": _FORMAT_JSON_TEXT,
            },
            outputs={
                "skill": "string",
                "visible": "boolean",
                "layer": "string?",
                "scope_rules": "object[]",
                "lost": "object[]",
                "remediation": "object[]",
            },
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            owner_binary="sbp",
            mcp_tool="skillbox_explain_skill",
            scopes=("client", "profile", "cwd"),
            examples=("python3 .env-manager/manage.py explain wiki --cwd \"$PWD\" --format json",),
            validations=("python3 -m pytest tests/ -k explain",),
            graph_nodes=("skill", "client", "profile"),
        ),
        CommandSpec(
            id="runtime.mcp_audit",
            tier=2,
            surface=("cli", "mcp"),
            summary="Audit Claude/Codex MCP config parity; only undeclared servers count as drift.",
            inputs={"cwd": "string?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "servers": "object[]", "unexplained_drift": "object[]"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_mcp_audit",
            scopes=("cwd",),
            examples=("python3 .env-manager/manage.py mcp-audit --cwd \"$PWD\" --format json",),
            validations=("python3 -m unittest tests.test_mcp_visibility",),
            graph_nodes=("mcp_tool", "service"),
        ),
        CommandSpec(
            id="runtime.mcp_sync",
            tier=2,
            surface=("cli",),
            summary="Render .mcp.json + .codex/config.toml from one MCP declaration; preserves operator entries.",
            inputs={"cwd": "string?", "config_root": "string?", "apply": "boolean?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "surfaces": "object", "written": "string[]"},
            side_effect="local_write",
            risk="low",
            entrypoint="manage.py",
            scopes=("cwd",),
            examples=("python3 .env-manager/manage.py mcp sync --cwd \"$PWD\" --dry-run --format json",),
            validations=("python3 -m unittest tests.test_mcp_render",),
            graph_nodes=("mcp_tool", "service"),
        ),
        CommandSpec(
            id="runtime.fleet_converge",
            tier=2,
            surface=("cli",),
            summary="Per-repo heal PLAN over the deduped fleet, grouped by triage class; each action carries its exact single-repo command. Plan only — never writes.",
            inputs={
                "cwd": "string?",
                "scan_root": "string[]?",
                "max_depth": "integer?",
                "all": "boolean?",
                "no_mcp": "boolean?",
                "format": _FORMAT_JSON_TEXT,
            },
            outputs={
                "kind": "string",
                "dry_run": "boolean",
                "classes": "string[]",
                "summary": "object",
                "repos": "object[]",
                "next_actions": "string[]",
            },
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            owner_binary="sbp",
            scopes=("client", "profile", "cwd"),
            examples=("python3 .env-manager/manage.py fleet converge --dry-run --format json",),
            validations=("python3 -m pytest tests/ -k converge",),
            graph_nodes=("skill", "repo", "mcp_tool", "command"),
        ),
        CommandSpec(
            id="runtime.fleet_relink",
            tier=2,
            surface=("cli",),
            summary="Machine-migration rewrite: repoint other-machine links old-root->new-root when translated target is valid; else leave for converge.",
            inputs={
                "from_root": "string?",
                "to_root": "string?",
                "cwd": "string?",
                "scan_root": "string[]?",
                "max_depth": "integer?",
                "all": "boolean?",
                "apply": "boolean?",
                "format": _FORMAT_JSON_TEXT,
            },
            outputs={
                "kind": "string",
                "dry_run": "boolean",
                "roots": "object",
                "decisions": "string[]",
                "summary": "object",
                "repos": "object[]",
                "next_actions": "string[]",
            },
            side_effect="local_write",
            risk="medium",
            entrypoint="manage.py",
            owner_binary="sbp",
            scopes=("client", "profile", "cwd"),
            examples=("python3 .env-manager/manage.py fleet relink --dry-run --format json",),
            validations=("python3 -m pytest tests/test_fleet_relink.py",),
            graph_nodes=("skill", "repo", "command"),
        ),
        CommandSpec(
            id="runtime.ports",
            tier=2,
            surface=("cli", "mcp"),
            summary="List the machine-readable port registry for the active scope: owner, source, bind scope, and profiles.",
            inputs={
                "resolve": "string?",
                "client": "string?",
                "profile": "string[]?",
                "format": _FORMAT_JSON_TEXT,
            },
            outputs={
                "ok": "boolean",
                "count": "integer",
                "entries": "object[]",
                "warnings": "object[]",
            },
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_ports",
            scopes=("client", "profile", "service"),
            examples=(
                "python3 .env-manager/manage.py ports --format json",
                "python3 .env-manager/manage.py ports --resolve api-stub --format json",
            ),
            validations=("python3 -m unittest tests.test_agent_ops_port_registry",),
            graph_nodes=("service", "command"),
        ),
        CommandSpec(
            id="runtime.pressure_report",
            tier=2,
            surface=("cli",),
            summary="Read-only disk pressure, protected buckets, worker target, and RCH/SBH posture.",
            inputs={"format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "pressure": "object", "next_actions": "string[]"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            examples=("python3 .env-manager/manage.py pressure-report --format json",),
            validations=("python3 -m unittest tests.test_pressure_report",),
            graph_nodes=("evidence",),
        ),
        CommandSpec(
            id="runtime.sync",
            tier=2,
            surface=("cli", "mcp"),
            summary="Create managed repo/artifact/log directories and install declared skills for the scope.",
            inputs={"client": "string?", "profile": "string[]?", "dry_run": "boolean?"},
            outputs={"ok": "boolean", "steps": "object[]"},
            side_effect="local_write",
            risk="medium",
            entrypoint="manage.py",
            mcp_tool="skillbox_sync",
            scopes=("client", "profile"),
            examples=("python3 .env-manager/manage.py sync --client personal --dry-run",),
            graph_nodes=("repo", "artifact", "skill", "log"),
        ),
        CommandSpec(
            id="runtime.up",
            tier=2,
            surface=("cli", "mcp"),
            summary="Sync runtime state, run service bootstrap tasks, and start manageable services.",
            inputs={"client": "string?", "profile": "string[]?", "service": "string[]?", "dry_run": "boolean?"},
            outputs={"ok": "boolean", "steps": "object[]", "services": "object[]"},
            side_effect="service",
            risk="medium",
            entrypoint="manage.py",
            mcp_tool="skillbox_up",
            scopes=("client", "profile", "service"),
            examples=("python3 .env-manager/manage.py up --profile surfaces --service api-stub",),
            graph_nodes=("service", "task"),
        ),
        CommandSpec(
            id="runtime.down",
            tier=2,
            surface=("cli", "mcp"),
            summary="Stop manageable services, stopping selected dependents before their prerequisites.",
            inputs={"client": "string?", "profile": "string[]?", "service": "string[]?", "dry_run": "boolean?"},
            outputs={"ok": "boolean", "steps": "object[]"},
            side_effect="service",
            risk="medium",
            entrypoint="manage.py",
            mcp_tool="skillbox_down",
            scopes=("client", "profile", "service"),
            examples=("python3 .env-manager/manage.py down --profile surfaces --service api-stub",),
            graph_nodes=("service",),
        ),
        CommandSpec(
            id="runtime.logs",
            tier=2,
            surface=("cli", "mcp"),
            summary="Print recent log output for declared services.",
            inputs={"client": "string?", "profile": "string[]?", "service": "string[]?", "lines": "integer?"},
            outputs={"ok": "boolean", "logs": "object[]"},
            side_effect="none",
            risk="low",
            entrypoint="manage.py",
            mcp_tool="skillbox_logs",
            scopes=("client", "profile", "service"),
            examples=("python3 .env-manager/manage.py logs --profile surfaces --service api-stub --lines 80",),
            graph_nodes=("log", "service"),
        ),
        # ---- Tier 2: outer reconcile, Make, and box lifecycle surfaces ----
        CommandSpec(
            id="outer.render",
            tier=2,
            surface=("cli",),
            summary="Print the resolved outer sandbox model.",
            inputs={"with_compose": "boolean?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "model": "object"},
            side_effect="none",
            risk="low",
            entrypoint="04-reconcile.py",
            examples=("python3 scripts/04-reconcile.py render --with-compose",),
            validations=("python3 -m unittest tests.test_reconcile",),
        ),
        CommandSpec(
            id="outer.doctor",
            tier=2,
            surface=("cli",),
            summary="Run outer repo drift and readiness checks: manifests, Compose wiring, skill-repo sync.",
            inputs={"skip_compose": "boolean?", "skip_skill_sync": "boolean?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "checks": "object[]"},
            side_effect="none",
            risk="low",
            entrypoint="04-reconcile.py",
            examples=("python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync",),
            validations=("python3 -m unittest tests.test_reconcile",),
            graph_nodes=("check",),
        ),
        CommandSpec(
            id="make.dev_sanity",
            tier=2,
            surface=("make",),
            summary="Validate the internal runtime graph, filesystem readiness, and managed skill integrity.",
            inputs={},
            outputs={"ok": "boolean", "checks": "object[]"},
            side_effect="none",
            risk="low",
            entrypoint="Makefile",
            examples=("make dev-sanity",),
            graph_nodes=("check",),
        ),
        CommandSpec(
            id="box.status",
            tier=2,
            surface=("cli", "make"),
            summary="Health-check a remote box: phone URL, public SSH, Tailnet ping, MagicDNS, posture violations.",
            inputs={"box_id": "string", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "checks": "object[]", "violations": "object[]"},
            side_effect="network",
            risk="low",
            entrypoint="box.py",
            scopes=("box",),
            examples=("make box-status BOX=<id>",),
            validations=("python3 -m unittest tests.test_box_lifecycle",),
            graph_nodes=("box", "check"),
        ),
        CommandSpec(
            id="box.up",
            tier=2,
            surface=("cli", "make"),
            summary="Provision a new remote box (DigitalOcean + Tailscale) or resume a partial provision.",
            inputs={"box_id": "string", "profile": "string?", "dry_run": "boolean?", "resume": "boolean?"},
            outputs={"ok": "boolean", "steps": "object[]", "box": "object"},
            side_effect="network",
            risk="high",
            entrypoint="box.py",
            scopes=("box", "profile"),
            examples=("python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json",),
            validations=("python3 -m unittest tests.test_box_lifecycle",),
            graph_nodes=("box",),
        ),
        CommandSpec(
            id="box.down",
            tier=2,
            surface=("cli", "make"),
            summary="Tear down a remote box, destroying the droplet and removing Tailscale enrollment.",
            inputs={"box_id": "string", "dry_run": "boolean?", "format": _FORMAT_JSON_TEXT},
            outputs={"ok": "boolean", "steps": "object[]"},
            side_effect="destructive",
            risk="destructive",
            entrypoint="box.py",
            scopes=("box",),
            examples=("python3 scripts/box.py down <box-id> --dry-run --format json",),
            validations=("python3 -m unittest tests.test_box_lifecycle",),
            graph_nodes=("box",),
        ),
    )


def load_default_registry() -> dict[str, CommandSpec]:
    """Return the validated seed registry keyed by id; raise on ABI drift."""
    specs = default_registry()
    issues = validate_registry(specs)
    if issues:
        raise RegistryValidationError(issues)
    return {spec.id: spec for spec in specs}


def registry_payload(specs: Iterable[CommandSpec] | None = None) -> dict[str, Any]:
    """Additive ``registry`` section for the existing capabilities payload.

    Existing capabilities top-level keys stay untouched; consumers attach this
    payload under a new ``registry`` key.
    """
    if specs is None:
        specs = default_registry()
    specs = sorted(specs, key=lambda spec: spec.id)
    return {
        "abi_version": REGISTRY_ABI_VERSION,
        "counts": {
            "total": len(specs),
            "tier1": sum(1 for spec in specs if spec.tier == 1),
            "tier2": sum(1 for spec in specs if spec.tier == 2),
        },
        "capabilities": [spec.to_payload() for spec in specs],
    }


def registry_text_lines(specs: Iterable[CommandSpec] | None = None) -> list[str]:
    """Compact text view for CLI help; one line per entry plus a header."""
    if specs is None:
        specs = default_registry()
    specs = sorted(specs, key=lambda spec: spec.id)
    tier1 = sum(1 for spec in specs if spec.tier == 1)
    lines = [
        f"command registry {REGISTRY_ABI_VERSION} "
        f"({len(specs)} entries; tier1 {tier1}, tier2 {len(specs) - tier1})"
    ]
    for spec in specs:
        surfaces = ",".join(spec.surface)
        lines.append(
            f"  {spec.id:<26} [{surfaces}] risk={spec.risk} effect={spec.side_effect} {spec.summary}"
        )
    return lines


def replace_spec(spec: CommandSpec, **changes: Any) -> CommandSpec:
    """Convenience wrapper for building spec variants in tests and fixtures."""
    return dataclasses.replace(spec, **changes)


__all__ = [
    "REGISTRY_ABI_VERSION",
    "SURFACES",
    "SIDE_EFFECTS",
    "RISKS",
    "TIERS",
    "SCOPE_KINDS",
    "OWNER_BINARIES",
    "KNOWN_ENTRYPOINTS",
    "GRAPH_NODE_KINDS",
    "REQUIRED_TIER1_IDS",
    "REQUIRED_TIER2_IDS",
    "REQUIRED_COMMAND_IDS",
    "CommandSpec",
    "RegistryValidationError",
    "spec_from_payload",
    "validate_spec",
    "validate_registry",
    "default_registry",
    "load_default_registry",
    "registry_payload",
    "registry_text_lines",
    "replace_spec",
]
