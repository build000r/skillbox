#!/usr/bin/env python3
"""
pulse — live reconciliation daemon for the skillbox runtime graph.

Watches declared vs actual state on a fixed interval. When drift is detected:
- Safe drift (crashed service, missing log dir) is auto-healed.
- Risky drift (missing required repo, config change) emits an event for agents.

Every state change is written to the runtime journal (logs/runtime/journal.jsonl)
and queryable via the skillbox_journal / skillbox_pulse MCP tools.

Designed to run as a managed service declared in runtime.yaml.
Start: python3 .env-manager/pulse.py [--interval 30] [--root-dir /workspace]
Stop:  kill $(cat logs/runtime/pulse.pid)  — or use `make pulse-stop`
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT_DIR = SCRIPT_DIR.parent.resolve()
SCRIPTS_DIR = DEFAULT_ROOT_DIR / "scripts"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.runtime_model import build_runtime_model  # noqa: E402
from manage import (  # noqa: E402
    DEFAULT_SERVICE_START_WAIT_SECONDS,
    emit_event,
    ensure_directory,
    filter_model,
    live_service_pid,
    normalize_active_clients,
    normalize_active_profiles,
    process_is_running,
    probe_service,
    remove_pid_file,
    resolve_runtime_command_cwd,
    service_paths,
    service_supports_lifecycle,
    sync_runtime,
    tail_lines,
    translated_runtime_command,
    wait_for_service_health,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL = 30
PID_REL = Path("logs") / "runtime" / "pulse.pid"
STATE_REL = Path("logs") / "runtime" / "pulse.state.json"
LOG_REL = Path("logs") / "runtime" / "pulse.log"

# ---------------------------------------------------------------------------
# Logging (structured, to file + stderr)
# ---------------------------------------------------------------------------

_log_handle = None


def _open_log(root_dir: Path) -> None:
    global _log_handle
    log_path = root_dir / LOG_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_handle = log_path.open("a", encoding="utf-8")


def log(level: str, message: str, **extra: Any) -> None:
    entry = {
        "ts": time.time(),
        "level": level,
        "msg": message,
        **extra,
    }
    line = json.dumps(entry, separators=(",", ":"), default=str)
    if _log_handle:
        _log_handle.write(line + "\n")
        _log_handle.flush()
    print(f"[pulse] {level}: {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# PID management
# ---------------------------------------------------------------------------

def write_pid(root_dir: Path) -> Path:
    pid_path = root_dir / PID_REL
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return pid_path


def remove_pid(root_dir: Path) -> None:
    pid_path = root_dir / PID_REL
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def existing_pid(root_dir: Path) -> int | None:
    pid_path = root_dir / PID_REL
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None
    if process_is_running(pid):
        return pid
    # Stale PID file — clean up.
    remove_pid(root_dir)
    return None


# ---------------------------------------------------------------------------
# State snapshot — track what changed between cycles
# ---------------------------------------------------------------------------

def _model_config_hash(root_dir: Path) -> str:
    """Hash the raw bytes of runtime.yaml + all overlay files to detect config edits."""
    h = hashlib.sha256()
    runtime_yaml = root_dir / "workspace" / "runtime.yaml"
    if runtime_yaml.is_file():
        h.update(runtime_yaml.read_bytes())
    overlays_dir = root_dir / "workspace" / "clients"
    if overlays_dir.is_dir():
        for overlay in sorted(overlays_dir.glob("*/overlay.yaml")):
            h.update(overlay.read_bytes())
    env_file = root_dir / ".env"
    if env_file.is_file():
        h.update(env_file.read_bytes())
    return h.hexdigest()[:16]


def _snapshot_services(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a {service_id: probe_result} map of current service states."""
    return {
        service["id"]: probe_service(model, service)
        for service in model.get("services", [])
    }


def _snapshot_checks(model: dict[str, Any]) -> dict[str, bool]:
    """Build a {check_id: ok} map of current check states."""
    results: dict[str, bool] = {}
    for check in model.get("checks", []):
        check_id = check["id"]
        if check["type"] == "path_exists":
            results[check_id] = Path(str(check["host_path"])).exists()
        else:
            results[check_id] = True  # Unknown check types pass by default.
    return results


# ---------------------------------------------------------------------------
# Auto-heal: restart a crashed managed service
# ---------------------------------------------------------------------------

