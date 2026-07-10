from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any


DEFAULT_ADAPTER_REMOTE_ROOT = "/srv/skillbox/rch-adapter"
DEFAULT_RCH_DEFAULT_REMOTE_ROOT = "/data/projects"
DEFAULT_STAGE_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    ".skillbox-state",
    "target",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    ".DS_Store",
}


def _safe_slug(value: str, *, fallback: str = "project") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return slug[:80] or fallback


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def _default_stage_id(source: Path) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{_safe_slug(source.name)}-{stamp}-{_short_hash(str(source))}"


def _resolve_existing_command(command: str) -> str:
    if "/" in command:
        return command
    return shutil.which(command) or command


def _default_real_ssh(root_dir: Path) -> str:
    managed = root_dir / ".skillbox-state" / "home" / ".local" / "bin" / "ssh"
    if managed.exists():
        return str(managed)
    return _resolve_existing_command("ssh")


def _default_real_rsync() -> str:
    if Path("/usr/bin/rsync").exists():
        return "/usr/bin/rsync"
    return _resolve_existing_command("rsync")


def _normalize_command(command_parts: list[str]) -> list[str]:
    parts = list(command_parts)
    if parts and parts[0] == "--":
        parts = parts[1:]
    return parts


def build_rch_stage_plan(
    root_dir: Path,
    *,
    source: Path | None = None,
    stage_root: Path | None = None,
    stage_id: str | None = None,
    command_parts: list[str] | None = None,
    target_box: str = "worker-devbox",
    remote_root: str = DEFAULT_ADAPTER_REMOTE_ROOT,
    rch_binary: str | None = None,
    real_ssh: str | None = None,
    real_rsync: str | None = None,
    rch_home: Path | None = None,
    xdg_state_home: Path | None = None,
) -> dict[str, Any]:
    source_root = (source or Path.cwd()).expanduser().resolve()
    resolved_stage_root = (
        stage_root.expanduser()
        if stage_root is not None
        else root_dir / ".skillbox-state" / "rch-adapter"
    ).resolve()
    resolved_stage_id = _safe_slug(stage_id, fallback="stage") if stage_id else _default_stage_id(source_root)
    run_root = resolved_stage_root / resolved_stage_id
    local_projects_root = run_root / "projects"
    local_alias_root = run_root / "dp"
    local_project_root = local_projects_root / _safe_slug(source_root.name)
    bin_dir = run_root / "bin"
    manifest_path = run_root / "manifest.json"
    remote_root_clean = "/" + str(remote_root).strip().strip("/")
    remote_run_root = f"{remote_root_clean}/{resolved_stage_id}"
    remote_projects_root = f"{remote_run_root}/projects"
    remote_alias_root = f"{remote_run_root}/dp"
    command = _normalize_command(command_parts or [])
    resolved_rch = str(Path(rch_binary).expanduser().resolve()) if rch_binary else (shutil.which("rch") or "rch")
    resolved_home = str(rch_home.expanduser().resolve()) if rch_home else None
    resolved_xdg = str(xdg_state_home.expanduser().resolve()) if xdg_state_home else None

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "RCH_CANONICAL_PROJECT_ROOT": str(local_projects_root),
        "RCH_ALIAS_PROJECT_ROOT": str(local_alias_root),
        "SKILLBOX_RCH_ADAPTER_REAL_SSH": real_ssh or _default_real_ssh(root_dir),
        "SKILLBOX_RCH_ADAPTER_REAL_RSYNC": real_rsync or _default_real_rsync(),
        "SKILLBOX_RCH_ADAPTER_REMOTE_CANONICAL": remote_projects_root,
        "SKILLBOX_RCH_ADAPTER_REMOTE_ALIAS": remote_alias_root,
        "SKILLBOX_RCH_ADAPTER_LOCAL_CANONICAL": str(local_projects_root),
        "SKILLBOX_RCH_ADAPTER_LOCAL_ALIAS": str(local_alias_root),
        "SKILLBOX_RCH_ADAPTER_RCH_REMOTE_ROOT": DEFAULT_RCH_DEFAULT_REMOTE_ROOT,
        "SKILLBOX_RCH_ADAPTER_ALLOW_DELETE": "0",
        "SKILLBOX_RCH_ADAPTER_STRIP_ZSTD": "1",
    }
    if resolved_home:
        env["HOME"] = resolved_home
    if resolved_xdg:
        env["XDG_STATE_HOME"] = resolved_xdg

    exec_command = [resolved_rch, "exec", "--", *command] if command else []
    return {
        "ok": True,
        "mode": "plan",
        "mutates": False,
        "deletes": False,
        "remote_writes": False,
        "root_dir": str(root_dir),
        "target_box": target_box,
        "source": str(source_root),
        "stage": {
            "id": resolved_stage_id,
            "root": str(run_root),
            "local_projects_root": str(local_projects_root),
            "local_alias_root": str(local_alias_root),
            "local_project_root": str(local_project_root),
            "bin_dir": str(bin_dir),
            "manifest_path": str(manifest_path),
        },
        "remote": {
            "root": remote_run_root,
            "projects_root": remote_projects_root,
            "alias_root": remote_alias_root,
            "default_rch_root_translated": DEFAULT_RCH_DEFAULT_REMOTE_ROOT,
        },
        "translations": [
            {"from": str(local_projects_root), "to": remote_projects_root},
            {"from": str(local_alias_root), "to": remote_alias_root},
            {"from": DEFAULT_RCH_DEFAULT_REMOTE_ROOT, "to": remote_projects_root},
        ],
        "wrappers": {
            "ssh": str(bin_dir / "ssh"),
            "rsync": str(bin_dir / "rsync"),
            "real_ssh": env["SKILLBOX_RCH_ADAPTER_REAL_SSH"],
            "real_rsync": env["SKILLBOX_RCH_ADAPTER_REAL_RSYNC"],
            "delete_stripped_by_default": True,
            "zstd_choice_stripped_by_default": True,
        },
        "env": env,
        "command": {
            "argv": command,
            "exec_argv": exec_command,
            "cwd": str(local_project_root),
        },
        "actions": [
            "create local staging directories",
            "copy source tree into staging without deleting stale files",
            "write ssh/rsync path-translation wrappers",
            "write manifest with manual cleanup commands",
        ],
        "manual_cleanup_commands": [
            f"rm -rf {run_root}",
            f"ssh {target_box} 'rm -rf {remote_run_root}'",
        ],
        "policy": {
            "default_is_plan_only": True,
            "deletes_by_default": False,
            "remote_delete_flags_stripped": True,
            "zstd_choice_stripped": True,
            "approved_target": target_box,
            "excluded_targets": ["prod", "production", "primary-prod"],
            "protected_local_paths": ["~/.ssh", "~/.codex", "~/.claude"],
        },
    }


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in DEFAULT_STAGE_EXCLUDES}


