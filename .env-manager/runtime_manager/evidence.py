"""Read-only runtime evidence packet.

Assembles the validated proof lane — doctor, status, pressure, pulse, skills,
MCP parity, git dirty, and a Beads pointer — into a single machine-readable
packet with stable top-level keys and explicit blocked/gray conditions. It only
*reads*: it composes existing read-only surfaces (``doctor``, ``status``,
``pressure``, ``skills``, ``mcp-audit``) and never starts services, syncs, or
provisions. Future agents can capture one packet instead of stitching the
individual command outputs by hand.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .shared import (
    git_dirty_paths,
    next_actions_for_doctor,
    next_actions_for_status,
    run_command,
)
from .runtime_ops import doctor_results, runtime_status, service_paths
from .skill_visibility import collect_skill_visibility
from .mcp_visibility import collect_mcp_audit

# Stable top-level keys; tests and downstream consumers depend on this contract.
EVIDENCE_TOP_LEVEL_KEYS = (
    "kind",
    "scope",
    "sections",
    "blocked_conditions",
    "next_actions",
    "overall",
)

_MAX_DIRTY_PATHS = 25


def _doctor_section(model: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    results = doctor_results(model, root_dir)
    failures = [r.code for r in results if r.status == "fail"]
    warnings = [r.code for r in results if r.status == "warn"]
    status = "fail" if failures else "warn" if warnings else "pass"
    return {
        "status": status,
        "summary": {
            "total": len(results),
            "pass": sum(1 for r in results if r.status == "pass"),
            "warn": len(warnings),
            "fail": len(failures),
        },
        "failures": failures,
        "warnings": warnings,
        "next_actions": next_actions_for_doctor(results),
    }


def _status_section(status_payload: dict[str, Any]) -> dict[str, Any]:
    services = status_payload.get("services") or []
    stopped_states = {"stopped", "not-running", "down"}
    tasks = status_payload.get("tasks") or []
    repos = status_payload.get("repos") or []
    return {
        "services": {
            "total": len(services),
            "running": sum(1 for s in services if s.get("state") == "running"),
            "stopped": sum(1 for s in services if s.get("state") in stopped_states),
            "blocked": list(status_payload.get("blocked_services") or []),
        },
        "tasks": {
            "total": len(tasks),
            "pending": sum(1 for t in tasks if t.get("state") == "pending"),
        },
        "repos": {
            "total": len(repos),
            "missing": [r.get("id") for r in repos if not r.get("present", True)],
        },
        "next_actions": next_actions_for_status(status_payload),
    }


def _pressure_section(status_payload: dict[str, Any]) -> dict[str, Any]:
    advisory = status_payload.get("pressure_advisory") or {}
    local_disk = advisory.get("local_disk") or {}
    return {
        "ok": bool(advisory.get("ok", True)),
        "mode": advisory.get("mode", "read_only"),
        "mutates": bool(advisory.get("mutates", False)),
        "level": local_disk.get("pressure_level"),
        "free_gib": local_disk.get("free_gib"),
        "warnings": [str(w) for w in (advisory.get("warnings") or []) if str(w).strip()],
    }


def _pulse_state_candidates(root_dir: Path, model: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    pulse_service = next(
        (svc for svc in model.get("services") or [] if svc.get("id") == "pulse"),
        None,
    )
    if pulse_service is not None:
        try:
            candidates.append(Path(service_paths(model, pulse_service)["log_dir"]) / "pulse.state.json")
        except Exception:
            pass
    candidates.append(root_dir / ".skillbox-state" / "logs" / "runtime" / "pulse.state.json")
    candidates.append(root_dir / "logs" / "runtime" / "pulse.state.json")
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _pulse_section(root_dir: Path, model: dict[str, Any]) -> dict[str, Any]:
    state_path = next(
        (path for path in _pulse_state_candidates(root_dir, model) if path.is_file()),
        None,
    )
    if state_path is None:
        return {"state": "not-running", "running": False, "state_file_present": False}
    try:
        snapshot = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"state": "unreadable", "running": False, "state_file_present": True, "error": str(exc)}
    updated_at = snapshot.get("updated_at")
    age = round(time.time() - updated_at, 1) if isinstance(updated_at, (int, float)) else None
    return {
        # The state file records the last cycle; liveness is inferred by callers
        # that also hold the pid. The packet reports the persisted snapshot only.
        "state": "stopped" if age is None or age > 0 else "running",
        "running": False,
        "state_file_present": True,
        "cycles": snapshot.get("cycle_count"),
        "heals": snapshot.get("heals"),
        "events": snapshot.get("events_emitted"),
        "last_tick_age_s": age,
        "pressure_warnings": [str(w) for w in (snapshot.get("pressure_warnings") or []) if str(w).strip()],
    }


def _skills_section(model: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    payload = collect_skill_visibility(
        model,
        cwd=cwd,
        include_global=True,
        include_project=True,
        include_sources=False,
    )
    summary = payload.get("summary") or {}
    issue_keys = ("broken_global", "broken_project", "global_not_allowed", "extra_global")
    issue_counts = {key: int(summary.get(key, 0) or 0) for key in issue_keys}
    return {
        "effective": int(summary.get("effective", 0) or 0),
        "issues": issue_counts,
        "issue_total": sum(issue_counts.values()),
        "shadowed": int(summary.get("shadowed", 0) or 0),
        "next_actions": list(payload.get("next_actions") or []),
    }


def _mcp_section(
    root_dir: Path,
    model: dict[str, Any],
    cwd: str | None,
    declared_servers: list[str] | None,
) -> dict[str, Any]:
    payload = collect_mcp_audit(
        root_dir, model, cwd=cwd, declared_servers=declared_servers,
    )
    summary = payload.get("summary") or {}
    parity = payload.get("parity") or {}
    return {
        "expected": summary.get("expected", 0),
        "unexplained_drift": summary.get("unexplained_drift", 0),
        "invalid_configs": summary.get("invalid_configs", 0),
        "claude_only": parity.get("claude_only") or [],
        "claude_only_declared": parity.get("claude_only_declared") or [],
        "next_actions": list(payload.get("next_actions") or []),
    }


def _git_section(root_dir: Path) -> dict[str, Any]:
    dirty = git_dirty_paths(root_dir)
    branch_result = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root_dir)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    return {
        "branch": branch,
        "dirty": bool(dirty),
        "dirty_count": len(dirty),
        "paths": dirty[:_MAX_DIRTY_PATHS],
        "paths_truncated": max(0, len(dirty) - _MAX_DIRTY_PATHS),
    }


def _beads_section(root_dir: Path) -> dict[str, Any]:
    beads_dir = root_dir / ".beads"
    db_present = (beads_dir / "beads.db").exists()
    jsonl_present = (beads_dir / "issues.jsonl").exists()
    return {
        "present": beads_dir.is_dir(),
        "db_present": db_present,
        "jsonl_present": jsonl_present,
        # Pointer only; the packet never shells out to br so it stays dependency-free.
        "pointer": "br ready --json",
    }


def _blocked_conditions(sections: dict[str, Any]) -> list[str]:
    conditions: list[str] = []
    doctor = sections["doctor"]
    if doctor["summary"]["fail"]:
        conditions.append(f"doctor: {doctor['summary']['fail']} check(s) failing: {', '.join(doctor['failures'])}")
    elif doctor["summary"]["warn"]:
        conditions.append(f"doctor: {doctor['summary']['warn']} warning(s): {', '.join(doctor['warnings'])}")
    mcp = sections["mcp"]
    if mcp["invalid_configs"]:
        conditions.append(f"mcp: {mcp['invalid_configs']} invalid config(s)")
    if mcp["unexplained_drift"]:
        conditions.append(f"mcp: {mcp['unexplained_drift']} unexplained drift server(s)")
    pressure = sections["pressure"]
    if pressure["warnings"]:
        conditions.append(f"pressure: {len(pressure['warnings'])} advisory warning(s)")
    pulse = sections["pulse"]
    if not pulse.get("state_file_present"):
        conditions.append("pulse: no state file (continuous observation has not run here)")
    skills = sections["skills"]
    if skills["issue_total"]:
        conditions.append(f"skills: {skills['issue_total']} broken/scope issue(s)")
    git = sections["git"]
    if git["dirty"]:
        conditions.append(f"git: {git['dirty_count']} uncommitted path(s)")
    return conditions


def _overall(sections: dict[str, Any]) -> str:
    doctor = sections["doctor"]
    mcp = sections["mcp"]
    skills = sections["skills"]
    if doctor["summary"]["fail"] or mcp["invalid_configs"] or skills["issue_total"]:
        return "red"
    if (
        doctor["summary"]["warn"]
        or mcp["unexplained_drift"]
        or sections["pressure"]["warnings"]
        or sections["git"]["dirty"]
        or not sections["pulse"].get("state_file_present")
    ):
        return "yellow"
    return "green"


def _aggregate_next_actions(sections: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for key in ("doctor", "status", "skills", "mcp"):
        for action in sections[key].get("next_actions") or []:
            if action not in seen:
                seen.add(action)
                ordered.append(action)
    pointer = sections["beads"]["pointer"]
    if pointer not in seen:
        ordered.append(pointer)
    return ordered


def collect_runtime_evidence(
    root_dir: Path,
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    declared_servers: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble the read-only runtime evidence packet for ``model``."""
    status_payload = runtime_status(model)
    sections = {
        "doctor": _doctor_section(model, root_dir),
        "status": _status_section(status_payload),
        "pressure": _pressure_section(status_payload),
        "pulse": _pulse_section(root_dir, model),
        "skills": _skills_section(model, cwd),
        "mcp": _mcp_section(root_dir, model, cwd, declared_servers),
        "git": _git_section(root_dir),
        "beads": _beads_section(root_dir),
    }
    return {
        "kind": "runtime-evidence",
        "scope": {
            "root_dir": str(root_dir),
            "cwd": cwd,
            "active_profiles": list(status_payload.get("active_profiles") or []),
            "active_clients": list(status_payload.get("active_clients") or []),
        },
        "sections": sections,
        "blocked_conditions": _blocked_conditions(sections),
        "next_actions": _aggregate_next_actions(sections),
        "overall": _overall(sections),
    }


