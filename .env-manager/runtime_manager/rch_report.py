from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


_HOOK_NEGATIVE_RE = re.compile(r"\b(not[\s-]+installed|missing|disabled|inactive)\b")
_HOOK_POSITIVE_RE = re.compile(r"\b(installed|active|enabled)\b")


RCH_SAFE_PROBES = (
    ("robot_triage", ("--robot-triage", "--json")),
    ("status", ("status", "--workers", "--jobs", "--json")),
    ("check", ("check", "--json")),
    ("hook_status", ("hook", "status", "--json")),
)


def _json_or_none(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _run_rch_probe(binary: str, probe_id: str, args: tuple[str, ...], timeout_seconds: float) -> dict[str, Any]:
    command = [binary, *args]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "id": probe_id,
            "command": " ".join(command),
            "ok": False,
            "returncode": None,
            "error": str(exc),
            "json": None,
            "stdout": "",
            "stderr": "",
        }
    return {
        "id": probe_id,
        "command": " ".join(command),
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "json": _json_or_none(result.stdout),
        "stdout": result.stdout.strip()[-2000:],
        "stderr": result.stderr.strip()[-2000:],
    }


def _walk_values(value: Any) -> list[Any]:
    values = [value]
    if isinstance(value, dict):
        for nested in value.values():
            values.extend(_walk_values(nested))
    elif isinstance(value, list):
        for nested in value:
            values.extend(_walk_values(nested))
    return values


def _worker_lists(probes: list[dict[str, Any]]) -> list[list[Any]]:
    lists: list[list[Any]] = []
    for probe in probes:
        payload = probe.get("json")
        for value in _walk_values(payload):
            if isinstance(value, dict) and isinstance(value.get("workers"), list):
                lists.append(value["workers"])
    return lists


def _worker_is_ready(worker: Any) -> bool:
    if not isinstance(worker, dict):
        return False
    for key in ("healthy", "ready", "available", "online", "enabled"):
        if worker.get(key) is True:
            return True
    state = str(worker.get("state") or worker.get("status") or "").lower()
    return state in {"healthy", "ready", "available", "online", "idle"}


def _hook_summary(probes: list[dict[str, Any]]) -> dict[str, Any]:
    hook_probe = next((probe for probe in probes if probe.get("id") == "hook_status"), None)
    if hook_probe is None:
        return {"known": False, "installed": None}
    text = json.dumps(hook_probe.get("json"), sort_keys=True, default=str).lower()
    text += "\n" + str(hook_probe.get("stdout") or "").lower()
    if _HOOK_NEGATIVE_RE.search(text):
        installed: bool | None = False
    elif _HOOK_POSITIVE_RE.search(text):
        installed = True
    else:
        installed = None
    return {
        "known": hook_probe.get("ok") or installed is not None,
        "installed": installed,
        "probe_ok": hook_probe.get("ok"),
    }


def _binary_from_arg_env_or_path(binary: str | None) -> tuple[str | None, str | None]:
    if binary:
        path = str(Path(binary).expanduser())
        return path, "argument" if Path(path).exists() else "argument-missing"
    configured = os.environ.get("SKILLBOX_RCH_BIN", "").strip()
    if configured:
        path = str(Path(configured).expanduser())
        return path, "env" if Path(path).exists() else "env-missing"
    found = shutil.which("rch")
    return (found, "path") if found else (None, None)


def classify_rch_posture(binary_present: bool, probes: list[dict[str, Any]]) -> dict[str, Any]:
    if not binary_present:
        return {
            "state": "not-configured",
            "worker_state": "no-worker",
            "healthy_workers": 0,
            "total_workers": 0,
            "fail_open_expected": True,
            "hook": {"known": False, "installed": None},
        }

    workers = [worker for worker_list in _worker_lists(probes) for worker in worker_list]
    healthy_workers = [worker for worker in workers if _worker_is_ready(worker)]
    failed_probe_ids = [str(probe.get("id")) for probe in probes if not probe.get("ok")]
    combined_text = "\n".join(
        str(probe.get("stdout") or "") + "\n" + str(probe.get("stderr") or "")
        for probe in probes
    ).lower()

    if healthy_workers:
        state = "worker-ready"
        worker_state = "worker-ready"
    elif workers:
        state = "remediation"
        worker_state = "no-healthy-workers"
    elif "no worker" in combined_text or "no workers" in combined_text:
        state = "remediation"
        worker_state = "no-workers"
    elif failed_probe_ids:
        state = "remediation"
        worker_state = "unknown"
    else:
        state = "no-workers"
        worker_state = "no-workers"

    return {
        "state": state,
        "worker_state": worker_state,
        "healthy_workers": len(healthy_workers),
        "total_workers": len(workers),
        "failed_probe_ids": failed_probe_ids,
        "fail_open_expected": True,
        "hook": _hook_summary(probes),
    }


