#!/usr/bin/env bash
# guard-dev-port.sh - PreToolUse/PATH-shim guard for direct dev-server starts.

set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_PATH="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$SCRIPT_SOURCE")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd -P)"
REPO_ROOT="${SKILLBOX_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"

HOOK_INPUT=""
MODE="hook"
SHIM_BIN=""
if [[ "${1:-}" == "--shim" ]]; then
  MODE="shim"
  SHIM_BIN="${2:-}"
  shift 2 || true
else
  HOOK_INPUT="$(cat)"
fi

export SKILLBOX_DEV_GUARD_MODE="$MODE"
export SKILLBOX_DEV_GUARD_SHIM_BIN="$SHIM_BIN"
export SKILLBOX_DEV_GUARD_HOOK_INPUT="$HOOK_INPUT"
export SKILLBOX_DEV_GUARD_ARGV_JSON
SKILLBOX_DEV_GUARD_ARGV_JSON="$(
  python3 - "$@" <<'PY'
import json
import sys

print(json.dumps(sys.argv[1:]))
PY
)"

python3 - "$REPO_ROOT" <<'PY'
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEV_COMMANDS = {
    "npm": (("run", "dev"),),
    "pnpm": (("dev",), ("run", "dev")),
    "yarn": (("dev",), ("run", "dev")),
    "vite": ((),),
    "next": (("dev",),),
    "astro": (("dev",),),
}


def _repo_root() -> Path:
    return Path(sys.argv[1]).resolve()


def _json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return default


def _basename(token: str) -> str:
    return Path(token).name


def _shell_segments(command: str) -> list[list[str]]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in {"&&", "||", ";", "|"}:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    index = 0
    assignment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
    while index < len(tokens) and assignment.match(tokens[index]):
        index += 1
    return tokens[index:]


def _matches_suffix(tokens: list[str], suffix: tuple[str, ...]) -> bool:
    tool = _basename(tokens[0]) if tokens else ""
    if tool == "vite" and not suffix:
        return len(tokens) == 1 or str(tokens[1]).startswith("-")
    if not suffix:
        return True
    if len(tokens) < 1 + len(suffix):
        return False
    return tuple(tokens[1 : 1 + len(suffix)]) == suffix


def _segment_dev_signature(segment: list[str]) -> str | None:
    if segment:
        tool = _basename(segment[0])
        suffixes = DEV_COMMANDS.get(tool)
        if suffixes:
            for suffix in suffixes:
                if _matches_suffix(segment, suffix):
                    return " ".join((tool, *suffix)).strip()
    return None


def dev_signature(command: str) -> str | None:
    for raw_segment in _shell_segments(command):
        signature = _segment_dev_signature(_strip_env_assignments(raw_segment))
        if signature:
            return signature
    return None


def effective_command_cwd(command: str, cwd_text: str) -> str:
    cwd = Path(cwd_text).expanduser()
    for raw_segment in _shell_segments(command):
        segment = _strip_env_assignments(raw_segment)
        if not segment:
            continue
        if _segment_dev_signature(segment):
            break
        if segment[0] == "cd" and len(segment) >= 2:
            target = Path(segment[1]).expanduser()
            cwd = target if target.is_absolute() else cwd / target
    return str(cwd)


def _hook_command_and_cwd() -> tuple[str, str]:
    mode = os.environ.get("SKILLBOX_DEV_GUARD_MODE", "hook")
    if mode == "shim":
        bin_name = os.environ.get("SKILLBOX_DEV_GUARD_SHIM_BIN", "")
        argv = _json_loads(os.environ.get("SKILLBOX_DEV_GUARD_ARGV_JSON", "[]"), [])
        command = " ".join(shlex.quote(str(part)) for part in [bin_name, *argv] if str(part))
        return command, os.getcwd()

    payload = _json_loads(os.environ.get("SKILLBOX_DEV_GUARD_HOOK_INPUT", ""), {})
    if not isinstance(payload, dict) or str(payload.get("tool_name") or "") != "Bash":
        return "", os.getcwd()
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return "", os.getcwd()
    command = str(tool_input.get("command") or tool_input.get("cmd") or "")
    cwd = str(tool_input.get("cwd") or os.getcwd())
    return command, cwd