def runtime_evidence_markdown(payload: dict[str, Any]) -> str:
    s = payload["sections"]
    lines = [
        "# Runtime evidence packet",
        "",
        f"- overall: **{payload['overall']}**",
        f"- scope: profiles={payload['scope']['active_profiles']} clients={payload['scope']['active_clients']}",
        "",
        "## Sections",
        "",
        f"- doctor: {s['doctor']['status']} "
        f"({s['doctor']['summary']['pass']}/{s['doctor']['summary']['warn']}/{s['doctor']['summary']['fail']} pass/warn/fail)",
        f"- status: services {s['status']['services']['running']}/{s['status']['services']['total']} running",
        f"- pressure: ok={s['pressure']['ok']} level={s['pressure']['level']} free_gib={s['pressure']['free_gib']}",
        f"- pulse: {s['pulse']['state']} (state_file_present={s['pulse']['state_file_present']})",
        f"- skills: effective={s['skills']['effective']} issues={s['skills']['issue_total']}",
        f"- mcp: expected={s['mcp']['expected']} unexplained_drift={s['mcp']['unexplained_drift']}",
        f"- git: branch={s['git']['branch']} dirty={s['git']['dirty']} ({s['git']['dirty_count']})",
        f"- beads: present={s['beads']['present']} -> `{s['beads']['pointer']}`",
        "",
        "## Blocked / gray conditions",
        "",
        *([f"- {c}" for c in payload["blocked_conditions"]] or ["- none"]),
        "",
        "## Next actions",
        "",
        *([f"- {a}" for a in payload["next_actions"]] or ["- none"]),
        "",
    ]
    return "\n".join(lines)