def _restart_service(
    model: dict[str, Any],
    service: dict[str, Any],
    reason: str,
) -> bool:
    """Attempt to restart a single crashed service. Returns True on success."""
    service_id = service["id"]
    manageable, skip_reason = service_supports_lifecycle(service)
    if not manageable:
        log("debug", f"skip restart {service_id}: {skip_reason}")
        return False

    paths = service_paths(model, service)
    command, env = translated_runtime_command(model, service)
    cwd = resolve_runtime_command_cwd(model, service)

    ensure_directory(paths["log_dir"], dry_run=False)

    log("info", f"restarting {service_id}", reason=reason, command=command)

    try:
        with paths["log_file"].open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=True,
                start_new_session=True,
                text=True,
            )
        paths["pid_file"].write_text(f"{process.pid}\n", encoding="utf-8")
        health = wait_for_service_health(
            service, process, DEFAULT_SERVICE_START_WAIT_SECONDS,
        )
        if health.get("state") in {"failed", "timeout"}:
            # Clean up — don't leave a zombie.
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except OSError:
                pass
            remove_pid_file(paths["pid_file"])
            emit_event("pulse.restart_failed", service_id, {
                "reason": reason,
                "health_state": health.get("state"),
            })
            log("warn", f"restart failed for {service_id}", state=health.get("state"))
            return False

        emit_event("pulse.restarted", service_id, {
            "reason": reason,
            "pid": process.pid,
        })
        log("info", f"restarted {service_id}", pid=process.pid)
        return True

    except Exception as exc:
        emit_event("pulse.restart_failed", service_id, {
            "reason": reason,
            "error": str(exc),
        })
        log("error", f"restart exception for {service_id}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Core reconciliation cycle
# ---------------------------------------------------------------------------

class PulseState:
    """Mutable state that persists across cycles."""

    def __init__(self) -> None:
        self.config_hash: str = ""
        self.service_states: dict[str, str] = {}  # service_id → state string
        self.check_states: dict[str, bool] = {}    # check_id → ok
        self.restart_backoff: dict[str, float] = {}  # service_id → next eligible restart time
        self.cycle_count: int = 0
        self.heals: int = 0
        self.events_emitted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_count": self.cycle_count,
            "heals": self.heals,
            "events_emitted": self.events_emitted,
            "config_hash": self.config_hash,
            "service_states": dict(self.service_states),
            "check_states": dict(self.check_states),
        }


# Backoff: after a failed restart, wait this many seconds before retrying.
RESTART_BACKOFF_SECONDS = 120.0
# Maximum consecutive restart attempts tracked per service.
MAX_RESTART_ATTEMPTS = 3


