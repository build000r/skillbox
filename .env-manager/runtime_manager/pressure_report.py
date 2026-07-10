from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .shared import is_elevated_pressure_level


GIB = 1024 ** 3
DEFAULT_TARGET_BOX = "worker-devbox"
EXCLUDED_BOX_IDS = ("prod", "production", "primary-prod")

PROTECTED_BUCKETS = (
    {
        "id": "codex-state",
        "path": "~/.codex",
        "reason": "Codex logs, sessions, memory, and local agent state.",
    },
    {
        "id": "claude-state",
        "path": "~/.claude",
        "reason": "Claude logs, hooks, sessions, skills, and local agent state.",
    },
    {
        "id": "ssh-material",
        "path": "~/.ssh",
        "reason": "SSH identities and host trust material.",
    },
)

REVIEW_ONLY_CANDIDATES = (
    {
        "id": "playwright-cache",
        "path": "~/Library/Caches/ms-playwright",
        "class": "tool-cache",
        "reason": "Browser automation cache; reinstallable, but cleanup still needs operator approval.",
    },
    {
        "id": "xcode-derived-data",
        "path": "~/Library/Developer/Xcode/DerivedData",
        "class": "build-cache",
        "reason": "Xcode build products and indexes.",
    },
    {
        "id": "core-simulator",
        "path": "~/Library/Developer/CoreSimulator",
        "class": "simulator-state",
        "reason": "iOS simulator devices, runtimes, and logs.",
    },
    {
        "id": "docker-desktop",
        "path": "~/Library/Containers/com.docker.docker",
        "class": "container-state",
        "reason": "Docker Desktop VM, images, volumes, and container state.",
    },
    {
        "id": "downloads",
        "path": "~/Downloads",
        "class": "operator-files",
        "reason": "Human-owned downloads; review before any cleanup.",
    },
    {
        "id": "messages-attachments",
        "path": "~/Library/Messages/Attachments",
        "class": "operator-files",
        "reason": "Personal message attachments; review only.",
    },
    {
        "id": "spotify-cache",
        "path": "~/Library/Caches/com.spotify.client",
        "class": "app-cache",
        "reason": "Application cache; may be reclaimable after review.",
    },
    {
        "id": "google-cache",
        "path": "~/Library/Caches/Google",
        "class": "app-cache",
        "reason": "Browser/application cache; may be reclaimable after review.",
    },
    {
        "id": "telegram-cache",
        "path": "~/Library/Caches/ru.keepcoder.Telegram",
        "class": "app-cache",
        "reason": "Application cache; may be reclaimable after review.",
    },
)


def _gib(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / GIB, 2)


def _expand_home(path: str, home: Path) -> Path:
    if path == "~":
        return home
    if path.startswith("~/"):
        return home / path[2:]
    return Path(path)


def _du_size_bytes(path: Path, timeout_seconds: float) -> tuple[int | None, str | None]:
    try:
        result = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()[-300:] or "du failed"
    first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    raw_kb = first.split(None, 1)[0] if first else ""
    try:
        return int(raw_kb) * 1024, None
    except ValueError:
        return None, f"could not parse du output: {first!r}"


def _bucket_entry(
    spec: dict[str, str],
    *,
    home: Path,
    policy: str,
    scan_sizes: bool,
    du_timeout_seconds: float,
) -> dict[str, Any]:
    path = _expand_home(spec["path"], home)
    present = path.exists()
    size_bytes: int | None = None
    size_error: str | None = None
    if present and scan_sizes:
        size_bytes, size_error = _du_size_bytes(path, du_timeout_seconds)
    return {
        "id": spec["id"],
        "display_path": spec["path"],
        "path": str(path),
        "present": present,
        "policy": policy,
        "class": spec.get("class"),
        "reason": spec["reason"],
        "size_bytes": size_bytes,
        "size_gib": _gib(size_bytes),
        "size_error": size_error,
    }


def _pressure_level(free_percent: float, free_bytes: int) -> str:
    if free_percent <= 5.0 or free_bytes <= 5 * GIB:
        return "critical"
    if free_percent <= 10.0 or free_bytes <= 10 * GIB:
        return "high"
    if free_percent <= 20.0 or free_bytes <= 20 * GIB:
        return "elevated"
    return "normal"