def collect_rch_report(
    root_dir: Path,
    *,
    binary: str | None = None,
    run_probes: bool = True,
    timeout_seconds: float = 5.0,
    target_box: str = "portfolio-devbox",
) -> dict[str, Any]:
    configured_binary, source = _binary_from_arg_env_or_path(binary)
    binary_present = bool(configured_binary and source not in {"argument-missing", "env-missing"})
    probes: list[dict[str, Any]] = []
    if binary_present and run_probes:
        probes = [
            _run_rch_probe(str(configured_binary), probe_id, args, timeout_seconds)
            for probe_id, args in RCH_SAFE_PROBES
        ]
    posture = classify_rch_posture(binary_present, probes)
    return {
        "ok": True,
        "mode": "read_only",
        "mutates": False,
        "root_dir": str(root_dir),
        "target_box": target_box,
        "binary": {
            "path": configured_binary,
            "present": binary_present,
            "source": source,
        },
        "approved_worker_scope": {
            "default_target": target_box,
            "excluded_targets": ["jeremy", "ssh-info", "sweet-potato-prod"],
            "remote_writes_allowed": False,
        },
        "global_hook_install": {
            "allowed": False,
            "reason": "Hook installation changes global agent behavior and needs a separate explicit approval step.",
            "status_probe": "rch hook status --json",
        },
        "safe_probe_commands": [
            "rch --robot-triage --json",
            "rch status --workers --jobs --json",
            "rch check --json",
            "rch hook status --json",
        ],
        "setup_plan": [
            {
                "id": "inspect",
                "command": "python3 .env-manager/manage.py rch-report --format json",
                "mutates": False,
            },
            {
                "id": "worker-probe",
                "command": "rch workers probe --all",
                "mutates": False,
            },
            {
                "id": "manual-offload",
                "command": "rch exec -- <build-or-test-command>",
                "mutates": False,
                "requires_ready_worker": True,
            },
            {
                "id": "hook-install",
                "command": "rch hook install",
                "mutates": True,
                "requires_explicit_approval": True,
            },
        ],
        "posture": posture,
        "probes": probes,
        "next_actions": _rch_next_actions(binary_present, posture, target_box),
    }


def _rch_next_actions(binary_present: bool, posture: dict[str, Any], target_box: str) -> list[str]:
    if not binary_present:
        return [
            "RCH is not installed here; keep build offload in planning mode.",
            f"Use only the approved non-production worker target: {target_box}.",
            "Do not run `rch hook install` without explicit approval.",
        ]
    if posture.get("worker_state") == "worker-ready":
        return [
            "Use `rch exec -- <build-or-test-command>` for expensive validation.",
            "Keep hook status read-only unless the operator explicitly approves hook installation.",
        ]
    return [
        "Run `rch --robot-triage --json` and remediate reported setup issues.",
        f"Configure/probe workers for approved target {target_box}; do not use excluded production boxes.",
        "RCH is fail-open, so unavailable remote execution should fall back to local commands.",
    ]


def rch_report_text_lines(payload: dict[str, Any]) -> list[str]:
    posture = payload.get("posture") or {}
    binary = payload.get("binary") or {}
    lines = [
        "rch report: read-only",
        f"binary: {'present' if binary.get('present') else 'missing'} {binary.get('path') or ''}".rstrip(),
        f"state: {posture.get('state')} workers={posture.get('healthy_workers', 0)}/{posture.get('total_workers', 0)}",
        f"hook install allowed: {payload.get('global_hook_install', {}).get('allowed')}",
        "next:",
    ]
    lines.extend(f"  - {action}" for action in payload.get("next_actions") or [])
    return lines