def copy_source_to_stage(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_dir():
        raise RuntimeError(f"source is not a directory: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    before_files = set(destination.rglob("*")) if destination.exists() else set()
    shutil.copytree(source, destination, ignore=_copy_ignore, symlinks=True, dirs_exist_ok=True)
    after_files = set(destination.rglob("*")) if destination.exists() else set()
    copied = [path for path in after_files if path.is_file()]
    return {
        "source": str(source),
        "destination": str(destination),
        "files_present": len(copied),
        "preexisting_entries": len(before_files),
        "deleted_entries": 0,
        "excluded_names": sorted(DEFAULT_STAGE_EXCLUDES),
    }


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _ssh_wrapper_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

real_ssh="${SKILLBOX_RCH_ADAPTER_REAL_SSH:-/usr/bin/ssh}"
local_canonical="${SKILLBOX_RCH_ADAPTER_LOCAL_CANONICAL:?missing local canonical root}"
local_alias="${SKILLBOX_RCH_ADAPTER_LOCAL_ALIAS:?missing local alias root}"
remote_canonical="${SKILLBOX_RCH_ADAPTER_REMOTE_CANONICAL:?missing remote canonical root}"
remote_alias="${SKILLBOX_RCH_ADAPTER_REMOTE_ALIAS:?missing remote alias root}"
rch_remote_root="${SKILLBOX_RCH_ADAPTER_RCH_REMOTE_ROOT:-/data/projects}"

rewrite_arg() {
  local value="$1"
  local needle='if [ -f "$required" ]; then'
  if [[ "$value" == *"RCH_DEP_PRESENT:%s"* && "$value" == *"RCH_DEP_MISSING:%s"* && "$value" == *"$needle"* ]]; then
    local replacement
    replacement='actual="$required"; case "$actual" in '"$rch_remote_root"'/*) actual="'"$remote_canonical"'/${actual#'"$rch_remote_root"'/}" ;; '"$rch_remote_root"') actual="'"$remote_canonical"'" ;; esac; if [ -f "$actual" ]; then'
    value="${value%%"$needle"*}$replacement${value#*"$needle"}"
    printf '%s' "$value"
    return
  fi
  value="${value//$local_canonical/$remote_canonical}"
  value="${value//$local_alias/$remote_alias}"
  value="${value//$rch_remote_root/$remote_canonical}"
  printf '%s' "$value"
}

rewrite_stdin() {
  perl -pe 'BEGIN {
    $local_canonical = $ENV{"SKILLBOX_RCH_ADAPTER_LOCAL_CANONICAL"} // "";
    $local_alias = $ENV{"SKILLBOX_RCH_ADAPTER_LOCAL_ALIAS"} // "";
    $remote_canonical = $ENV{"SKILLBOX_RCH_ADAPTER_REMOTE_CANONICAL"} // "";
    $remote_alias = $ENV{"SKILLBOX_RCH_ADAPTER_REMOTE_ALIAS"} // "";
    $rch_remote_root = $ENV{"SKILLBOX_RCH_ADAPTER_RCH_REMOTE_ROOT"} || "/data/projects";
  }
  s/\\Q$local_canonical\\E/$remote_canonical/g if length($local_canonical);
  s/\\Q$local_alias\\E/$remote_alias/g if length($local_alias);
  s/\\Q$rch_remote_root\\E/$remote_canonical/g if length($rch_remote_root);'
}

args=("$@")
dest_index=-1
skip_next=0
for i in "${!args[@]}"; do
  arg="${args[$i]}"
  if (( skip_next )); then
    skip_next=0
    continue
  fi
  case "$arg" in
    --)
      if (( i + 1 < ${#args[@]} )); then dest_index=$((i + 1)); fi
      break
      ;;
    -B|-b|-c|-D|-E|-e|-F|-I|-i|-J|-L|-l|-m|-O|-o|-p|-Q|-R|-S|-W|-w)
      skip_next=1
      ;;
    -*)
      ;;
    *)
      dest_index=$i
      break
      ;;
  esac
