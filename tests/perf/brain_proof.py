#!/usr/bin/env python3
"""Standalone latency proof for the agent operations brain.

The proof exercises the in-process payload functions for the read-first brain
surfaces on a representative synthetic graph. It avoids subprocess startup for
the main timing loop so regressions point at graph/search/decision compute,
then runs one generous CLI import smoke to catch slow command startup.

Usage:
    python3 tests/perf/brain_proof.py [--cycles N] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import cli as CLI  # noqa: E402
from runtime_manager.agent_decisions import explain_payload, next_action_payload  # noqa: E402
from runtime_manager.agent_graph_engine import graph_command_payload  # noqa: E402
from runtime_manager.agent_search import search_payload  # noqa: E402


BUDGETS_MS = {
    "capabilities": 50.0,
    "graph_critical_path": 150.0,
    "next_no_adapters": 150.0,
    "explain_service": 100.0,
    "search_graph": 100.0,
    "capabilities_cli_import": 2000.0,
}


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


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[index]


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

    budget = BUDGETS_MS[name]
    p50 = round(_percentile(durations, 50), 3)
    p95 = round(_percentile(durations, 95), 3)
    return {
        "surface": name,
        "cycles": cycles,
        "p50_ms": p50,
        "p95_ms": p95,
        "max_ms": round(max(durations), 3),
        "payload_elapsed_p95_ms": round(_percentile(payload_elapsed, 95), 3),
        "budget_ms": budget,
        "ok": p95 <= budget,
    }


def _capabilities_cli_import_smoke() -> dict[str, Any]:
    start = time.perf_counter()
    result = subprocess.run(
        [sys.executable, ".env-manager/manage.py", "capabilities", "--no-adapters", "--format", "json"],
        cwd=ROOT_DIR,
        env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
        capture_output=True,
        text=True,
        check=False,
        timeout=2.0,
    )
    elapsed = (time.perf_counter() - start) * 1000.0
    payload: dict[str, Any] = {}
    if result.stdout.strip():
        payload = json.loads(result.stdout)
        _elapsed_meta(payload)
    budget = BUDGETS_MS["capabilities_cli_import"]
    ok = result.returncode == 0 and elapsed <= budget and bool(payload.get("ok", False))
    return {
        "surface": "capabilities_cli_import",
        "cycles": 1,
        "p50_ms": round(elapsed, 3),
        "p95_ms": round(elapsed, 3),
        "max_ms": round(elapsed, 3),
        "payload_elapsed_p95_ms": float((payload.get("meta") or {}).get("elapsed_ms") or 0.0),
        "budget_ms": budget,
        "ok": ok,
        "returncode": result.returncode,
        "stderr": result.stderr.strip(),
    }


def build_proof(cycles: int) -> dict[str, Any]:
    graph = fixture_graph()
    surfaces: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("capabilities", lambda: CLI._capabilities_payload(ROOT_DIR, compact=True)),  # noqa: SLF001
        ("graph_critical_path", lambda: graph_command_payload(graph, algorithm="critical-path")),
        ("next_no_adapters", lambda: next_action_payload(graph, adapters={}, evidence={})),
        ("explain_service", lambda: explain_payload(graph, "service:svc-25", adapters={})),
        ("search_graph", lambda: search_payload("svc-25", graph=graph, limit=10)),
    ]
    rows = [_measure(name, cycles, fn) for name, fn in surfaces]
    rows.append(_capabilities_cli_import_smoke())
    return {
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
        "rows": rows,
        "ok": all(row["ok"] for row in rows),
    }


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
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the agent ops brain latency proof.")
    parser.add_argument("--cycles", type=int, default=20, help="Timing cycles per in-process surface.")
    parser.add_argument("--out", default=None, help="Output directory for proof artifacts.")
    parser.add_argument("--run-id", default=None, help="Override the default UTC run-id directory.")
    args = parser.parse_args()

    cycles = max(1, int(args.cycles))
    proof = build_proof(cycles)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = Path(args.out) if args.out else ROOT_DIR / "tests" / "artifacts" / "perf" / run_id / "brain"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "proof.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    (out_dir / "proof.md").write_text(_render_markdown(proof), encoding="utf-8")

    print(_render_table(proof["rows"]))
    print(f"brain proof written: {out_dir}")
    return 0 if proof["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
