#!/usr/bin/env python3
"""Standalone latency proof for the agent operations brain.

The proof has two layers.

**Fixture proof (deterministic, gating).** It exercises the in-process payload
functions for the read-first brain surfaces on a representative synthetic
graph, plus the component phases that surround them: runtime-model build and
bounded adapter collection against a stubbed subprocess runner. It avoids real
external tools so regressions point at graph/search/decision/model compute, then
runs one generous CLI import smoke to catch slow command startup.

**Live observation (non-gating).** ``--live`` runs the real ``next`` command
with its real adapters and records the truthful breakdown -- end-to-end wall,
startup, model build, adapter collection, compute -- along with which adapters
were unavailable or timed out. It never contributes to the exit code: external
tools under machine load are an observation, not a regression.

The problem this exists to prevent: a real ``next`` run measured ~10s of wall
clock while its payload reported ``elapsed_ms`` of 1.9, because ``elapsed_ms``
only ever covered the decision function.

Usage:
    python3 tests/perf/brain_proof.py [--cycles N] [--out DIR] [--live]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import agent_adapters as ADAPTERS  # noqa: E402
from runtime_manager import cli as CLI  # noqa: E402
from runtime_manager.agent_decisions import explain_payload, next_action_payload  # noqa: E402
from runtime_manager.agent_graph_engine import graph_command_payload  # noqa: E402
from runtime_manager.agent_search import search_payload  # noqa: E402
from runtime_manager.agent_timing import (  # noqa: E402
    InvocationTiming,
    attach_component_timing,
)
from runtime_manager.validation import build_runtime_model  # noqa: E402


# Budgets are deliberately generous: this proof gates on "did a surface become
# an order of magnitude slower", never on absolute machine speed.
BUDGETS_MS = {
    "capabilities": 50.0,
    "graph_critical_path": 150.0,
    "next_no_adapters": 150.0,
    "explain_service": 100.0,
    "search_graph": 100.0,
    "adapter_collection_stub": 1500.0,
    "model_build": 5000.0,
    # Subprocess wall on a shared box. Measured 1.1-1.9s at load average 34, so
    # this is an order-of-magnitude regression detector, not a precision gate:
    # the old 2000ms budget aborted the whole harness with TimeoutExpired under
    # ordinary concurrent load. In-process compute budgets above are unchanged.
    "capabilities_cli_import": 6000.0,
}

# Component meta keys that a brain payload must carry once the CLI has attached
# the invocation breakdown. Asserted structurally, never by value.
REQUIRED_COMPONENT_META = ("compute_ms", "elapsed_ms", "end_to_end_ms")

LIVE_DEFAULT_COMMAND = ("next", "--format", "json", "--limit", "1")
LIVE_DEFAULT_TIMEOUT_SECONDS = 180.0


def _node(node_id: str, kind: str, label: str, **attrs: object) -> dict[str, object]:
    return {"id": node_id, "kind": kind, "label": label, "attrs": attrs}


def _edge(source: str, target: str, kind: str = "depends_on", **attrs: object) -> dict[str, object]:
    return {"source": source, "target": target, "kind": kind, "attrs": attrs}


def fixture_graph() -> dict[str, object]:
    """Build a stable ~500-node graph with realistic brain node kinds."""
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []

    for i in range(50):
        nodes.append(_node(f"repo:repo-{i}", "repo", f"repo-{i}"))
        nodes.append(_node(f"service:svc-{i}", "service", f"service {i}", port=8000 + i))
        nodes.append(_node(f"check:check-{i}", "check", f"check {i}", command=f"check-{i} --json"))
        nodes.append(_node(f"skill:skill-{i}", "skill", f"skill {i}", category="proof"))
        nodes.append(_node(f"mcp_tool:tool-{i}", "mcp_tool", f"tool {i}"))
        nodes.append(_node(f"command:cmd-{i}", "command", f"command {i}"))
        nodes.append(_node(f"bead:proof-{i}", "bead", f"proof bead {i}", status="open", priority=i % 4))
        nodes.append(_node(f"task:build-{i}", "task", f"build task {i}"))
        nodes.append(_node(f"task:test-{i}", "task", f"test task {i}"))
        nodes.append(_node(f"task:release-{i}", "task", f"release task {i}"))

        edges.extend(
            [
                _edge(f"service:svc-{i}", f"repo:repo-{i}", "declared_in"),
                _edge(f"check:check-{i}", f"service:svc-{i}", "checks"),
                _edge(f"skill:skill-{i}", f"service:svc-{i}", "supports"),
                _edge(f"command:cmd-{i}", f"mcp_tool:tool-{i}", "exposes"),
                _edge(f"bead:proof-{i}", f"task:build-{i}", "tracks"),
                _edge(f"task:build-{i}", f"repo:repo-{i}", "depends_on"),
                _edge(f"task:test-{i}", f"task:build-{i}", "depends_on"),
                _edge(f"task:release-{i}", f"task:test-{i}", "depends_on"),
            ]
        )
        if i:
            edges.append(_edge(f"service:svc-{i}", f"service:svc-{i - 1}", "depends_on"))
            edges.append(_edge(f"task:build-{i}", f"task:release-{i - 1}", "depends_on"))

    return {"ok": True, "nodes": nodes, "edges": edges, "warnings": []}


def _elapsed_meta(payload: dict[str, Any]) -> float:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise AssertionError("payload missing meta object")
    elapsed = meta.get("elapsed_ms")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        raise AssertionError("payload missing numeric meta.elapsed_ms")
    return float(elapsed)


def _assert_component_meta(payload: dict[str, Any], *, surface: str) -> dict[str, Any]:
    """Assert the component breakdown is present and self-consistent.

    Structural only: values are machine dependent, key presence and the
    "end-to-end is never smaller than compute" invariant are not.
    """
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise AssertionError(f"{surface}: payload missing meta object")
    missing = [key for key in REQUIRED_COMPONENT_META if key not in meta]
    if missing:
        raise AssertionError(f"{surface}: meta missing component timing keys {missing}")
    if float(meta["end_to_end_ms"]) + 1e-6 < float(meta["compute_ms"]):
        raise AssertionError(
            f"{surface}: end_to_end_ms {meta['end_to_end_ms']} < compute_ms {meta['compute_ms']}"
        )
    timing = meta.get("timing")
    if not isinstance(timing, dict) or "phases" not in timing:
        raise AssertionError(f"{surface}: meta.timing.phases missing")
    return meta


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[index]


def _row(
    name: str,
    durations: list[float],
    *,
    cycles: int,
    payload_elapsed: list[float] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    budget = BUDGETS_MS[name]
    p95 = round(_percentile(durations, 95), 3)
    row: dict[str, Any] = {
        "surface": name,
        "cycles": cycles,
        "p50_ms": round(_percentile(durations, 50), 3),
        "p95_ms": p95,
        "max_ms": round(max(durations), 3),
        "payload_elapsed_p95_ms": (
            round(_percentile(payload_elapsed, 95), 3) if payload_elapsed else 0.0
        ),
        "budget_ms": budget,
        "ok": p95 <= budget,
    }
    row.update(extra)
    return row


def _measure(name: str, cycles: int, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    durations: list[float] = []
    payload_elapsed: list[float] = []
    for _ in range(cycles):
        start = time.perf_counter()
        payload = fn()
        durations.append((time.perf_counter() - start) * 1000.0)
        payload_elapsed.append(_elapsed_meta(payload))
        if not payload.get("ok", False):
            raise AssertionError(f"{name} returned non-ok payload: {payload.get('error')}")

    return _row(name, durations, cycles=cycles, payload_elapsed=payload_elapsed)


def _measure_component(name: str, cycles: int, fn: Callable[[], Any]) -> dict[str, Any]:
    """Time a component phase that produces no payload meta of its own."""
    durations: list[float] = []
    last: Any = None
    for _ in range(cycles):
        start = time.perf_counter()
        last = fn()
        durations.append((time.perf_counter() - start) * 1000.0)
    row = _row(name, durations, cycles=cycles)
    row["detail"] = _component_detail(name, last)
    return row


def _component_detail(name: str, value: Any) -> dict[str, Any]:
    if name == "model_build" and isinstance(value, dict):
        return {
            "services": len(value.get("services") or []),
            "repos": len(value.get("repos") or []),
            "skills": len(value.get("skills") or []),
        }
    if name == "adapter_collection_stub" and isinstance(value, dict):
        timing = value.get("timing") or {}
        return {
            "adapter_count": timing.get("adapter_count"),
            "statuses": timing.get("statuses"),
            "timeouts": timing.get("timeouts"),
            "unavailable": timing.get("unavailable"),
        }
    return {}


def _stubbed_adapter_collection(tmp_root: Path) -> dict[str, Any]:
    """Collect adapter evidence with every external binary stubbed out.

    This keeps adapter *plumbing* cost (spec build, timeout resolution, parse,
    roll-up) on the gated fixture proof while leaving real tool latency to the
    non-gating live mode.
    """
    completed = subprocess.CompletedProcess(["stub"], 0, stdout="{}", stderr="")

    def fake_run(_command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return completed

    original = ADAPTERS.subprocess.run
    ADAPTERS.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        return ADAPTERS.collect_agent_adapter_evidence(tmp_root)
    finally:
        ADAPTERS.subprocess.run = original  # type: ignore[assignment]


def _capabilities_cli_import_smoke() -> dict[str, Any]:
    budget = BUDGETS_MS["capabilities_cli_import"]
    start = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, ".env-manager/manage.py", "capabilities", "--no-adapters", "--format", "json"],
            cwd=ROOT_DIR,
            env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
            capture_output=True,
            text=True,
            check=False,
            # Strictly above the budget so an over-budget run is reported as a
            # FAIL row instead of aborting the harness with TimeoutExpired.
            timeout=(budget / 1000.0) * 2.0,
        )
    except subprocess.TimeoutExpired:
        elapsed = (time.perf_counter() - start) * 1000.0
        return {
            "surface": "capabilities_cli_import",
            "cycles": 1,
            "p50_ms": round(elapsed, 3),
            "p95_ms": round(elapsed, 3),
            "max_ms": round(elapsed, 3),
            "payload_elapsed_p95_ms": 0.0,
            "budget_ms": budget,
            "ok": False,
            "returncode": None,
            "stderr": "capabilities smoke exceeded the harness timeout",
            "component_meta_keys": [],
        }
    elapsed = (time.perf_counter() - start) * 1000.0
    payload: dict[str, Any] = {}
    if result.stdout.strip():
        payload = json.loads(result.stdout)
        _elapsed_meta(payload)
        _assert_component_meta(payload, surface="capabilities_cli_import")
    ok = result.returncode == 0 and elapsed <= budget and bool(payload.get("ok", False))
    meta = payload.get("meta") or {}
    return {
        "surface": "capabilities_cli_import",
        "cycles": 1,
        "p50_ms": round(elapsed, 3),
        "p95_ms": round(elapsed, 3),
        "max_ms": round(elapsed, 3),
        "payload_elapsed_p95_ms": float(meta.get("elapsed_ms") or 0.0),
        "budget_ms": budget,
        "ok": ok,
        "returncode": result.returncode,
        "stderr": result.stderr.strip(),
        "component_meta_keys": sorted(
            key for key in meta if key in {*REQUIRED_COMPONENT_META, "model_ms", "adapter_collection_ms", "startup_ms"}
        ),
    }


def component_contract_probe() -> dict[str, Any]:
    """Prove the component breakdown assembles from recorded phases only.

    Uses a private recorder with synthetic phase values so the *shape* of the
    contract is asserted deterministically, independent of machine speed.
    """
    recorder = InvocationTiming()
    recorder.record("startup_ms", 900.0)
    recorder.record("model_ms", 250.0)
    recorder.record("adapter_collection_ms", 7500.0)
    recorder.set_detail("adapters", {"timeouts": ["bv_triage"], "unavailable": []})
    payload = attach_component_timing(
        {"ok": True, "meta": {"elapsed_ms": 1.7}},
        invocation=recorder,
        end_to_end_ms=8700.0,
    )
    meta = payload["meta"]
    return {
        "keys": sorted(meta),
        "compute_ms": meta["compute_ms"],
        "end_to_end_ms": meta["end_to_end_ms"],
        "model_ms": meta["model_ms"],
        "adapter_collection_ms": meta["adapter_collection_ms"],
        "startup_ms": meta["startup_ms"],
        "compute_share_pct": round(100.0 * meta["compute_ms"] / meta["end_to_end_ms"], 4),
        "ok": (
            meta["compute_ms"] == 1.7
            and meta["end_to_end_ms"] == 8700.0
            and meta["adapter_collection_ms"] == 7500.0
        ),
    }


def _live_meta_breakdown(meta: dict[str, Any]) -> dict[str, Any]:
    timing = meta.get("timing") if isinstance(meta.get("timing"), dict) else {}
    adapters = timing.get("adapters") if isinstance(timing.get("adapters"), dict) else {}
    return {
        "end_to_end_ms": meta.get("end_to_end_ms"),
        "startup_ms": meta.get("startup_ms"),
        "model_ms": meta.get("model_ms"),
        "adapter_collection_ms": meta.get("adapter_collection_ms"),
        "compute_ms": meta.get("compute_ms"),
        "legacy_elapsed_ms": meta.get("elapsed_ms"),
        "adapter_durations_ms": adapters.get("durations_ms") or {},
        "adapter_statuses": adapters.get("statuses") or {},
        "adapter_timeouts": adapters.get("timeouts") or [],
        "adapter_unavailable": adapters.get("unavailable") or [],
        "slowest_adapter": adapters.get("slowest"),
    }


def live_observation(
    *,
    command: tuple[str, ...] = LIVE_DEFAULT_COMMAND,
    timeout_seconds: float = LIVE_DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Observe one real CLI invocation with real adapters. Never gates.

    Real ``bv``/``sbp``/``br`` latency depends on machine load and on which
    optional tools exist, so this records what happened -- including timeouts
    and unavailable binaries -- and always reports ``gating: false``.
    """
    argv = [sys.executable, ".env-manager/manage.py", *command]
    start = time.perf_counter()
    observation: dict[str, Any] = {
        "gating": False,
        "command": argv[1:],
        "timeout_seconds": timeout_seconds,
    }
    try:
        result = subprocess.run(
            argv,
            cwd=ROOT_DIR,
            env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        observation["observation_status"] = "harness_timeout"
        observation["wall_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
        observation["note"] = "live command exceeded the observation timeout; recorded, not failed"
        return observation
    except OSError as exc:
        observation["observation_status"] = "unavailable"
        observation["wall_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
        observation["note"] = f"live command could not start: {exc}"
        return observation

    observation["wall_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
    observation["returncode"] = result.returncode
    observation["stderr_excerpt"] = result.stderr.strip()[:500]
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        observation["observation_status"] = "unparseable"
        observation["note"] = f"live stdout was not JSON: {exc}"
        return observation

    meta = payload.get("meta")
    if not isinstance(meta, dict):
        observation["observation_status"] = "no_meta"
        observation["note"] = "live payload carried no meta object"
        return observation

    observation["observation_status"] = "observed" if result.returncode == 0 else "nonzero_exit"
    observation["breakdown"] = _live_meta_breakdown(meta)
    end_to_end = observation["breakdown"].get("end_to_end_ms")
    compute = observation["breakdown"].get("compute_ms")
    if isinstance(end_to_end, (int, float)) and isinstance(compute, (int, float)) and end_to_end:
        observation["compute_share_pct"] = round(100.0 * float(compute) / float(end_to_end), 4)
        observation["unattributed_wall_ms"] = round(
            observation["wall_ms"] - float(end_to_end), 3
        )
    return observation


def build_proof(
    cycles: int,
    *,
    live: bool = False,
    live_timeout_seconds: float = LIVE_DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    graph = fixture_graph()
    surfaces: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("capabilities", lambda: CLI._capabilities_payload(ROOT_DIR, compact=True)),  # noqa: SLF001
        ("graph_critical_path", lambda: graph_command_payload(graph, algorithm="critical-path")),
        ("next_no_adapters", lambda: next_action_payload(graph, adapters={}, evidence={})),
        ("explain_service", lambda: explain_payload(graph, "service:svc-25", adapters={})),
        ("search_graph", lambda: search_payload("svc-25", graph=graph, limit=10)),
    ]
    rows = [_measure(name, cycles, fn) for name, fn in surfaces]

    # Component phases. The runtime model is built once per cycle here and
    # nowhere else in this proof: no timing field costs a second build.
    with tempfile.TemporaryDirectory(prefix="brain-proof-adapters-") as tmpdir:
        tmp_root = Path(tmpdir)
        rows.append(
            _measure_component(
                "adapter_collection_stub",
                cycles,
                lambda: _stubbed_adapter_collection(tmp_root),
            )
        )
    rows.append(_measure_component("model_build", max(1, min(cycles, 3)), lambda: build_runtime_model(ROOT_DIR)))
    rows.append(_capabilities_cli_import_smoke())

    _assert_component_meta(
        CLI._capabilities_payload(ROOT_DIR, compact=True),  # noqa: SLF001
        surface="capabilities",
    )
    contract = component_contract_probe()

    proof: dict[str, Any] = {
        "kind": "agent-ops-brain-latency-proof",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "fixture": {
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "service_count": 50,
            "task_count": 150,
            "skill_count": 50,
        },
        "budgets_ms": BUDGETS_MS,
        "component_contract": contract,
        "rows": rows,
    }
    if live:
        proof["live"] = live_observation(timeout_seconds=live_timeout_seconds)
    proof["ok"] = all(row["ok"] for row in rows) and bool(contract["ok"])
    return proof


def _render_table(rows: list[dict[str, Any]]) -> str:
    header = f"{'surface':<24} {'p50_ms':>8} {'p95_ms':>8} {'budget':>8} {'status':>6}"
    lines = [header, "-" * len(header)]
    for row in rows:
        status = "PASS" if row["ok"] else "FAIL"
        lines.append(
            f"{row['surface']:<24} {row['p50_ms']:>8.3f} {row['p95_ms']:>8.3f} "
            f"{row['budget_ms']:>8.1f} {status:>6}"
        )
    return "\n".join(lines)


def _render_live(live: dict[str, Any]) -> str:
    lines = [f"live observation ({live.get('observation_status')}, non-gating):"]
    if live.get("note"):
        lines.append(f"  note: {live['note']}")
    lines.append(f"  wall_ms: {live.get('wall_ms')}")
    breakdown = live.get("breakdown") or {}
    for key in ("end_to_end_ms", "startup_ms", "model_ms", "adapter_collection_ms", "compute_ms"):
        if breakdown.get(key) is not None:
            lines.append(f"  {key}: {breakdown[key]}")
    if breakdown.get("adapter_durations_ms"):
        lines.append(f"  adapters_ms: {json.dumps(breakdown['adapter_durations_ms'], sort_keys=True)}")
    if breakdown.get("adapter_timeouts"):
        lines.append(f"  adapter_timeouts: {', '.join(breakdown['adapter_timeouts'])}")
    if breakdown.get("adapter_unavailable"):
        lines.append(f"  adapter_unavailable: {', '.join(breakdown['adapter_unavailable'])}")
    if live.get("compute_share_pct") is not None:
        lines.append(f"  compute_share_pct: {live['compute_share_pct']}")
    return "\n".join(lines)


def _render_markdown(proof: dict[str, Any]) -> str:
    lines = [
        "# Agent ops brain latency proof",
        "",
        f"- generated_at_utc: `{proof['generated_at_utc']}`",
        f"- python: `{proof['python']}`",
        f"- nodes: `{proof['fixture']['nodes']}`",
        f"- edges: `{proof['fixture']['edges']}`",
        f"- status: `{'PASS' if proof['ok'] else 'FAIL'}`",
        "",
        "```",
        _render_table(proof["rows"]),
        "```",
        "",
    ]
    if proof.get("live"):
        lines.extend(["## Live observation (non-gating)", "", "```", _render_live(proof["live"]), "```", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the agent ops brain latency proof.")
    parser.add_argument("--cycles", type=int, default=20, help="Timing cycles per in-process surface.")
    parser.add_argument("--out", default=None, help="Output directory for proof artifacts.")
    parser.add_argument("--run-id", default=None, help="Override the default UTC run-id directory.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also observe one real `next` invocation with real adapters (recorded, never gating).",
    )
    parser.add_argument(
        "--live-timeout",
        type=float,
        default=LIVE_DEFAULT_TIMEOUT_SECONDS,
        help="Harness timeout for the live observation, in seconds.",
    )
    args = parser.parse_args()

    cycles = max(1, int(args.cycles))
    proof = build_proof(cycles, live=bool(args.live), live_timeout_seconds=float(args.live_timeout))
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = Path(args.out) if args.out else ROOT_DIR / "tests" / "artifacts" / "perf" / run_id / "brain"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "proof.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    (out_dir / "proof.md").write_text(_render_markdown(proof), encoding="utf-8")

    print(_render_table(proof["rows"]))
    if proof.get("live"):
        print(_render_live(proof["live"]))
    print(f"brain proof written: {out_dir}")
    return 0 if proof["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