def print_runtime_evidence_text(payload: dict[str, Any]) -> None:
    s = payload["sections"]
    print(f"runtime evidence: overall={payload['overall']}")
    print(
        f"  doctor: {s['doctor']['status']} "
        f"({s['doctor']['summary']['pass']}p/{s['doctor']['summary']['warn']}w/{s['doctor']['summary']['fail']}f)"
    )
    print(f"  status: services {s['status']['services']['running']}/{s['status']['services']['total']} running")
    print(f"  pressure: ok={s['pressure']['ok']} level={s['pressure']['level']} free_gib={s['pressure']['free_gib']}")
    print(f"  pulse: {s['pulse']['state']} (state_file={s['pulse']['state_file_present']})")
    print(f"  skills: effective={s['skills']['effective']} issues={s['skills']['issue_total']}")
    print(f"  mcp: expected={s['mcp']['expected']} unexplained_drift={s['mcp']['unexplained_drift']}")
    print(f"  git: branch={s['git']['branch']} dirty={s['git']['dirty']} ({s['git']['dirty_count']})")
    print(f"  beads: present={s['beads']['present']} -> {s['beads']['pointer']}")
    if payload["blocked_conditions"]:
        print("  blocked/gray:")
        for condition in payload["blocked_conditions"]:
            print(f"    - {condition}")
    if payload["next_actions"]:
        print("  next_actions:")
        for action in payload["next_actions"]:
            print(f"    - {action}")
