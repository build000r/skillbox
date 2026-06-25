#!/usr/bin/env python3
"""
pulse — live reconciliation daemon for the skillbox runtime graph.

Watches declared vs actual state on a fixed interval. When drift is detected:
- Safe drift (crashed service, missing log dir) is auto-healed.
- Risky drift (missing required repo, config change) emits an event for agents.

Every state change is logged to logs/runtime/runtime.log
and queryable via the skillbox_pulse MCP tool.

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

from lib.runtime_model import (  # noqa: E402
    build_runtime_model,
    client_overlay_paths,
    load_runtime_env,
    runtime_path_to_host_path,
)
from manage import (  # noqa: E402
    DEFAULT_SERVICE_START_WAIT_SECONDS,
    DEFAULT_SERVICE_STOP_WAIT_SECONDS,
    StateLockTimeout,
    all_process_listeners,
    build_port_registry,
    log_runtime_event,
    ensure_directory,
    filter_model,
    locked_json_update,
    normalize_active_clients,
    normalize_active_profiles,
    process_is_running,
    probe_service,
    process_tree_pids,
    read_service_pid,
    remove_pid_file,
    resolve_runtime_command_cwd,
    runtime_pressure_advisory,
    service_paths,
    service_supports_lifecycle,
    stop_process,
    sync_runtime,
    translated_runtime_command,
    wait_for_service_health,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL = 30
# pulse persists its pid/state/log inside the SAME runtime log directory the
# runtime manager resolves for the pulse service. The manager's path_exists
# healthcheck plus its stop/status logic all read <runtime-log-dir>/pulse.pid,
# where the directory comes from SKILLBOX_LOG_ROOT (host path
# .skillbox-state/logs/runtime). Writing the pid anywhere else means the manager
# can never see pulse: it reports the daemon "down" forever, and every `up`
# spawns another orphan. Resolve the directory from the runtime model so pulse
# and the manager always agree.
PID_NAME = "pulse.pid"
STATE_NAME = "pulse.state.json"
LOG_NAME = "pulse.log"
PORT_GUARD_TELEMETRY_NAME = "port-guard.telemetry.json"
DEFAULT_UNHEALTHY_GRACE_SECONDS = 60.0
DEFAULT_PORT_SENTINEL_MODE = "observe"
DEFAULT_PORT_SENTINEL_GRACE_SECONDS = 15.0
PORT_SENTINEL_MODES = {"off", "observe", "enforce"}
PORT_SENTINEL_REAP_WAIT_SECONDS = 1.0
PORT_SENTINEL_SYSTEM_NAMES = {
    "containerd",
    "docker-proxy",
    "dockerd",
    "nginx",
    "sshd",
    "systemd-resolve",
    "tailscaled",
}
PORT_SENTINEL_DEV_SIGNATURES = (
    ("vite", ("vite",)),
    ("next", ("next",)),
    ("webpack-dev-server", ("webpack-dev-server",)),
    ("webpack-serve", ("webpack", "serve")),
    ("react-scripts", ("react-scripts", "start")),
    ("turbopack", ("turbo", "dev")),
)
PORT_GUARD_COUNTER_KEYS = (
    "hook_blocks",
    "shim_blocks",
    "post_bind_mismatches",
    "rogues_seen",
    "rogues_reaped",
    "wildcard_criticals",
)

_runtime_dir_cache: dict[Path, Path] = {}


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        marker = str(path)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(path)
    return unique


def _state_runtime_dir(root_dir: Path) -> Path:
    env_values = load_runtime_env(root_dir)
    log_root = str(env_values.get("SKILLBOX_LOG_ROOT") or "/workspace/logs").rstrip("/")
    return runtime_path_to_host_path(root_dir, env_values, f"{log_root}/runtime")


def _runtime_dir(root_dir: Path) -> Path:
    """Runtime log directory the manager uses for the pulse service.

    Falls back to <root_dir>/logs/runtime when the model can't be built (the
    bare ``stop``/``status`` CLI paths, degraded environments). Only a
    successful model resolution is cached, so a transient failure never pins
    the fallback.
    """
    cached = _runtime_dir_cache.get(root_dir)
    if cached is not None:
        return cached
    try:
        model = build_runtime_model(root_dir)
        pulse_service = next(
            (svc for svc in model.get("services", []) if svc.get("id") == "pulse"),
            None,
        )
        if pulse_service is not None:
            runtime_dir = service_paths(model, pulse_service)["log_dir"]
            _runtime_dir_cache[root_dir] = runtime_dir
            return runtime_dir
    except Exception:
        pass
    try:
        return _state_runtime_dir(root_dir)
    except Exception:
        return root_dir / "logs" / "runtime"


def _runtime_dir_candidates(root_dir: Path) -> list[Path]:
    candidates = [_runtime_dir(root_dir)]
    try:
        candidates.append(_state_runtime_dir(root_dir))
    except Exception:
        pass
    candidates.extend(
        [
            root_dir / ".skillbox-state" / "logs" / "runtime",
            root_dir / "logs" / "runtime",
        ]
    )
    return _unique_paths(candidates)


def pulse_pid_path(root_dir: Path) -> Path:
    return _runtime_dir(root_dir) / PID_NAME


def pulse_pid_candidates(root_dir: Path) -> list[Path]:
    return [path / PID_NAME for path in _runtime_dir_candidates(root_dir)]


def pulse_state_path(root_dir: Path) -> Path:
    return _runtime_dir(root_dir) / STATE_NAME


def pulse_state_candidates(root_dir: Path) -> list[Path]:
    return [path / STATE_NAME for path in _runtime_dir_candidates(root_dir)]


def port_guard_telemetry_path(root_dir: Path) -> Path:
    return _runtime_dir(root_dir) / PORT_GUARD_TELEMETRY_NAME


def port_guard_telemetry_candidates(root_dir: Path) -> list[Path]:
    return [path / PORT_GUARD_TELEMETRY_NAME for path in _runtime_dir_candidates(root_dir)]


def pulse_log_path(root_dir: Path) -> Path:
    return _runtime_dir(root_dir) / LOG_NAME

# ---------------------------------------------------------------------------
# Logging (structured, to file + stderr)
# ---------------------------------------------------------------------------

_log_handle = None


def _open_log(root_dir: Path) -> None:
    global _log_handle
    log_path = pulse_log_path(root_dir)
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
    pid_path = pulse_pid_path(root_dir)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = pid_path.with_suffix(pid_path.suffix + ".tmp")
    tmp_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    os.replace(tmp_path, pid_path)
    return pid_path


def remove_pid(root_dir: Path) -> None:
    for pid_path in pulse_pid_candidates(root_dir):
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass


def existing_pid(root_dir: Path) -> int | None:
    for pid_path in pulse_pid_candidates(root_dir):
        if not pid_path.exists():
            continue
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            continue
        if process_is_running(pid):
            return pid
        # Stale PID file — clean up.
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
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
    env_values = load_runtime_env(root_dir)
    for overlay in client_overlay_paths(root_dir, env_values):
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
    cleanup_paths = _move_restart_cleanup_paths(service, cwd)

    log(
        "info",
        f"restarting {service_id}",
        reason=reason,
        command=command,
        cleanup_paths=cleanup_paths,
    )

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
        try:
            tmp_pid = paths["pid_file"].with_suffix(paths["pid_file"].suffix + ".tmp")
            tmp_pid.write_text(f"{process.pid}\n", encoding="utf-8")
            os.replace(tmp_pid, paths["pid_file"])
        except OSError:
            # PID write failed — don't leave an orphan child untracked.
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except OSError:
                pass
            raise
        wait_seconds = float(service.get("start_wait_seconds") or DEFAULT_SERVICE_START_WAIT_SECONDS)
        health = wait_for_service_health(
            service, process, wait_seconds,
        )
        if health.get("state") in {"failed", "timeout"}:
            # Clean up — don't leave a zombie. SIGTERM alone isn't enough:
            # a service that ignores it would survive while the pid file is
            # removed, and pulse would then start a fresh copy each cycle.
            # stop_process escalates to SIGKILL after wait_seconds.
            stop_process(process.pid, DEFAULT_SERVICE_STOP_WAIT_SECONDS)
            remove_pid_file(paths["pid_file"])
            log_runtime_event("pulse.restart_failed", service_id, {
                "reason": reason,
                "health_state": health.get("state"),
            })
            log("warn", f"restart failed for {service_id}", state=health.get("state"))
            return False

        log_runtime_event("pulse.restarted", service_id, {
            "reason": reason,
            "pid": process.pid,
        })
        log("info", f"restarted {service_id}", pid=process.pid)
        return True

    except Exception as exc:
        log_runtime_event("pulse.restart_failed", service_id, {
            "reason": reason,
            "error": str(exc),
        })
        log("error", f"restart exception for {service_id}: {exc}")
        return False


def _move_restart_cleanup_paths(service: dict[str, Any], cwd: Path) -> list[dict[str, str]]:
    """Move service-declared generated state aside before a supervised restart."""
    moved: list[dict[str, str]] = []
    raw_paths = service.get("restart_cleanup_paths") or []
    if not isinstance(raw_paths, list):
        return moved

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for raw_path in raw_paths:
        text = str(raw_path).strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = cwd / path
        if not path.exists():
            continue

        target = path.with_name(f"{path.name}.stale-pulse-{stamp}")
        suffix = 1
        while target.exists():
            target = path.with_name(f"{path.name}.stale-pulse-{stamp}-{suffix}")
            suffix += 1
        try:
            path.rename(target)
            moved.append({"from": str(path), "to": str(target)})
        except OSError as exc:
            log("warn", f"failed cleanup move for {service.get('id')}: {exc}", path=str(path))
    return moved


def _service_should_ensure_running(service: dict[str, Any]) -> bool:
    return bool(service.get("supervise") or service.get("required"))


def _restart_with_backoff(
    model: dict[str, Any],
    state: "PulseState",
    service: dict[str, Any],
    service_id: str,
    *,
    now: float,
    reason: str,
) -> bool | None:
    backoff_until = state.restart_backoff.get(service_id, 0)
    if now < backoff_until:
        remaining = int(backoff_until - now)
        log("info", f"skipping restart for {service_id} (backoff {remaining}s)")
        return None
    attempts = state.restart_attempts.get(service_id, 0)
    if attempts >= MAX_RESTART_ATTEMPTS:
        state.restart_backoff[service_id] = now + RESTART_BACKOFF_SECONDS
        log_runtime_event("pulse.restart_suppressed", service_id, {
            "reason": reason,
            "attempts": attempts,
            "max_attempts": MAX_RESTART_ATTEMPTS,
        })
        state.events_emitted += 1
        log("warn", f"suppressing restart for {service_id} after {attempts} failed attempts")
        return None

    ok = _restart_service(model, service, reason=reason)
    if ok:
        state.heals += 1
        state.restart_backoff.pop(service_id, None)
        state.restart_attempts.pop(service_id, None)
        return True

    state.restart_attempts[service_id] = attempts + 1
    state.restart_backoff[service_id] = now + RESTART_BACKOFF_SECONDS
    return False


# ---------------------------------------------------------------------------
# Port sentinel: observe/reap rogue dev-server listeners
# ---------------------------------------------------------------------------

def copy_port_sentinel_counters(counters: dict[str, Any]) -> dict[str, Any]:
    copied = {
        key: int(counters.get(key) or 0)
        for key in PORT_GUARD_COUNTER_KEYS
    }
    copied["by_signature"] = dict(counters.get("by_signature") or {})
    for key in ("first_seen_at", "last_seen_at", "last_reaped_at"):
        value = str(counters.get(key) or "").strip()
        if value:
            copied[key] = value
    return copied


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _default_port_guard_counters() -> dict[str, Any]:
    return {
        **{key: 0 for key in PORT_GUARD_COUNTER_KEYS},
        "by_signature": {},
    }


def _normalize_port_guard_counters(raw: dict[str, Any] | None) -> dict[str, Any]:
    counters = _default_port_guard_counters()
    if not isinstance(raw, dict):
        return counters
    for key in PORT_GUARD_COUNTER_KEYS:
        try:
            counters[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            counters[key] = 0
    if isinstance(raw.get("by_signature"), dict):
        counters["by_signature"] = {
            str(key): int(value or 0)
            for key, value in raw["by_signature"].items()
            if str(key).strip()
        }
    for key in ("first_seen_at", "last_seen_at", "last_reaped_at"):
        value = str(raw.get(key) or "").strip()
        if value:
            counters[key] = value
    return counters


def _touch_port_guard_counters(counters: dict[str, Any], *, timestamp: str | None = None) -> None:
    stamp = timestamp or _utc_timestamp()
    counters.setdefault("first_seen_at", stamp)
    counters["last_seen_at"] = stamp


def _increment_port_guard_counter(
    counters: dict[str, Any],
    key: str,
    amount: int = 1,
    *,
    timestamp: str | None = None,
) -> None:
    if key not in PORT_GUARD_COUNTER_KEYS:
        return
    counters[key] = int(counters.get(key) or 0) + int(amount)
    _touch_port_guard_counters(counters, timestamp=timestamp)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _external_port_guard_counters(root_dir: Path) -> dict[str, Any]:
    for candidate in port_guard_telemetry_candidates(root_dir):
        if not candidate.is_file():
            continue
        payload = _read_json_object(candidate)
        counters = payload.get("counters") if isinstance(payload.get("counters"), dict) else payload
        if isinstance(counters, dict):
            return _normalize_port_guard_counters(counters)
    return _default_port_guard_counters()


def _merge_port_guard_counters(state: "PulseState", external: dict[str, Any]) -> None:
    external = _normalize_port_guard_counters(external)
    counters = _normalize_port_guard_counters(state.port_sentinel_counters)
    for key in PORT_GUARD_COUNTER_KEYS:
        counters[key] = max(int(counters.get(key) or 0), int(external.get(key) or 0))
    by_signature: dict[str, int] = {}
    for source in (external.get("by_signature") or {}, counters.get("by_signature") or {}):
        for key, value in dict(source).items():
            marker = str(key)
            by_signature[marker] = max(int(by_signature.get(marker) or 0), int(value or 0))
    counters["by_signature"] = by_signature
    for key in ("first_seen_at", "last_seen_at", "last_reaped_at"):
        values = [str(counters.get(key) or "").strip(), str(external.get(key) or "").strip()]
        values = [value for value in values if value]
        if values:
            counters[key] = min(values) if key == "first_seen_at" else max(values)
    state.port_sentinel_counters = counters


def _merge_port_guard_counters_into_snapshot(root_dir: Path, snapshot: dict[str, Any]) -> None:
    port_sentinel = snapshot.get("port_sentinel")
    if not isinstance(port_sentinel, dict):
        port_sentinel = {}
    state = PulseState()
    state.port_sentinel_counters = _normalize_port_guard_counters(port_sentinel)
    _merge_port_guard_counters(state, _external_port_guard_counters(root_dir))
    snapshot["port_sentinel"] = {
        **port_sentinel,
        **copy_port_sentinel_counters(state.port_sentinel_counters),
    }


def load_pulse_state(root_dir: Path) -> "PulseState":
    state = PulseState()
    state_path = next(
        (candidate for candidate in pulse_state_candidates(root_dir) if candidate.is_file()),
        pulse_state_path(root_dir),
    )
    payload = _read_json_object(state_path)
    port_sentinel = payload.get("port_sentinel") if isinstance(payload.get("port_sentinel"), dict) else {}
    state.port_sentinel_counters = _normalize_port_guard_counters(port_sentinel)
    _merge_port_guard_counters(state, _external_port_guard_counters(root_dir))
    return state


def _env_value(model: dict[str, Any], key: str, default: str = "") -> str:
    raw = os.environ.get(key)
    if raw is None:
        raw = (model.get("env") or {}).get(key)
    if raw is None:
        raw = default
    return str(raw).strip()


def _port_sentinel_config(model: dict[str, Any]) -> tuple[str, float]:
    mode = _env_value(model, "SKILLBOX_PORT_SENTINEL", DEFAULT_PORT_SENTINEL_MODE).lower()
    if mode not in PORT_SENTINEL_MODES:
        mode = DEFAULT_PORT_SENTINEL_MODE
    raw_grace = _env_value(
        model,
        "SKILLBOX_PORT_SENTINEL_GRACE_SECONDS",
        str(DEFAULT_PORT_SENTINEL_GRACE_SECONDS),
    )
    try:
        grace_seconds = max(0.0, float(raw_grace))
    except ValueError:
        grace_seconds = DEFAULT_PORT_SENTINEL_GRACE_SECONDS
    return mode, grace_seconds


def _process_identity(pid: int) -> dict[str, Any] | None:
    proc_dir = Path("/proc") / str(pid)
    try:
        raw_cmdline = (proc_dir / "cmdline").read_bytes()
        cmdline = raw_cmdline.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        cmdline = ""
    try:
        comm = (proc_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        comm = ""
    try:
        raw_stat = (proc_dir / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _before, _sep, after = raw_stat.rpartition(")")
    fields = after.split()
    start_time = fields[19] if len(fields) > 19 else ""
    return {
        "pid": pid,
        "comm": comm,
        "cmdline": cmdline or comm,
        "start_time": start_time,
    }


def _dev_server_signature(identity: dict[str, Any]) -> str:
    haystack = f"{identity.get('comm') or ''} {identity.get('cmdline') or ''}".lower()
    for label, tokens in PORT_SENTINEL_DEV_SIGNATURES:
        if all(token in haystack for token in tokens):
            return label
    return ""


def _system_listener_allowed(identity: dict[str, Any], port: int, signature: str) -> bool:
    comm = str(identity.get("comm") or "").strip().lower()
    cmdline = str(identity.get("cmdline") or "").strip().lower()
    if comm in PORT_SENTINEL_SYSTEM_NAMES:
        return True
    if any(name in cmdline for name in PORT_SENTINEL_SYSTEM_NAMES):
        return True
    return False


def _declared_port_set(model: dict[str, Any]) -> set[int]:
    ports: set[int] = set()
    try:
        entries = build_port_registry(model)
    except Exception:
        return ports
    for entry in entries:
        if entry.get("warning") or entry.get("port") is None:
            continue
        try:
            ports.add(int(entry["port"]))
        except (TypeError, ValueError):
            continue
    return ports


def _managed_service_pids(model: dict[str, Any]) -> set[int]:
    managed: set[int] = set()
    for service in model.get("services") or []:
        try:
            pid = read_service_pid(service_paths(model, service)["pid_file"])
        except Exception:
            pid = None
        if pid is None or not process_is_running(pid):
            continue
        try:
            managed.update(process_tree_pids(pid))
        except Exception:
            managed.add(pid)
    return managed


def _candidate_key(pid: int, port: int, start_time: str) -> str:
    return f"{pid}:{port}:{start_time}"


def _scan_rogue_listeners(model: dict[str, Any]) -> list[dict[str, Any]]:
    declared_ports = _declared_port_set(model)
    managed_pids = _managed_service_pids(model)
    candidates: list[dict[str, Any]] = []
    identity_cache: dict[int, dict[str, Any] | None] = {}

    for listener in all_process_listeners():
        try:
            pid = int(listener.get("pid"))
            port = int(listener.get("port"))
        except (TypeError, ValueError):
            continue
        if pid in managed_pids:
            continue
        identity = identity_cache.setdefault(pid, _process_identity(pid))
        if identity is None:
            continue
        signature = _dev_server_signature(identity)
        if _system_listener_allowed(identity, port, signature):
            continue
        enforcement = "dev-server" if signature else "report-only"
        reason = "dev_server_signature" if signature else "unmanaged_listener"
        if port in declared_ports and signature:
            reason = "dev_server_on_declared_port"
        candidate = {
            "key": _candidate_key(pid, port, str(identity.get("start_time") or "")),
            "pid": pid,
            "port": port,
            "comm": identity.get("comm") or "",
            "cmdline": identity.get("cmdline") or "",
            "start_time": identity.get("start_time") or "",
            "signature": signature or "none",
            "enforcement": enforcement,
            "reason": reason,
            "declared_port": port in declared_ports,
        }
        candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item["port"], item["pid"]))


def _same_process(candidate: dict[str, Any], identity: dict[str, Any] | None) -> bool:
    if identity is None:
        return False
    return (
        str(identity.get("start_time") or "") == str(candidate.get("start_time") or "")
        and str(identity.get("cmdline") or "") == str(candidate.get("cmdline") or "")
    )


def _terminate_rogue(candidate: dict[str, Any]) -> str:
    pid = int(candidate["pid"])
    identity = _process_identity(pid)
    if not _same_process(candidate, identity):
        return "skipped-pid-reused"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-gone"
    except PermissionError:
        return "permission-denied"

    deadline = time.monotonic() + PORT_SENTINEL_REAP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return "terminated"
        time.sleep(0.05)

    identity = _process_identity(pid)
    if not _same_process(candidate, identity):
        return "skipped-pid-reused"
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        return "permission-denied"
    return "killed"


def _record_port_sentinel_seen(state: "PulseState", candidate: dict[str, Any]) -> None:
    counters = state.port_sentinel_counters
    _increment_port_guard_counter(counters, "rogues_seen")
    by_signature = counters.setdefault("by_signature", {})
    signature = str(candidate.get("signature") or "none")
    by_signature[signature] = int(by_signature.get(signature) or 0) + 1
    if _candidate_uses_wildcard(candidate) and candidate.get("enforcement") == "dev-server":
        _increment_port_guard_counter(counters, "wildcard_criticals")


def _candidate_uses_wildcard(candidate: dict[str, Any]) -> bool:
    cmdline = str(candidate.get("cmdline") or "").lower()
    return (
        "0.0.0.0" in cmdline
        or "::" in cmdline
        or "--host=0" in cmdline
        or "--host 0" in cmdline
    )


def _port_sentinel_event(action: str, candidate: dict[str, Any], state: "PulseState", **extra: Any) -> None:
    detail = {
        "kind": "port_sentinel",
        "action": action,
        "pid": candidate.get("pid"),
        "port": candidate.get("port"),
        "signature": candidate.get("signature"),
        "reason": candidate.get("reason"),
        "enforcement": candidate.get("enforcement"),
        **extra,
    }
    log_runtime_event("pulse.port_sentinel", str(candidate.get("pid")), detail)
    state.events_emitted += 1
    log("warn", f"port sentinel {action}: pid {candidate.get('pid')} port {candidate.get('port')}")


def _reconcile_port_sentinel(model: dict[str, Any], state: "PulseState", *, now: float) -> None:
    mode, grace_seconds = _port_sentinel_config(model)
    state.port_sentinel_mode = mode
    state.port_sentinel_grace_seconds = grace_seconds
    if mode == "off":
        state.port_sentinel_last_candidates = []
        state.port_sentinel_first_seen.clear()
        return

    candidates = _scan_rogue_listeners(model)
    active_keys = {candidate["key"] for candidate in candidates}
    for stale_key in list(state.port_sentinel_first_seen):
        if stale_key not in active_keys:
            state.port_sentinel_first_seen.pop(stale_key, None)

    last_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        key = candidate["key"]
        first_observation = key not in state.port_sentinel_first_seen
        first_seen = state.port_sentinel_first_seen.setdefault(key, now)
        age_seconds = max(0.0, now - first_seen)
        candidate_view = {
            "pid": candidate["pid"],
            "port": candidate["port"],
            "signature": candidate["signature"],
            "enforcement": candidate["enforcement"],
            "reason": candidate["reason"],
            "age_seconds": round(age_seconds, 1),
        }
        last_candidates.append(candidate_view)

        if first_observation:
            _record_port_sentinel_seen(state, candidate)
            _port_sentinel_event("observed", candidate, state, mode=mode)

        if mode != "enforce" or candidate.get("enforcement") != "dev-server":
            continue
        if age_seconds < grace_seconds:
            continue

        action = _terminate_rogue(candidate)
        if action in {"terminated", "killed", "already-gone"}:
            counters = state.port_sentinel_counters
            _increment_port_guard_counter(counters, "rogues_reaped")
            counters["last_reaped_at"] = str(counters.get("last_seen_at") or _utc_timestamp())
            state.port_sentinel_first_seen.pop(key, None)
        _port_sentinel_event(action, candidate, state, mode=mode, age_seconds=round(age_seconds, 1))

    state.port_sentinel_last_candidates = last_candidates[-10:]


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
        self.restart_attempts: dict[str, int] = {}  # service_id → consecutive failed restart attempts
        self.unhealthy_since: dict[str, float] = {}  # service_id → monotonic timestamp
        self.pressure_warnings: list[str] = []
        self.pressure_advisory: dict[str, Any] = {}
        self.port_sentinel_first_seen: dict[str, float] = {}
        self.port_sentinel_mode: str = DEFAULT_PORT_SENTINEL_MODE
        self.port_sentinel_grace_seconds: float = DEFAULT_PORT_SENTINEL_GRACE_SECONDS
        self.port_sentinel_counters: dict[str, Any] = _default_port_guard_counters()
        self.port_sentinel_last_candidates: list[dict[str, Any]] = []
        self.cycle_count: int = 0
        self.heals: int = 0
        self.events_emitted: int = 0

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        unhealthy_for = {}
        if now is not None:
            unhealthy_for = {
                service_id: round(max(0.0, now - started_at), 1)
                for service_id, started_at in self.unhealthy_since.items()
            }
        return {
            "cycle_count": self.cycle_count,
            "heals": self.heals,
            "events_emitted": self.events_emitted,
            "config_hash": self.config_hash,
            "service_states": dict(self.service_states),
            "check_states": dict(self.check_states),
            "pressure_warnings": list(self.pressure_warnings),
            "pressure_advisory": dict(self.pressure_advisory),
            "port_sentinel": {
                "mode": self.port_sentinel_mode,
                "grace_seconds": self.port_sentinel_grace_seconds,
                "active_candidates": len(self.port_sentinel_first_seen),
                "last_candidates": list(self.port_sentinel_last_candidates),
                **copy_port_sentinel_counters(self.port_sentinel_counters),
            },
            "restart_attempts": dict(self.restart_attempts),
            "unhealthy_for_seconds": unhealthy_for,
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
    active_clients: list[str] | None = None,
    active_profiles: list[str] | None = None,
    unhealthy_grace_seconds: float = DEFAULT_UNHEALTHY_GRACE_SECONDS,
) -> None:
    """Run one reconciliation cycle."""
    state.cycle_count += 1

    loaded = _load_pulse_model(root_dir, active_clients, active_profiles)
    if loaded is None:
        return
    model, profiles, clients = loaded
    _handle_pulse_config_change(root_dir, state, model, auto_sync=auto_sync)
    now = time.monotonic()
    _reconcile_pulse_services(
        model,
        state,
        auto_restart=auto_restart,
        unhealthy_grace_seconds=unhealthy_grace_seconds,
        now=now,
    )
    _reconcile_port_sentinel(model, state, now=now)
    _reconcile_pulse_checks(model, state)
    _reconcile_pressure_advisory(root_dir, state)
    _write_pulse_state(
        root_dir,
        state,
        now=now,
        auto_restart=auto_restart,
        auto_sync=auto_sync,
        active_clients=clients,
        active_profiles=profiles,
        unhealthy_grace_seconds=unhealthy_grace_seconds,
    )


def _load_pulse_model(
    root_dir: Path,
    active_clients: list[str] | None,
    active_profiles: list[str] | None,
) -> tuple[dict[str, Any], set[str], set[str]] | None:
    try:
        model = build_runtime_model(root_dir)
    except Exception as exc:
        log("error", f"failed to load runtime model: {exc}")
        return None

    profiles = normalize_active_profiles(active_profiles)
    try:
        clients = normalize_active_clients(model, active_clients)
    except RuntimeError as exc:
        log_runtime_event("pulse.scope_error", "clients", {
            "requested": active_clients or [],
            "error": str(exc),
        }, root_dir)
        log("error", f"invalid pulse client scope: {exc}")
        return None
    return filter_model(model, profiles, clients), profiles, clients


def _handle_pulse_config_change(
    root_dir: Path,
    state: PulseState,
    model: dict[str, Any],
    *,
    auto_sync: bool,
) -> None:
    new_hash = _model_config_hash(root_dir)
    if not state.config_hash or new_hash == state.config_hash:
        state.config_hash = new_hash
        return

    log_runtime_event("pulse.config_changed", "runtime", {
        "old_hash": state.config_hash,
        "new_hash": new_hash,
    })
    state.events_emitted += 1
    log("info", "config changed", old=state.config_hash, new=new_hash)
    if auto_sync:
        _pulse_auto_sync(model, state)
    state.config_hash = new_hash


def _pulse_auto_sync(model: dict[str, Any], state: PulseState) -> None:
    try:
        actions = sync_runtime(model, dry_run=False)
        log_runtime_event("pulse.auto_sync", "runtime", {"action_count": len(actions)})
        state.events_emitted += 1
        log("info", f"auto-sync completed ({len(actions)} actions)")
    except Exception as exc:
        log("error", f"auto-sync failed: {exc}")


def _service_can_autorestart(service: dict[str, Any] | None) -> bool:
    return bool(service and service_supports_lifecycle(service)[0])


def _service_needs_supervision(service: dict[str, Any] | None) -> bool:
    return bool(service and _service_should_ensure_running(service) and service_supports_lifecycle(service)[0])


def _track_unhealthy_http(
    state: PulseState,
    service_id: str,
    *,
    is_unhealthy_http: bool,
    now: float,
) -> None:
    if is_unhealthy_http:
        state.unhealthy_since.setdefault(service_id, now)
    else:
        state.unhealthy_since.pop(service_id, None)


def _pulse_first_service_state(
    model: dict[str, Any],
    state: PulseState,
    service: dict[str, Any] | None,
    service_id: str,
    current_state: str,
    *,
    auto_restart: bool,
    now: float,
) -> str:
    if (
        auto_restart
        and current_state in ("down", "declared")
        and _service_needs_supervision(service)
    ):
        log_runtime_event("pulse.service_down", service_id, {"state": current_state})
        state.events_emitted += 1
        restarted = _restart_with_backoff(
            model,
            state,
            service,
            service_id,
            now=now,
            reason="supervised_down",
        )
        if restarted:
            return "running"
    return current_state


def _pulse_service_transition(
    model: dict[str, Any],
    state: PulseState,
    service: dict[str, Any] | None,
    service_id: str,
    previous_state: str,
    current_state: str,
    *,
    auto_restart: bool,
    now: float,
) -> str:
    is_crash = previous_state in ("running", "starting") and current_state in ("down", "declared")
    event_type = "pulse.service_crashed" if is_crash else "pulse.service_state_changed"
    log_runtime_event(event_type, service_id, {"from": previous_state, "to": current_state})
    state.events_emitted += 1
    log("warn" if is_crash else "info", f"service {service_id}: {previous_state} -> {current_state}")
    if is_crash and auto_restart and _service_can_autorestart(service):
        restarted = _restart_with_backoff(
            model,
            state,
            service,
            service_id,
            now=now,
            reason="crashed",
        )
        if restarted:
            return "running"
    return current_state


def _pulse_supervised_down_state(
    model: dict[str, Any],
    state: PulseState,
    service: dict[str, Any] | None,
    service_id: str,
    current_state: str,
    *,
    auto_restart: bool,
    now: float,
) -> str:
    if current_state not in ("down", "declared") or not auto_restart or not _service_needs_supervision(service):
        return current_state
    restarted = _restart_with_backoff(
        model,
        state,
        service,
        service_id,
        now=now,
        reason="supervised_down",
    )
    return "running" if restarted else current_state


def _pulse_unhealthy_http_state(
    model: dict[str, Any],
    state: PulseState,
    service: dict[str, Any] | None,
    service_id: str,
    current_state: str,
    *,
    has_live_pid: bool,
    auto_restart: bool,
    unhealthy_grace_seconds: float,
    now: float,
) -> str:
    if current_state != "starting" or not has_live_pid or not auto_restart:
        return current_state
    unhealthy_started_at = state.unhealthy_since.get(service_id, now)
    unhealthy_for = now - unhealthy_started_at
    if unhealthy_for < unhealthy_grace_seconds or not _service_can_autorestart(service):
        return current_state
    log_runtime_event("pulse.service_unhealthy", service_id, {
        "state": current_state,
        "unhealthy_for_seconds": round(unhealthy_for, 1),
    })
    state.events_emitted += 1
    restarted = _restart_with_backoff(
        model,
        state,
        service,
        service_id,
        now=now,
        reason="unhealthy_http",
    )
    if restarted:
        state.unhealthy_since.pop(service_id, None)
        return "running"
    return current_state


def _reconcile_pulse_service(
    model: dict[str, Any],
    state: PulseState,
    services_by_id: dict[str, dict[str, Any]],
    service_id: str,
    probe: dict[str, Any],
    *,
    auto_restart: bool,
    unhealthy_grace_seconds: float,
    now: float,
) -> None:
    current_state = probe.get("state", "declared")
    previous_state = state.service_states.get(service_id)
    has_live_pid = probe.get("pid") is not None
    service = services_by_id.get(service_id)
    _track_unhealthy_http(
        state,
        service_id,
        is_unhealthy_http=current_state == "starting" and has_live_pid,
        now=now,
    )

    if previous_state is None:
        current_state = _pulse_first_service_state(
            model, state, service, service_id, current_state,
            auto_restart=auto_restart, now=now,
        )
        state.service_states[service_id] = current_state
        return
    if current_state != previous_state:
        current_state = _pulse_service_transition(
            model, state, service, service_id, previous_state, current_state,
            auto_restart=auto_restart, now=now,
        )
    current_state = _pulse_supervised_down_state(
        model, state, service, service_id, current_state,
        auto_restart=auto_restart, now=now,
    )
    current_state = _pulse_unhealthy_http_state(
        model, state, service, service_id, current_state,
        has_live_pid=has_live_pid,
        auto_restart=auto_restart,
        unhealthy_grace_seconds=unhealthy_grace_seconds,
        now=now,
    )
    if current_state == "running":
        state.restart_attempts.pop(service_id, None)
        state.restart_backoff.pop(service_id, None)
    state.service_states[service_id] = current_state


def _reconcile_pulse_services(
    model: dict[str, Any],
    state: PulseState,
    *,
    auto_restart: bool,
    unhealthy_grace_seconds: float,
    now: float,
) -> None:
    current_services = _snapshot_services(model)
    services_by_id = {s["id"]: s for s in model.get("services", [])}

    for service_id, probe in current_services.items():
        _reconcile_pulse_service(
            model,
            state,
            services_by_id,
            service_id,
            probe,
            auto_restart=auto_restart,
            unhealthy_grace_seconds=unhealthy_grace_seconds,
            now=now,
        )


def _reconcile_pulse_checks(model: dict[str, Any], state: PulseState) -> None:
    current_checks = _snapshot_checks(model)

    for check_id, ok in current_checks.items():
        previous_ok = state.check_states.get(check_id)
        if previous_ok is None:
            state.check_states[check_id] = ok
            continue
        if ok != previous_ok:
            event_type = "pulse.check_recovered" if ok else "pulse.check_failed"
            log_runtime_event(event_type, check_id, {"ok": ok})
            state.events_emitted += 1
            log(
                "info" if ok else "warn",
                f"check {check_id}: {'recovered' if ok else 'failed'}",
            )

        state.check_states[check_id] = ok


def _reconcile_pressure_advisory(root_dir: Path, state: PulseState) -> None:
    advisory = runtime_pressure_advisory(root_dir)
    warnings = [str(warning) for warning in advisory.get("warnings") or [] if str(warning).strip()]
    if warnings != state.pressure_warnings:
        log_runtime_event(
            "pulse.pressure_advisory",
            "pressure",
            {
                "warnings": warnings,
                "mutates": False,
                "safe_first_commands": advisory.get("safe_first_commands") or [],
            },
            root_dir,
        )
        state.events_emitted += 1
        if warnings:
            log("warn", "pressure/offload advisory changed", warnings=warnings)
        else:
            log("info", "pressure/offload advisory cleared")
    state.pressure_warnings = warnings
    state.pressure_advisory = advisory


def _write_pulse_state(
    root_dir: Path,
    state: PulseState,
    *,
    now: float,
    auto_restart: bool,
    auto_sync: bool,
    active_clients: set[str],
    active_profiles: set[str],
    unhealthy_grace_seconds: float,
) -> None:
    state_path = pulse_state_path(root_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _merge_port_guard_counters(state, _external_port_guard_counters(root_dir))
    snapshot = {
        "pid": os.getpid(),
        "updated_at": time.time(),
        "interval": getattr(reconcile_once, "_interval", DEFAULT_INTERVAL),
        "auto_restart": auto_restart,
        "auto_sync": auto_sync,
        "active_clients": sorted(active_clients),
        "active_profiles": sorted(active_profiles),
        "unhealthy_grace_seconds": unhealthy_grace_seconds,
    } | state.to_dict(now=now)
    try:
        # Serialize the pulse snapshot against focus writers and publish it via
        # an atomic fsync+rename so concurrent readers never observe a torn
        # file. pulse state is a full snapshot, so the mutate fn ignores the
        # current value. Best-effort: a stuck lock or write error must not crash
        # the daemon cycle (StateLockTimeout subclasses RuntimeError).
        locked_json_update(state_path, lambda _current: snapshot)
    except (StateLockTimeout, OSError):
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
    active_clients: list[str] | None = None,
    active_profiles: list[str] | None = None,
    unhealthy_grace_seconds: float = DEFAULT_UNHEALTHY_GRACE_SECONDS,
) -> int:
    """Run the pulse daemon until signalled to stop."""
    global _shutdown

    # Prevent double-start.
    running_pid = existing_pid(root_dir)
    if running_pid is not None:
        print(f"[pulse] already running (pid {running_pid})", file=sys.stderr)
        return 1

    _open_log(root_dir)
    write_pid(root_dir)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Stash interval so reconcile_once can include it in state snapshots.
    reconcile_once._interval = interval  # type: ignore[attr-defined]

    log_runtime_event("pulse.started", "daemon", {
        "pid": os.getpid(),
        "interval": interval,
        "auto_restart": auto_restart,
        "auto_sync": auto_sync,
        "active_clients": active_clients or [],
        "active_profiles": active_profiles or [],
        "unhealthy_grace_seconds": unhealthy_grace_seconds,
    }, root_dir)
    log("info", "started", pid=os.getpid(), interval=interval)

    state = load_pulse_state(root_dir)

    try:
        while not _shutdown:
            try:
                reconcile_once(
                    root_dir,
                    state,
                    auto_restart=auto_restart,
                    auto_sync=auto_sync,
                    active_clients=active_clients,
                    active_profiles=active_profiles,
                    unhealthy_grace_seconds=unhealthy_grace_seconds,
                )
            except Exception as exc:
                log("error", f"cycle failed: {exc}")

            # Sleep in small increments so we respond to signals promptly.
            deadline = time.monotonic() + interval
            while not _shutdown and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
    finally:
        log_runtime_event("pulse.stopped", "daemon", {
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
    state_path = next(
        (candidate for candidate in pulse_state_candidates(root_dir) if candidate.is_file()),
        pulse_state_path(root_dir),
    )
    running_pid = existing_pid(root_dir)

    if not state_path.is_file():
        _print_missing_pulse_state(running_pid)
        return 0

    try:
        snapshot = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"pulse: error reading state: {exc}")
        return 1
    if isinstance(snapshot, dict):
        _merge_port_guard_counters_into_snapshot(root_dir, snapshot)

    _print_pulse_snapshot(snapshot, running_pid)
    return 0


def _print_missing_pulse_state(running_pid: int | None) -> None:
    if running_pid:
        print(f"pulse: running (pid {running_pid}), no state file yet")
    else:
        print("pulse: not running (no state file)")


def _print_pulse_snapshot(snapshot: dict[str, Any], running_pid: int | None) -> None:
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

    _print_pulse_services(snapshot.get("service_states", {}))
    _print_pulse_checks(snapshot.get("check_states", {}))
    _print_port_sentinel(snapshot.get("port_sentinel", {}))
    _print_pulse_pressure(snapshot.get("pressure_warnings", []))


def _print_pulse_services(service_states: dict[str, Any]) -> None:
    if not service_states:
        return
    print("  services:")
    for sid, sstate in sorted(service_states.items()):
        marker = "+" if sstate == "running" else "-" if sstate == "down" else "~"
        print(f"    {marker} {sid}: {sstate}")


def _print_pulse_checks(check_states: dict[str, Any]) -> None:
    failed = [cid for cid, ok in check_states.items() if not ok]
    if failed:
        print(f"  failed checks: {', '.join(sorted(failed))}")
    elif check_states:
        print(f"  checks: all passing ({len(check_states)})")


def _print_pulse_pressure(pressure_warnings: list[Any]) -> None:
    warnings = [str(item) for item in pressure_warnings if str(item).strip()]
    if not warnings:
        return
    print("  pressure/offload warnings:")
    for warning in warnings:
        print(f"    ! {warning}")


def _print_port_sentinel(port_sentinel: dict[str, Any]) -> None:
    if not port_sentinel:
        return
    mode = port_sentinel.get("mode", DEFAULT_PORT_SENTINEL_MODE)
    seen = int(port_sentinel.get("rogues_seen") or 0)
    reaped = int(port_sentinel.get("rogues_reaped") or 0)
    active = int(port_sentinel.get("active_candidates") or 0)
    print(f"  port sentinel: {mode}, seen {seen}, reaped {reaped}, active {active}")
    hook_blocks = int(port_sentinel.get("hook_blocks") or 0)
    shim_blocks = int(port_sentinel.get("shim_blocks") or 0)
    post_bind = int(port_sentinel.get("post_bind_mismatches") or 0)
    wildcard = int(port_sentinel.get("wildcard_criticals") or 0)
    first_seen = str(port_sentinel.get("first_seen_at") or "never")
    last_seen = str(port_sentinel.get("last_seen_at") or "never")
    print(
        "  port guard counters: "
        f"hook {hook_blocks}, shim {shim_blocks}, post-bind {post_bind}, "
        f"wildcard {wildcard}, first {first_seen}, last {last_seen}"
    )
    for candidate in port_sentinel.get("last_candidates") or []:
        print(
            "    ! "
            f"pid {candidate.get('pid')} port {candidate.get('port')} "
            f"{candidate.get('signature')} {candidate.get('enforcement')}"
        )


def read_state(root_dir: Path) -> dict[str, Any]:
    """Read pulse state for programmatic consumers (MCP tool)."""
    state_path = next(
        (candidate for candidate in pulse_state_candidates(root_dir) if candidate.is_file()),
        pulse_state_path(root_dir),
    )
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
            _merge_port_guard_counters_into_snapshot(root_dir, result)
        except (json.JSONDecodeError, OSError):
            result["state_error"] = "failed to read state file"

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _split_scope_values(raw_value: str) -> list[str]:
    return [
        part.strip()
        for chunk in raw_value.split(",")
        for part in chunk.split()
        if part.strip()
    ]


def _scope_from_cli_or_env(
    cli_values: list[str] | None,
    env_name: str,
    env_values: dict[str, str] | None = None,
) -> list[str] | None:
    values = [value.strip() for value in cli_values or [] if value and value.strip()]
    if values:
        return values
    env_value = os.environ.get(env_name, "").strip()
    if not env_value and env_values is not None:
        env_value = str(env_values.get(env_name) or "").strip()
    if not env_value:
        return None
    return _split_scope_values(env_value)


def _float_from_cli_or_env(
    cli_value: float | None,
    env_name: str,
    default: float,
    env_values: dict[str, str] | None = None,
) -> float:
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(env_name, "").strip()
    if not env_value and env_values is not None:
        env_value = str(env_values.get(env_name) or "").strip()
    if not env_value:
        return default
    try:
        return float(env_value)
    except ValueError:
        log("warn", f"ignoring invalid {env_name}", value=env_value)
        return default

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
    run_parser.add_argument(
        "--client",
        action="append",
        default=None,
        help="Client overlay to supervise. Can be repeated. Defaults to SKILLBOX_PULSE_CLIENTS or the runtime default client.",
    )
    run_parser.add_argument(
        "--profile",
        action="append",
        default=None,
        help="Runtime profile to supervise. Can be repeated. Defaults to SKILLBOX_PULSE_PROFILES or core.",
    )
    run_parser.add_argument(
        "--unhealthy-grace-seconds",
        type=float,
        default=None,
        help=f"Seconds a live service may fail healthchecks before restart (default: {DEFAULT_UNHEALTHY_GRACE_SECONDS:g}).",
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
    env_values = load_runtime_env(root_dir)
    interval = args.interval if hasattr(args, "interval") and args.interval else None
    if interval is None:
        env_interval = os.environ.get("SKILLBOX_PULSE_INTERVAL", "").strip()
        if not env_interval:
            env_interval = str(env_values.get("SKILLBOX_PULSE_INTERVAL") or "").strip()
        interval = int(env_interval) if env_interval else DEFAULT_INTERVAL
    active_clients = _scope_from_cli_or_env(
        getattr(args, "client", None),
        "SKILLBOX_PULSE_CLIENTS",
        env_values,
    )
    active_profiles = _scope_from_cli_or_env(
        getattr(args, "profile", None),
        "SKILLBOX_PULSE_PROFILES",
        env_values,
    )
    unhealthy_grace_seconds = _float_from_cli_or_env(
        getattr(args, "unhealthy_grace_seconds", None),
        "SKILLBOX_PULSE_UNHEALTHY_GRACE_SECONDS",
        DEFAULT_UNHEALTHY_GRACE_SECONDS,
        env_values,
    )

    return run_daemon(
        root_dir,
        interval=interval,
        auto_restart=not getattr(args, "no_restart", False),
        auto_sync=getattr(args, "auto_sync", False),
        active_clients=active_clients,
        active_profiles=active_profiles,
        unhealthy_grace_seconds=unhealthy_grace_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