def _local_disk_report(home: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(home)
    total = int(usage.total)
    used = int(usage.used)
    free = int(usage.free)
    free_percent = round((free / total * 100.0), 2) if total else 0.0
    return {
        "path": str(home),
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "total_gib": _gib(total),
        "used_gib": _gib(used),
        "free_gib": _gib(free),
        "free_percent": free_percent,
        "pressure_level": _pressure_level(free_percent, free),
    }


def _load_boxes(root_dir: Path) -> list[dict[str, Any]]:
    path = root_dir / "workspace" / "boxes.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    boxes = payload.get("boxes")
    return boxes if isinstance(boxes, list) else []


def _box_report(root_dir: Path, target_box: str) -> dict[str, Any]:
    boxes = _load_boxes(root_dir)
    selected = next(
        (box for box in boxes if isinstance(box, dict) and str(box.get("id") or "") == target_box),
        None,
    )
    if selected is None:
        return {
            "target_box": target_box,
            "source": str(root_dir / "workspace" / "boxes.json"),
            "found": False,
            "live_free_known": False,
            "excluded_box_ids": list(EXCLUDED_BOX_IDS),
            "next_action": "python3 scripts/box.py list --format json",
        }

    volume_size_gb = selected.get("volume_size_gb")
    min_free_gb = selected.get("storage_min_free_gb")
    try:
        volume_size_bytes = int(float(volume_size_gb) * GIB) if volume_size_gb is not None else None
    except (TypeError, ValueError):
        volume_size_bytes = None
    try:
        min_free_bytes = int(float(min_free_gb) * GIB) if min_free_gb is not None else None
    except (TypeError, ValueError):
        min_free_bytes = None

    return {
        "target_box": target_box,
        "source": str(root_dir / "workspace" / "boxes.json"),
        "found": True,
        "state": selected.get("state"),
        "profile": selected.get("profile"),
        "tailscale_hostname": selected.get("tailscale_hostname"),
        "tailscale_ip": selected.get("tailscale_ip"),
        "state_root": selected.get("state_root"),
        "filesystem": selected.get("storage_filesystem"),
        "volume_name": selected.get("volume_name"),
        "volume_size_bytes": volume_size_bytes,
        "volume_size_gib": _gib(volume_size_bytes),
        "min_free_bytes": min_free_bytes,
        "min_free_gib": _gib(min_free_bytes),
        "live_free_bytes": None,
        "live_free_gib": None,
        "live_free_known": False,
        "live_probe_note": "Inventory only. Run the box readiness bead before remote install, deploy, or cleanup.",
        "safe_probe_command": f"python3 scripts/box.py status {target_box} --format json",
        "excluded_box_ids": list(EXCLUDED_BOX_IDS),
    }


def _which_from_env_or_path(env_name: str, names: tuple[str, ...]) -> tuple[str | None, str | None]:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        path = Path(configured).expanduser()
        return (str(path), "env") if path.exists() else (str(path), "env-missing")
    for name in names:
        found = shutil.which(name)
        if found:
            return found, "path"
    return None, None


def _process_running(process_names: tuple[str, ...]) -> dict[str, Any]:
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return {"known": False, "running": None, "method": "pgrep-missing", "process_names": list(process_names)}
    for name in process_names:
        try:
            result = subprocess.run(
                [pgrep, "-x", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"known": False, "running": None, "method": "pgrep", "error": str(exc), "process_names": list(process_names)}
        if result.returncode == 0:
            return {"known": True, "running": True, "method": "pgrep", "process_name": name}
    return {"known": True, "running": False, "method": "pgrep", "process_names": list(process_names)}


def _tool_report(
    *,
    id: str,
    env_name: str,
    binary_names: tuple[str, ...],
    daemon_process_names: tuple[str, ...],
    safe_probe_commands: list[str],
) -> dict[str, Any]:
    binary_path, source = _which_from_env_or_path(env_name, binary_names)
    installed = bool(binary_path and source != "env-missing")
    return {
        "id": id,
        "installed": installed,
        "binary_path": binary_path,
        "binary_source": source,
        "daemon": _process_running(daemon_process_names),
        "safe_probe_commands": safe_probe_commands,
        "missing_state": None if installed else f"{binary_names[0]} not found on PATH or {env_name}",
    }


def _tool_reports() -> dict[str, Any]:
    return {
        "rch": _tool_report(
            id="rch",
            env_name="SKILLBOX_RCH_BIN",
            binary_names=("rch",),
            daemon_process_names=("rchd",),
            safe_probe_commands=[
                "rch --robot-triage --json",
                "rch status --workers --jobs --json",
                "rch check --json",
                "rch hook status --json",
            ],
        ),
        "sbh": _tool_report(
            id="sbh",
            env_name="SKILLBOX_SBH_BIN",
            binary_names=("sbh",),
            daemon_process_names=("sbh", "sbhd", "sbh-daemon"),
            safe_probe_commands=[
                "sbh doctor --pal",
                "sbh status --json",
                "sbh stats --window 24h",
                "sbh blame --json",
                "sbh explain --id <decision-id>",
            ],
        ),
    }


def _next_actions(local_disk: dict[str, Any], tools: dict[str, Any], box: dict[str, Any]) -> list[str]:
    actions = [
        "Do not delete, truncate, or clean protected buckets without explicit operator approval.",
        "Use this report before expensive local build/test runs.",
    ]
    if is_elevated_pressure_level(local_disk["pressure_level"]):
        actions.append("Avoid local build storms; prefer a configured remote build lane for expensive validation.")
    if not tools["rch"]["installed"]:
        actions.append("RCH is not configured here yet; keep build offload in planning/probe mode.")
    else:
        actions.append("For expensive builds, prefer: rch exec -- <build-or-test-command>.")
    if not tools["sbh"]["installed"]:
        actions.append("SBH is not configured here yet; keep storage guard in observe-only planning.")
    else:
        actions.append("Keep SBH in observe/dry-run posture first: sbh status --json; sbh scan <path> --top 20.")
    if box.get("found"):
        actions.append(str(box.get("safe_probe_command")))
    else:
        actions.append("python3 scripts/box.py list --format json")
    return actions


def collect_pressure_report(
    root_dir: Path,
    *,
    home: Path | None = None,
    target_box: str = DEFAULT_TARGET_BOX,
    scan_candidate_sizes: bool = False,
    du_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    resolved_home = (home or Path.home()).expanduser().resolve()
    local_disk = _local_disk_report(resolved_home)
    protected = [
        _bucket_entry(
            spec,
            home=resolved_home,
            policy="protected_no_touch",
            scan_sizes=False,
            du_timeout_seconds=du_timeout_seconds,
        )
        for spec in PROTECTED_BUCKETS
    ]
    review_only = [
        _bucket_entry(
            spec,
            home=resolved_home,
            policy="review_only_never_auto_clean",
            scan_sizes=scan_candidate_sizes,
            du_timeout_seconds=du_timeout_seconds,
        )
        for spec in REVIEW_ONLY_CANDIDATES
    ]
    tools = _tool_reports()
    box = _box_report(root_dir, target_box)
    return {
        "ok": True,
        "mode": "read_only",
        "mutates": False,
        "root_dir": str(root_dir),
        "home": str(resolved_home),
        "target_policy": {
            "target_box": target_box,
            "excluded_box_ids": list(EXCLUDED_BOX_IDS),
            "remote_writes_allowed": False,
            "cleanup_allowed": False,
        },
        "local_disk": local_disk,
        "box": box,
        "protected_buckets": protected,
        "review_only_candidates": review_only,
        "tools": tools,
        "next_actions": _next_actions(local_disk, tools, box),
    }


def pressure_report_text_lines(payload: dict[str, Any]) -> list[str]:
    local_disk = payload.get("local_disk") or {}
    box = payload.get("box") or {}
    tools = payload.get("tools") or {}
    lines = [
        "pressure report: read-only",
        (
            f"local: {local_disk.get('path')} "
            f"free={local_disk.get('free_gib')}GiB/{local_disk.get('total_gib')}GiB "
            f"({local_disk.get('free_percent')}%, {local_disk.get('pressure_level')})"
        ),
    ]
    if box.get("found"):
        lines.append(
            f"box: {box.get('target_box')} state={box.get('state')} "
            f"volume={box.get('volume_size_gib')}GiB min_free={box.get('min_free_gib')}GiB "
            "live_free=not_probed"
        )
    else:
        lines.append(f"box: {box.get('target_box')} not found in inventory")
    for tool_id in ("rch", "sbh"):
        tool = tools.get(tool_id) or {}
        daemon = tool.get("daemon") or {}
        installed = "installed" if tool.get("installed") else "missing"
        running = daemon.get("running")
        running_text = "unknown" if running is None else ("running" if running else "not-running")
        lines.append(f"{tool_id}: {installed} daemon={running_text}")
    lines.append("protected:")
    for bucket in payload.get("protected_buckets") or []:
        present = "present" if bucket.get("present") else "missing"
        lines.append(f"  - {bucket.get('id')}: {bucket.get('display_path')} ({present}, no-touch)")
    lines.append("next:")
    for action in payload.get("next_actions") or []:
        lines.append(f"  - {action}")
    return lines