def reconcile_once(
    root_dir: Path,
    state: PulseState,
    *,
    auto_restart: bool = True,
    auto_sync: bool = False,
) -> None:
    """Run one reconciliation cycle."""
    state.cycle_count += 1

    # -----------------------------------------------------------------------
    # 1. Reload the runtime model (picks up any YAML/env changes).
    # -----------------------------------------------------------------------
    try:
        model = build_runtime_model(root_dir)
    except Exception as exc:
        log("error", f"failed to load runtime model: {exc}")
        return

    profiles = normalize_active_profiles(None)
    try:
        clients = normalize_active_clients(model, None)
    except RuntimeError:
        clients = set()
    model = filter_model(model, profiles, clients)

    # -----------------------------------------------------------------------
    # 2. Detect config changes.
    # -----------------------------------------------------------------------
    new_hash = _model_config_hash(root_dir)
    if state.config_hash and new_hash != state.config_hash:
        emit_event("pulse.config_changed", "runtime", {
            "old_hash": state.config_hash,
            "new_hash": new_hash,
        })
        state.events_emitted += 1
        log("info", "config changed", old=state.config_hash, new=new_hash)

        if auto_sync:
            try:
                actions = sync_runtime(model, dry_run=False)
                emit_event("pulse.auto_sync", "runtime", {
                    "action_count": len(actions),
                })
                state.events_emitted += 1
                log("info", f"auto-sync completed ({len(actions)} actions)")
            except Exception as exc:
                log("error", f"auto-sync failed: {exc}")
    state.config_hash = new_hash

    # -----------------------------------------------------------------------
    # 3. Check services — detect crashes, state transitions.
    # -----------------------------------------------------------------------
    now = time.monotonic()
    current_services = _snapshot_services(model)
    services_by_id = {s["id"]: s for s in model.get("services", [])}

    for service_id, probe in current_services.items():
        current_state = probe.get("state", "declared")
        previous_state = state.service_states.get(service_id)

        # First cycle — just record, don't react.
        if previous_state is None:
            state.service_states[service_id] = current_state
            continue

        # State transition detected.
        if current_state != previous_state:
            is_crash = (
                previous_state in ("running", "starting")
                and current_state in ("down", "declared")
            )

            event_type = "pulse.service_crashed" if is_crash else "pulse.service_state_changed"
            emit_event(event_type, service_id, {
                "from": previous_state,
                "to": current_state,
            })
            state.events_emitted += 1
            log(
                "warn" if is_crash else "info",
                f"service {service_id}: {previous_state} -> {current_state}",
            )

            # Auto-restart crashed managed services.
            if is_crash and auto_restart:
                service = services_by_id.get(service_id)
                if service and service_supports_lifecycle(service)[0]:
                    backoff_until = state.restart_backoff.get(service_id, 0)
                    if now >= backoff_until:
                        ok = _restart_service(model, service, reason="crashed")
                        if ok:
                            state.heals += 1
                            current_state = "running"
                            # Clear backoff on success.
                            state.restart_backoff.pop(service_id, None)
                        else:
                            state.restart_backoff[service_id] = now + RESTART_BACKOFF_SECONDS
                    else:
                        remaining = int(backoff_until - now)
                        log("info", f"skipping restart for {service_id} (backoff {remaining}s)")

        state.service_states[service_id] = current_state

    # -----------------------------------------------------------------------
    # 4. Run declared checks — detect failures and recoveries.
    # -----------------------------------------------------------------------
    current_checks = _snapshot_checks(model)

    for check_id, ok in current_checks.items():
        previous_ok = state.check_states.get(check_id)

        if previous_ok is None:
            state.check_states[check_id] = ok
            continue

        if ok != previous_ok:
            event_type = "pulse.check_recovered" if ok else "pulse.check_failed"
            emit_event(event_type, check_id, {"ok": ok})
            state.events_emitted += 1
            log(
                "info" if ok else "warn",
                f"check {check_id}: {'recovered' if ok else 'failed'}",
            )

        state.check_states[check_id] = ok

    # -----------------------------------------------------------------------
    # 5. Persist state snapshot for the MCP tool to read.
    # -----------------------------------------------------------------------
    state_path = root_dir / STATE_REL
    state_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "pid": os.getpid(),
        "updated_at": time.time(),
        "interval": getattr(reconcile_once, "_interval", DEFAULT_INTERVAL),
        "auto_restart": auto_restart,
        "auto_sync": auto_sync,
    } | state.to_dict()
    try:
        state_path.write_text(
            json.dumps(snapshot, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

_shutdown = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    _shutdown = True
    log("info", f"received signal {signum}, shutting down")


def run_daemon(
    root_dir: Path,
    *,
    interval: int = DEFAULT_INTERVAL,
    auto_restart: bool = True,
    auto_sync: bool = False,
) -> int:
    """Run the pulse daemon until signalled to stop."""
    global _shutdown

    # Prevent double-start.
    running_pid = existing_pid(root_dir)
    if running_pid is not None:
        print(f"[pulse] already running (pid {running_pid})", file=sys.stderr)
        return 1

    _open_log(root_dir)
    pid_path = write_pid(root_dir)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Stash interval so reconcile_once can include it in state snapshots.
    reconcile_once._interval = interval  # type: ignore[attr-defined]

    emit_event("pulse.started", "daemon", {
        "pid": os.getpid(),
        "interval": interval,
        "auto_restart": auto_restart,
        "auto_sync": auto_sync,
    }, root_dir)
    log("info", "started", pid=os.getpid(), interval=interval)

    state = PulseState()

    try:
        while not _shutdown:
            try:
                reconcile_once(
                    root_dir,
                    state,
                    auto_restart=auto_restart,
                    auto_sync=auto_sync,
                )
            except Exception as exc:
                log("error", f"cycle failed: {exc}")

            # Sleep in small increments so we respond to signals promptly.
            deadline = time.monotonic() + interval
            while not _shutdown and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
    finally:
        emit_event("pulse.stopped", "daemon", {
            "pid": os.getpid(),
            "cycles": state.cycle_count,
            "heals": state.heals,
            "events": state.events_emitted,
        }, root_dir)
        log("info", "stopped", cycles=state.cycle_count, heals=state.heals)
        remove_pid(root_dir)
        if _log_handle:
            _log_handle.close()

    return 0


# ---------------------------------------------------------------------------
# Status (non-daemon mode for `make pulse-status`)
# ---------------------------------------------------------------------------

def print_status(root_dir: Path) -> int:
    """Print current pulse status from the persisted state file."""
    state_path = root_dir / STATE_REL
    pid_path = root_dir / PID_REL

    running_pid = existing_pid(root_dir)

    if not state_path.is_file():
        if running_pid:
            print(f"pulse: running (pid {running_pid}), no state file yet")
        else:
            print("pulse: not running (no state file)")
        return 0

    try:
        snapshot = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"pulse: error reading state: {exc}")
        return 1

    alive = running_pid is not None
    status = "running" if alive else "stopped"
    pid = snapshot.get("pid", "?")
    cycles = snapshot.get("cycle_count", 0)
    heals = snapshot.get("heals", 0)
    events = snapshot.get("events_emitted", 0)
    interval = snapshot.get("interval", "?")
    updated = snapshot.get("updated_at")
    age = f"{time.time() - updated:.0f}s ago" if updated else "unknown"

    print(f"pulse: {status} (pid {pid})")
    print(f"  interval:  {interval}s")
    print(f"  cycles:    {cycles}")
    print(f"  heals:     {heals}")
    print(f"  events:    {events}")
    print(f"  last tick: {age}")

    service_states = snapshot.get("service_states", {})
    if service_states:
        print(f"  services:")
        for sid, sstate in sorted(service_states.items()):
            marker = "+" if sstate == "running" else "-" if sstate == "down" else "~"
            print(f"    {marker} {sid}: {sstate}")

    check_states = snapshot.get("check_states", {})
    failed = [cid for cid, ok in check_states.items() if not ok]
    if failed:
        print(f"  failed checks: {', '.join(sorted(failed))}")
    elif check_states:
        print(f"  checks: all passing ({len(check_states)})")

    return 0


def read_state(root_dir: Path) -> dict[str, Any]:
    """Read pulse state for programmatic consumers (MCP tool)."""
    state_path = root_dir / STATE_REL
    running_pid = existing_pid(root_dir)

    result: dict[str, Any] = {
        "running": running_pid is not None,
        "pid": running_pid,
    }

    if state_path.is_file():
        try:
            snapshot = json.loads(state_path.read_text(encoding="utf-8"))
            result.update(snapshot)
            result["running"] = running_pid is not None
            result["pid"] = running_pid
            if snapshot.get("updated_at"):
                result["seconds_since_tick"] = round(time.time() - snapshot["updated_at"], 1)
        except (json.JSONDecodeError, OSError):
            result["state_error"] = "failed to read state file"

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pulse — live reconciliation daemon for the skillbox runtime graph.",
    )
    parser.add_argument(
        "--root-dir",
        default=None,
        help="Override the repo root (default: parent of this script's directory).",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Start the pulse daemon (foreground).")
    run_parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=f"Seconds between reconciliation cycles (default: {DEFAULT_INTERVAL}).",
    )
    run_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Disable auto-restart of crashed services.",
    )
    run_parser.add_argument(
        "--auto-sync",
        action="store_true",
        help="Auto-run sync when config changes are detected.",
    )

    sub.add_parser("status", help="Print current pulse daemon status.")
    sub.add_parser("stop", help="Send SIGTERM to the running pulse daemon.")

    args = parser.parse_args()
    root_dir = Path(args.root_dir).resolve() if args.root_dir else DEFAULT_ROOT_DIR

    command = args.command or "run"

    if command == "status":
        return print_status(root_dir)

    if command == "stop":
        pid = existing_pid(root_dir)
        if pid is None:
            print("[pulse] not running")
            return 0
        os.kill(pid, signal.SIGTERM)
        print(f"[pulse] sent SIGTERM to {pid}")
        return 0

    # Default: run the daemon.
    interval = args.interval if hasattr(args, "interval") and args.interval else None
    if interval is None:
        env_interval = os.environ.get("SKILLBOX_PULSE_INTERVAL", "").strip()
        interval = int(env_interval) if env_interval else DEFAULT_INTERVAL

    return run_daemon(
        root_dir,
        interval=interval,
        auto_restart=not getattr(args, "no_restart", False),
        auto_sync=getattr(args, "auto_sync", False),
    )


if __name__ == "__main__":
    sys.exit(main())
