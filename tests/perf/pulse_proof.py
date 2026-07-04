#!/usr/bin/env python3
"""No-infra pulse regression proof + timing packet generator.

Drives the real `pulse.py` reconciliation functions with the runtime model,
service/check probes, and pressure advisory mocked out — so the proof exercises
pulse behavior without starting any infrastructure or mutating live services
(beyond the in-test fixtures). It captures:

  * service crash / auto-heal and recovery transitions,
  * check fail / recover transitions,
  * pressure-advisory raise / clear events (read-only),
  * the persisted state-file shape,
  * a `pulse.py status` rendering against a synthesized state file,
  * a cycle/timing baseline over repeated reconcile cycles.

It writes a JSON + Markdown artifact under
`tests/artifacts/perf/<run-id>/pulse/` with a stable behavioral fingerprint
(volatile timing/pids excluded) plus the per-run timing metrics.

Usage:
    python3 tests/perf/pulse_proof.py [--out DIR] [--cycles N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

PULSE = SourceFileLoader(
    "skillbox_pulse_proof",
    str((ENV_MANAGER_DIR / "pulse.py").resolve()),
).load_module()


# A restartable HTTP service is the canonical supervised target.
_WEB_SERVICE = {
    "id": "web",
    "kind": "http",
    "healthcheck": {"type": "http", "url": "http://127.0.0.1:3001"},
    "lifecycle": {"restart": "make web-restart"},
}


def _model(services: list[dict], checks: list[dict]) -> dict:
    return {
        "clients": [{"id": "proof"}],
        "selection": {},
        "services": services,
        "checks": checks,
    }


def _seq(values: list):
    """side_effect that yields each value once then repeats the last."""
    box = {"i": 0}

    def _next(*_a, **_k):
        i = min(box["i"], len(values) - 1)
        box["i"] += 1
        return values[i]

    return _next


def _run_reconcile_sequence(
    root: Path,
    *,
    model: dict,
    service_snapshots: list[dict],
    check_snapshots: list[dict],
    advisories: list[dict],
    auto_restart: bool = True,
) -> PULSE.PulseState:
    """Run one reconcile cycle per supplied snapshot, with all I/O seams mocked."""
    state = PULSE.PulseState()
    cycles = max(len(service_snapshots), len(check_snapshots), len(advisories))
    with (
        mock.patch.object(PULSE, "build_runtime_model", return_value=model),
        mock.patch.object(PULSE, "filter_model", side_effect=lambda m, _p, _c: m),
        mock.patch.object(PULSE, "_model_config_hash", return_value="proof-hash"),
        mock.patch.object(PULSE, "_snapshot_services", side_effect=_seq(service_snapshots)),
        mock.patch.object(PULSE, "_snapshot_checks", side_effect=_seq(check_snapshots)),
        mock.patch.object(PULSE, "_scan_rogue_listeners", return_value=[]),
        mock.patch.object(PULSE, "runtime_pressure_advisory", side_effect=_seq(advisories)),
        mock.patch.object(PULSE, "service_supports_lifecycle", return_value=(True, "")),
        mock.patch.object(PULSE, "_restart_service", return_value=True),
        mock.patch.object(PULSE, "log_runtime_event"),
    ):
        for _ in range(cycles):
            PULSE.reconcile_once(root, state, auto_restart=auto_restart)
    return state


def prove_service_transitions(root: Path) -> dict:
    """running -> crash(down) -> auto-heal, captured via real reconcile logic."""
    model = _model([_WEB_SERVICE], [])
    snapshots = [
        {"web": {"state": "running", "pid": 100, "url": "http://127.0.0.1:3001"}},
        {"web": {"state": "down"}},
        {"web": {"state": "running", "pid": 101, "url": "http://127.0.0.1:3001"}},
    ]
    state = _run_reconcile_sequence(
        root,
        model=model,
        service_snapshots=snapshots,
        check_snapshots=[{}],
        advisories=[{"warnings": []}],
    )
    return {
        "scenario": "service crash then auto-heal",
        "cycles": state.cycle_count,
        "final_service_states": dict(state.service_states),
        "events_emitted": state.events_emitted,
        "heals": state.heals,
    }


def prove_check_transitions(root: Path) -> dict:
    """check ok -> fail -> recover, captured via real reconcile logic."""
    model = _model([], [{"id": "disk", "kind": "noop"}])
    snapshots = [{"disk": True}, {"disk": False}, {"disk": True}]
    state = _run_reconcile_sequence(
        root,
        model=model,
        service_snapshots=[{}],
        check_snapshots=snapshots,
        advisories=[{"warnings": []}],
    )
    return {
        "scenario": "check fail then recover",
        "cycles": state.cycle_count,
        "final_check_states": dict(state.check_states),
        "events_emitted": state.events_emitted,
    }


def prove_pressure_advisory(root: Path) -> dict:
    """pressure advisory raised (read-only) then cleared."""
    model = _model([], [])
    advisories = [
        {"warnings": []},
        {"warnings": ["disk free below 10 GiB"], "safe_first_commands": ["pressure-report --format json"]},
        {"warnings": []},
    ]
    state = _run_reconcile_sequence(
        root,
        model=model,
        service_snapshots=[{}],
        check_snapshots=[{}],
        advisories=advisories,
    )
    return {
        "scenario": "pressure advisory raise then clear (read-only)",
        "cycles": state.cycle_count,
        "final_pressure_warnings": list(state.pressure_warnings),
        "events_emitted": state.events_emitted,
        "advisory_mutates": False,
    }


def prove_state_file_shape(root: Path) -> dict:
    """Run one cycle and capture the persisted state-file top-level shape."""
    model = _model([_WEB_SERVICE], [{"id": "disk", "kind": "noop"}])
    _run_reconcile_sequence(
        root,
        model=model,
        service_snapshots=[{"web": {"state": "running", "pid": 100}}],
        check_snapshots=[{"disk": True}],
        advisories=[{"warnings": []}],
    )
    state_path = PULSE.pulse_state_path(root)
    snapshot = json.loads(state_path.read_text(encoding="utf-8"))
    return {
        "state_path_rel": str(state_path.relative_to(root)),
        "top_level_keys": sorted(snapshot.keys()),
        # Stable subset of values (volatile pid/updated_at excluded).
        "sample": {
            "service_states": snapshot.get("service_states"),
            "check_states": snapshot.get("check_states"),
            "interval": snapshot.get("interval"),
            "auto_restart": snapshot.get("auto_restart"),
        },
    }


def prove_status_rendering() -> dict:
    """Render `pulse.py status` against a synthesized, deterministic state file."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state_dir = root / "logs" / "runtime"
        state_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "pid": 4242,
            "updated_at": time.time(),
            "interval": 30,
            "auto_restart": True,
            "auto_sync": False,
            "active_clients": [],
            "active_profiles": ["core"],
            "unhealthy_grace_seconds": 60.0,
            "cycle_count": 3,
            "heals": 1,
            "events_emitted": 4,
            "config_hash": "proof-hash",
            "service_states": {"web": "running", "api": "down"},
            "check_states": {"disk": True, "tls": False},
            "pressure_warnings": ["disk free below 10 GiB"],
            "pressure_advisory": {},
            "restart_attempts": {},
            "unhealthy_for_seconds": {},
        }
        (state_dir / "pulse.state.json").write_text(
            json.dumps(snapshot, indent=2) + "\n", encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, str(ENV_MANAGER_DIR / "pulse.py"), "--root-dir", str(root), "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        # The "last tick" age line is wall-clock volatile; normalize for the fingerprint.
        normalized = [
            "  last tick: <age>" if line.strip().startswith("last tick:") else line
            for line in result.stdout.splitlines()
        ]
        return {
            "command": "pulse.py --root-dir <tmp> status",
            "exit_code": result.returncode,
            "stdout_normalized": normalized,
        }


def measure_cycle_timing(root: Path, cycles: int) -> dict:
    """Time repeated reconcile cycles with stable mocked probes (no infra)."""
    model = _model([_WEB_SERVICE], [{"id": "disk", "kind": "noop"}])
    state = PULSE.PulseState()
    durations_ms: list[float] = []
    with (
        mock.patch.object(PULSE, "build_runtime_model", return_value=model),
        mock.patch.object(PULSE, "filter_model", side_effect=lambda m, _p, _c: m),
        mock.patch.object(PULSE, "_model_config_hash", return_value="proof-hash"),
        mock.patch.object(
            PULSE, "_snapshot_services",
            return_value={"web": {"state": "running", "pid": 100, "url": "http://127.0.0.1:3001"}},
        ),
        mock.patch.object(PULSE, "_snapshot_checks", return_value={"disk": True}),
        mock.patch.object(PULSE, "_scan_rogue_listeners", return_value=[]),
        mock.patch.object(PULSE, "runtime_pressure_advisory", return_value={"warnings": []}),
        mock.patch.object(PULSE, "service_supports_lifecycle", return_value=(True, "")),
        mock.patch.object(PULSE, "log_runtime_event"),
    ):
        for _ in range(cycles):
            start = time.perf_counter()
            PULSE.reconcile_once(root, state)
            durations_ms.append((time.perf_counter() - start) * 1000.0)

    ordered = sorted(durations_ms)
    return {
        "cycles": cycles,
        "total_ms": round(sum(durations_ms), 3),
        "avg_ms": round(sum(durations_ms) / len(durations_ms), 4),
        "p50_ms": round(ordered[len(ordered) // 2], 4),
        "p95_ms": round(ordered[int(len(ordered) * 0.95)], 4),
        "max_ms": round(ordered[-1], 4),
    }


def _fingerprint(behavioral: dict) -> str:
    payload = json.dumps(behavioral, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_proof(cycles: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        behavioral = {
            "service_transitions": prove_service_transitions(root),
            "check_transitions": prove_check_transitions(root),
            "pressure_advisory": prove_pressure_advisory(root),
            "state_file_shape": prove_state_file_shape(root),
            "status_rendering": prove_status_rendering(),
        }
        timing = measure_cycle_timing(root, cycles)

    blocked_conditions: list[str] = []
    # The proof is fully no-infra; nothing is gated. Record explicitly so the
    # artifact never silently implies coverage it does not have.
    if not blocked_conditions:
        blocked_conditions = ["none — proof runs without infrastructure or live services"]

    return {
        "kind": "pulse-regression-proof",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "behavioral": behavioral,
        "behavioral_fingerprint_sha256": _fingerprint(behavioral),
        "timing": timing,
        "blocked_conditions": blocked_conditions,
    }


def _render_markdown(proof: dict) -> str:
    b = proof["behavioral"]
    t = proof["timing"]
    lines = [
        "# Pulse regression proof + timing packet",
        "",
        f"- generated_at_utc: `{proof['generated_at_utc']}`",
        f"- python: `{proof['python']}`",
        f"- behavioral_fingerprint_sha256: `{proof['behavioral_fingerprint_sha256']}`",
        "",
        "## Behavioral proof (no-infra)",
        "",
        f"- **Service transitions** — {b['service_transitions']['scenario']}: "
        f"final={b['service_transitions']['final_service_states']}, "
        f"events={b['service_transitions']['events_emitted']}, "
        f"heals={b['service_transitions']['heals']}",
        f"- **Check transitions** — {b['check_transitions']['scenario']}: "
        f"final={b['check_transitions']['final_check_states']}, "
        f"events={b['check_transitions']['events_emitted']}",
        f"- **Pressure advisory** — {b['pressure_advisory']['scenario']}: "
        f"final_warnings={b['pressure_advisory']['final_pressure_warnings']}, "
        f"events={b['pressure_advisory']['events_emitted']}, "
        f"mutates={b['pressure_advisory']['advisory_mutates']}",
        f"- **State file shape** — keys: {b['state_file_shape']['top_level_keys']}",
        "",
        "### `pulse.py status` rendering",
        "",
        "```",
        *b["status_rendering"]["stdout_normalized"],
        "```",
        "",
        "## Cycle/timing baseline",
        "",
        f"- cycles: {t['cycles']}",
        f"- avg: {t['avg_ms']} ms/cycle, p50: {t['p50_ms']} ms, p95: {t['p95_ms']} ms, max: {t['max_ms']} ms",
        "",
        "## Blocked conditions",
        "",
        *[f"- {c}" for c in proof["blocked_conditions"]],
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the no-infra pulse proof packet.")
    parser.add_argument("--out", default=None, help="Output directory for the proof artifact.")
    parser.add_argument("--cycles", type=int, default=200, help="Timing-baseline reconcile cycles.")
    parser.add_argument("--run-id", default=None, help="Override the run-id directory name.")
    args = parser.parse_args()

    proof = build_proof(args.cycles)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = Path(args.out) if args.out else ROOT_DIR / "tests" / "artifacts" / "perf" / run_id / "pulse"
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "proof.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    (out_dir / "proof.md").write_text(_render_markdown(proof), encoding="utf-8")

    print(f"pulse proof written: {out_dir}")
    print(f"behavioral_fingerprint_sha256: {proof['behavioral_fingerprint_sha256']}")
    print(f"timing avg_ms: {proof['timing']['avg_ms']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