done

if (( dest_index >= 0 )); then
  for ((i=dest_index+1; i<${#args[@]}; i++)); do
    args[$i]="$(rewrite_arg "${args[$i]}")"
  done
fi

if (( dest_index >= 0 && dest_index + 2 < ${#args[@]} )) && [[ "${args[$((dest_index+1))]}" == "sh" && "${args[$((dest_index+2))]}" == "-s" ]]; then
  rewrite_stdin | exec "$real_ssh" "${args[@]}"
fi

exec "$real_ssh" "${args[@]}"
"""


def _rsync_wrapper_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

real_rsync="${SKILLBOX_RCH_ADAPTER_REAL_RSYNC:-/usr/bin/rsync}"
local_canonical="${SKILLBOX_RCH_ADAPTER_LOCAL_CANONICAL:?missing local canonical root}"
local_alias="${SKILLBOX_RCH_ADAPTER_LOCAL_ALIAS:?missing local alias root}"
remote_canonical="${SKILLBOX_RCH_ADAPTER_REMOTE_CANONICAL:?missing remote canonical root}"
remote_alias="${SKILLBOX_RCH_ADAPTER_REMOTE_ALIAS:?missing remote alias root}"
rch_remote_root="${SKILLBOX_RCH_ADAPTER_RCH_REMOTE_ROOT:-/data/projects}"
allow_delete="${SKILLBOX_RCH_ADAPTER_ALLOW_DELETE:-0}"
strip_zstd="${SKILLBOX_RCH_ADAPTER_STRIP_ZSTD:-1}"

rewrite_value() {
  local value="$1"
  value="${value//$local_canonical/$remote_canonical}"
  value="${value//$local_alias/$remote_alias}"
  value="${value//$rch_remote_root/$remote_canonical}"
  printf '%s' "$value"
}

args=()
rewrite_next=0
for arg in "$@"; do
  if [[ "$allow_delete" != "1" && ( "$arg" == "--delete" || "$arg" == --delete-* ) ]]; then
    continue
  fi
  if [[ "$strip_zstd" == "1" && "$arg" == --compress-choice=* ]]; then
    continue
  fi
  if (( rewrite_next )); then
    args+=("$(rewrite_value "$arg")")
    rewrite_next=0
    continue
  fi
  case "$arg" in
    --rsync-path)
      args+=("$arg")
      rewrite_next=1
      ;;
    --rsync-path=*)
      args+=("--rsync-path=$(rewrite_value "${arg#--rsync-path=}")")
      ;;
    *:/*)
      prefix="${arg%%:*}:"
      path_part="${arg#*:}"
      args+=("${prefix}$(rewrite_value "$path_part")")
      ;;
    *)
      args+=("$arg")
      ;;
  esac
done

exec "$real_rsync" "${args[@]}"
"""


def write_adapter_wrappers(plan: dict[str, Any]) -> dict[str, Any]:
    ssh_path = Path(plan["wrappers"]["ssh"])
    rsync_path = Path(plan["wrappers"]["rsync"])
    _write_executable(ssh_path, _ssh_wrapper_script())
    _write_executable(rsync_path, _rsync_wrapper_script())
    return {
        "ssh": str(ssh_path),
        "rsync": str(rsync_path),
        "deleted_entries": 0,
    }


def write_stage_manifest(plan: dict[str, Any], *, result: dict[str, Any] | None = None) -> Path:
    manifest_path = Path(plan["stage"]["manifest_path"])
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(plan)
    payload["result"] = result
    payload["written_at_unix"] = int(time.time())
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def prepare_rch_stage(plan: dict[str, Any], *, copy_source: bool = True, write_manifest: bool = True) -> dict[str, Any]:
    stage_root = Path(plan["stage"]["root"])
    local_projects_root = Path(plan["stage"]["local_projects_root"])
    local_alias_root = Path(plan["stage"]["local_alias_root"])
    stage_root.mkdir(parents=True, exist_ok=True)
    local_projects_root.mkdir(parents=True, exist_ok=True)
    if local_alias_root.is_symlink():
        try:
            current_target = os.readlink(local_alias_root)
        except OSError:
            current_target = ""
        if Path(current_target) != local_projects_root:
            local_alias_root.unlink()
            local_alias_root.symlink_to(local_projects_root, target_is_directory=True)
    elif local_alias_root.exists():
        raise RuntimeError(
            f"alias path {local_alias_root} exists and is not a symlink; "
            "remove it manually or pick a different --stage-id"
        )
    else:
        local_alias_root.symlink_to(local_projects_root, target_is_directory=True)
    wrappers = write_adapter_wrappers(plan)
    copy_result = None
    if copy_source:
        copy_result = copy_source_to_stage(Path(plan["source"]), Path(plan["stage"]["local_project_root"]))
    result = {
        "prepared": True,
        "mutates": True,
        "deletes": False,
        "wrappers": wrappers,
        "copy": copy_result,
    }
    if write_manifest:
        result["manifest_path"] = str(write_stage_manifest(plan, result=result))
    return result


def execute_rch_stage(
    plan: dict[str, Any],
    *,
    timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    exec_argv = plan["command"]["exec_argv"]
    if not exec_argv:
        raise RuntimeError("no command supplied for rch-stage --run")
    env = os.environ.copy()
    for key, value in plan["env"].items():
        env[key] = str(value)
    started = time.time()
    completed = subprocess.run(
        exec_argv,
        cwd=plan["command"]["cwd"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    return {
        "command": exec_argv,
        "cwd": plan["command"]["cwd"],
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "duration_ms": int((time.time() - started) * 1000),
        "stdout": completed.stdout[-8000:],
        "stderr": completed.stderr[-8000:],
    }


def rch_stage_text_lines(payload: dict[str, Any]) -> list[str]:
    stage = payload.get("stage") or {}
    remote = payload.get("remote") or {}
    command = payload.get("command") or {}
    lines = [
        f"RCH stage mode: {payload.get('mode')}",
        f"source: {payload.get('source')}",
        f"local stage: {stage.get('local_project_root')}",
        f"remote stage: {remote.get('projects_root')}",
        f"deletes by default: {payload.get('deletes')}",
        "env:",
        f"  RCH_CANONICAL_PROJECT_ROOT={payload.get('env', {}).get('RCH_CANONICAL_PROJECT_ROOT')}",
        f"  RCH_ALIAS_PROJECT_ROOT={payload.get('env', {}).get('RCH_ALIAS_PROJECT_ROOT')}",
        f"command: {' '.join(command.get('exec_argv') or [])}",
        "manual cleanup commands:",
    ]
    lines.extend(f"  {cmd}" for cmd in payload.get("manual_cleanup_commands", []))
    result = payload.get("result")
    if isinstance(result, dict):
        lines.append(f"result ok: {result.get('ok')}")
        if result.get("returncode") is not None:
            lines.append(f"returncode: {result.get('returncode')}")
    return lines