def _load_model(root: Path) -> dict[str, Any]:
    raw_model = os.environ.get("SKILLBOX_PORT_GUARD_MODEL_JSON", "").strip()
    if raw_model:
        parsed = json.loads(raw_model)
        return parsed if isinstance(parsed, dict) else {}
    model_file = os.environ.get("SKILLBOX_PORT_GUARD_MODEL_FILE", "").strip()
    if model_file:
        parsed = json.loads(Path(model_file).read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    sys.path.insert(0, str(root / ".env-manager"))
    sys.path.insert(0, str(root / "scripts"))
    from lib.runtime_model import build_runtime_model

    return build_runtime_model(root)


def _port_entries(model: dict[str, Any]) -> list[dict[str, Any]]:
    sys.path.insert(0, str(_repo_root() / ".env-manager"))
    sys.path.insert(0, str(_repo_root() / "scripts"))
    try:
        from runtime_manager.port_registry import build_port_registry

        return build_port_registry(model)
    except Exception:
        entries: list[dict[str, Any]] = []
        for service in model.get("services") or []:
            port = None
            healthcheck = service.get("healthcheck") or {}
            if isinstance(healthcheck, dict):
                if healthcheck.get("port") is not None:
                    try:
                        port = int(healthcheck["port"])
                    except (TypeError, ValueError):
                        port = None
                elif healthcheck.get("url"):
                    parsed = urlparse(str(healthcheck["url"]))
                    if parsed.port is not None:
                        port = int(parsed.port)
            if port is None:
                continue
            entries.append({
                "owner_id": str(service.get("id") or ""),
                "owner_kind": "service",
                "port": port,
            })
        return entries


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _service_repo_path(model: dict[str, Any], service: dict[str, Any]) -> Path | None:
    repos = {
        str(repo.get("id") or ""): repo
        for repo in model.get("repos") or []
        if str(repo.get("id") or "")
    }
    repo_id = str(service.get("repo_id") or service.get("repo") or "")
    repo = repos.get(repo_id)
    raw_path = ""
    if isinstance(repo, dict):
        raw_path = str(repo.get("host_path") or repo.get("path") or "")
    raw_path = raw_path or str(service.get("host_path") or "")
    if not raw_path:
        return None
    try:
        return Path(raw_path).expanduser().resolve()
    except OSError:
        return Path(raw_path).expanduser().absolute()


def _up_command(service: dict[str, Any]) -> str:
    parts = ["python3", ".env-manager/manage.py", "up"]
    client = str(service.get("client") or "").strip()
    profiles = [str(item).strip() for item in service.get("profiles") or [] if str(item).strip()]
    if client:
        parts.extend(["--client", client])
    if profiles:
        parts.extend(["--profile", profiles[0]])
    parts.extend(["--service", str(service.get("id") or "")])
    return " ".join(parts)


def _matching_service(model: dict[str, Any], cwd: Path) -> tuple[dict[str, Any], int] | None:
    ports_by_service: dict[str, list[int]] = {}
    for entry in _port_entries(model):
        if entry.get("owner_kind") == "service" and entry.get("port") is not None:
            ports_by_service.setdefault(str(entry.get("owner_id") or ""), []).append(int(entry["port"]))
    candidates: list[tuple[int, dict[str, Any], int]] = []
    for service in model.get("services") or []:
        service_id = str(service.get("id") or "")
        ports = ports_by_service.get(service_id) or []
        if not service_id or not ports:
            continue
        repo_path = _service_repo_path(model, service)
        if repo_path is None or not _is_relative_to(cwd, repo_path):
            continue
        candidates.append((len(str(repo_path)), service, sorted(ports)[0]))
    if not candidates:
        return None
    _, service, port = sorted(
        candidates,
        key=lambda item: (-item[0], str(item[1].get("id") or "")),
    )[0]
    return service, port


def _log(root: Path, verdict: str, detail: dict[str, Any]) -> None:
    try:
        log_path = root / "logs" / "runtime" / "runtime.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{stamp} port_guard {verdict} "
                f"{json.dumps(detail, separators=(',', ':'), sort_keys=True)}\n"
            )
    except OSError:
        pass


def main() -> int:
    root = _repo_root()
    command, cwd_text = _hook_command_and_cwd()
    if not command:
        return 0
    signature = dev_signature(command)
    if not signature:
        return 0
    cwd_text = effective_command_cwd(command, cwd_text)

    detail = {"cwd": cwd_text, "command": command, "signature": signature}
    if os.environ.get("SKILLBOX_MANAGED_RUN") == "1":
        _log(root, "bypass_managed_run", detail)
        return 0
    if os.environ.get("SKILLBOX_PORT_GUARD") == "off":
        _log(root, "bypass_operator", detail)
        return 0

    try:
        cwd = Path(cwd_text).expanduser().resolve()
        model = _load_model(root)
        match = _matching_service(model, cwd)
    except Exception as exc:
        detail["error"] = str(exc)
        _log(root, "block_error", detail)
        print("BLOCKED: dev-port guard could not evaluate this dev command", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        print("escape hatch: SKILLBOX_PORT_GUARD=off", file=sys.stderr)
        return 2
    if match is None:
        _log(root, "bypass_uncovered", detail)
        return 0

    service, port = match
    service_id = str(service.get("id") or "")
    up_command = _up_command(service)
    detail.update({"service": service_id, "port": port, "up_command": up_command})
    _log(root, "block", detail)
    print("BLOCKED: direct dev server command in managed repo", file=sys.stderr)
    print(f"service: {service_id}", file=sys.stderr)
    print(f"declared port: {port}", file=sys.stderr)
    print(f"use: {up_command}", file=sys.stderr)
    print(f"cwd: {cwd}", file=sys.stderr)
    print("escape hatch: SKILLBOX_PORT_GUARD=off", file=sys.stderr)
    return 2


raise SystemExit(main())
PY
