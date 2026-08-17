"""test-plan/v1: the compiled, sealed execution authority (skillbox-sbp-test-plan-compiler-er74).

**The plan is the authority** -- not the yaml, not a profile, not a live command.
Once compiled it is sealed: every downstream decision (placement, dispatch,
retry) reads the plan, and a worker receives a *projection* of it and never
re-reads the repository. That is what makes a run reproducible and auditable
rather than "whatever the tree looked like when the worker got there".

Consequences that shape this module:

* **Deterministic.** The same tree + manifest must compile to a byte-identical
  plan digest. So: canonical JSON, sorted everywhere, no timestamps, no absolute
  host paths, no host identity.
* **Refuse, don't degrade.** Any manifest finding from the schema leaf is a
  compile refusal. A partially-valid plan is worse than none, because it looks
  authoritative.
* **Explain everything.** The plan says why each unit is or is not runnable, and
  names the blocker.

Graph work is delegated to :mod:`runtime_manager.agent_graph_algorithms`
(``normalize_graph``, ``topological_layers`` for waves + SCC cycle evidence,
``blast_radius`` for skip explanations) rather than reimplemented.

A note that is easy to get wrong, and is deliberately pinned by a test:
``critical_path`` in that module is **node-count only** -- every node has weight
1. It is NOT timeout-weighted. This plan therefore reports ``dependency_depth``
(a node count) and ``timeout_ceiling_s`` (a sum of declared ceilings) as two
separate, honestly-labelled numbers. Neither is a runtime estimate, and this
module never presents one.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import agent_graph_algorithms as GA
from . import sbp_test_manifest as manifest_schema

PLAN_SCHEMA = "test-plan/v1"

#: Bumped when the COMPILED SHAPE changes. A worker that does not understand
#: this version must refuse the plan rather than interpret it loosely.
COMPILER_VERSION = "1"
#: Bumped when the host<->worker exchange changes. Separate from the compiler
#: version on purpose: a plan can be recompiled without changing the wire
#: contract, and the wire contract can change without recompiling plans.
RUNNER_PROTOCOL_VERSION = "1"

DEPENDS_ON = "depends_on"


class PlanRefusal(Exception):
    """A typed refusal to compile. The plan is authority; a partial one is not."""

    def __init__(self, code: str, message: str, *, findings: Iterable[Mapping[str, Any]] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.findings = [dict(item) for item in findings]

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error_code": self.code, "error": self.message}
        if self.findings:
            payload["findings"] = self.findings
        return payload


@dataclass(frozen=True)
class Plan:
    """A sealed plan. ``digest`` covers ``content`` exactly."""

    digest: str
    content: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        # digest first so it reads as the identity of what follows.
        return {"plan_digest": self.digest, **self.content}


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Canonical form used for digesting. Stable across hosts and runs."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_plan_digest(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def manifest_digest(repo: Path) -> str:
    """Digest of the manifest bytes as committed to disk."""
    path = Path(repo) / manifest_schema.MANIFEST_RELPATH
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _graph_payload(units: Mapping[str, manifest_schema.Unit]) -> dict[str, Any]:
    """AgentGraph-shaped payload for the shared algorithms.

    A ``depends_on`` edge is stored dependent -> dependency, matching
    ``topological_layers``' documented convention, so dependencies sort first.
    """
    nodes = [{"id": uid, "kind": "test_unit", "attrs": {}} for uid in sorted(units)]
    edges = []
    for uid in sorted(units):
        for dep in sorted(units[uid].depends_on):
            edges.append({"source": uid, "target": dep, "kind": DEPENDS_ON})
    return {"nodes": nodes, "edges": edges}


def _needs(unit: manifest_schema.Unit) -> dict[str, Any]:
    """Placement needs: what a worker must satisfy to host this unit."""
    requires = dict(unit.requires)
    return {
        "os": list(requires.get("os") or []),
        "python": requires.get("python"),
        "caps": list(requires.get("caps") or []),
        "services": list(unit.services),
        "resource_group": unit.resource_group,
        "exclusivity": unit.exclusivity,
    }


def _artifact_rules(unit: manifest_schema.Unit) -> dict[str, Any]:
    return {"collect": list(unit.artifacts)}


def _wave_concurrency(wave: Iterable[str], units: Mapping[str, manifest_schema.Unit]) -> int:
    """How many units in one wave may run at once.

    An ``exclusive`` unit runs alone. Units sharing a ``resource_group`` cannot
    run concurrently with each other, so a group contributes one slot regardless
    of how many members it has. This is a CEILING, not a schedule.
    """
    members = [units[uid] for uid in wave if uid in units]
    if not members:
        return 0
    if any(unit.exclusivity == "exclusive" for unit in members):
        return 1
    slots = 0
    seen_groups: set[str] = set()
    for unit in members:
        if unit.resource_group is None:
            slots += 1
        elif unit.resource_group not in seen_groups:
            seen_groups.add(unit.resource_group)
            slots += 1
    return slots


def compile_plan(
    repo: Path,
    *,
    group: str = manifest_schema.DEFAULT_GROUP,
    source_digests: Mapping[str, str] | None = None,
) -> Plan:
    """Compile a sealed test-plan/v1. Raises :class:`PlanRefusal` on any finding.

    ``source_digests`` binds the plan to the capsule identifiers. It is injected
    rather than computed here so plan mode stays free of side effects and the
    compiler stays deterministic and testable.
    """
    repo = Path(repo)
    manifest, findings = manifest_schema.load_manifest(repo)
    if manifest is None or findings:
        raise PlanRefusal(
            "manifest_invalid",
            "refusing to compile a plan from an invalid manifest; the plan is the "
            "execution authority, so a partial one would be worse than none",
            findings=manifest_schema.findings_payload(findings),
        )

    ordered, plan_findings = manifest_schema.compile_plan(manifest, group)
    if plan_findings:
        raise PlanRefusal(
            "group_uncompilable",
            f"refusing to compile group {group!r}",
            findings=manifest_schema.findings_payload(plan_findings),
        )

    selected = {unit.id: unit for unit in ordered}
    graph = _graph_payload(selected)
    topo = GA.topological_layers(graph, edge_kinds=(DEPENDS_ON,))
    depth = GA.critical_path(graph, edge_kinds=(DEPENDS_ON,))

    cycle_nodes = set(topo.get("cycle_nodes") or [])
    blocked = set(topo.get("blocked_by_cycle_nodes") or [])

    # Skip explanations come from blast_radius so a blocked unit names not just
    # itself but everything it takes down with it.
    downstream: dict[str, list[str]] = {}
    for node in sorted(cycle_nodes | blocked):
        radius = GA.blast_radius(graph, node, edge_kinds=(DEPENDS_ON,))
        # `affected` is a list of records, not bare ids; pull node_id out or the
        # membership test below silently never matches.
        downstream[node] = sorted(
            str(item.get("node_id"))
            for item in (radius.get("affected") or [])
            if isinstance(item, Mapping) and item.get("node_id")
        )

    wave_index = {uid: idx for idx, wave in enumerate(topo.get("layers") or []) for uid in wave}

    planned_units: list[dict[str, Any]] = []
    for uid in sorted(selected):
        unit = selected[uid]
        blockers = sorted(
            node for node, affected in downstream.items() if uid == node or uid in affected
        )
        runnable = not blockers
        if runnable:
            reason = "all declared dependencies resolve and are ordered"
        elif uid in cycle_nodes:
            reason = "participates in a dependency cycle"
        else:
            reason = f"blocked by {blockers[0]}"
        planned_units.append(
            {
                "id": uid,
                "argv": list(unit.command),
                "cwd": unit.cwd,
                "depends_on": list(unit.depends_on),
                "timeout_s": unit.timeout_s,
                "needs": _needs(unit),
                "artifacts": _artifact_rules(unit),
                "env_allowlist": list(unit.env),
                "cache": unit.cache,
                "wave": wave_index.get(uid),
                "runnable": runnable,
                "reason": reason,
                "blocked_by": blockers,
            }
        )

    waves = [sorted(wave) for wave in (topo.get("layers") or [])]
    ceilings = [unit.timeout_s for unit in selected.values() if unit.timeout_s is not None]

    content: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "compiler_version": COMPILER_VERSION,
        "runner_protocol_version": RUNNER_PROTOCOL_VERSION,
        "manifest_schema_version": manifest.schema_version,
        "manifest_digest": manifest_digest(repo),
        "source": dict(sorted((source_digests or {}).items())),
        "group": group,
        "groups": {name: list(members) for name, members in sorted(manifest.groups.items())},
        "units": planned_units,
        "edges": _graph_payload(selected)["edges"],
        "order": list(topo.get("order") or []),
        "waves": waves,
        "concurrency_ceiling": max(
            (_wave_concurrency(wave, selected) for wave in waves), default=0
        ),
        # Two honest numbers, neither a forecast. `dependency_depth` is a NODE
        # COUNT from critical_path (every node weighs 1 there); it is not time.
        # `timeout_ceiling_s` is the sum of declared ceilings, i.e. the longest
        # a run is ALLOWED to take, not how long it will take.
        "dependency_depth": int(depth.get("length") or 0),
        "timeout_ceiling_s": sum(ceilings) if ceilings else None,
        "estimates": None,
        "policy": {
            "refuses_invalid_manifest": True,
            "sealed_before_placement": True,
            "workers_reread_repo": False,
            "units_without_timeout": sorted(
                uid for uid in selected if selected[uid].timeout_s is None
            ),
        },
        "summary": {
            "unit_count": len(planned_units),
            "runnable_count": sum(1 for unit in planned_units if unit["runnable"]),
            "skipped_count": sum(1 for unit in planned_units if not unit["runnable"]),
            "wave_count": len(waves),
        },
    }
    return Plan(digest=compute_plan_digest(content), content=content)


def projection(plan: Plan, unit_id: str) -> dict[str, Any]:
    """What a worker receives for one unit. It never re-reads the repository.

    Carries the plan digest and source identifiers so the worker can prove which
    plan and which source it acted on, and deliberately carries nothing that
    would let it substitute its own view of the tree.
    """
    for unit in plan.content["units"]:
        if unit["id"] == unit_id:
            break
    else:
        raise PlanRefusal("unknown_unit", f"unit {unit_id!r} is not in this plan")
    return {
        "schema": PLAN_SCHEMA,
        "plan_digest": plan.digest,
        "runner_protocol_version": plan.content["runner_protocol_version"],
        "source": dict(plan.content["source"]),
        "unit": {
            "id": unit["id"],
            "argv": list(unit["argv"]),
            "cwd": unit["cwd"],
            "timeout_s": unit["timeout_s"],
            "needs": dict(unit["needs"]),
            "artifacts": dict(unit["artifacts"]),
            "env_allowlist": list(unit["env_allowlist"]),
        },
    }
