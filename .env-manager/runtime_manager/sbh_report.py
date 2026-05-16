from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .pressure_report import PROTECTED_BUCKETS, REVIEW_ONLY_CANDIDATES


SBH_SAFE_PROBES = (
    ("doctor_pal", ("doctor", "--pal")),
    ("status", ("status", "--json")),
    ("stats", ("stats", "--window", "24h")),
    ("blame", ("blame", "--json")),
)
SBH_RELEASE_CAVEATS = [
    {
        "id": "sbh-v0.4.23-linux-x86_64-asset-mismatch",
        "status": "known_bad",
        "observed_at": "2026-05-14",
        "version": "v0.4.23",
        "repository": "Dicklesworthstone/storage_ballast_helper",
        "affected_asset": "sbh-v0.4.23-x86_64-unknown-linux-gnu.tar.xz",
        "observed_file_type": "Mach-O 64-bit executable arm64",
        "expected_file_type": "ELF 64-bit LSB executable, x86-64",
        "policy": "do_not_promote_latest_linux_asset",
        "safe_fallback": "Use the verified v0.4.22 Linux x86_64 canary pin until the upstream asset is republished.",
    }
]


def _json_or_none(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _run_sbh_probe(binary: str, probe_id: str, args: tuple[str, ...], timeout_seconds: float) -> dict[str, Any]:
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


def _text_for_probes(probes: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for probe in probes:
        chunks.append(json.dumps(probe.get("json"), sort_keys=True, default=str))
        chunks.append(str(probe.get("stdout") or ""))
        chunks.append(str(probe.get("stderr") or ""))
    return "\n".join(chunks).lower()


def _probe_named(probes: list[dict[str, Any]], probe_id: str) -> dict[str, Any] | None:
    return next((probe for probe in probes if probe.get("id") == probe_id), None)


_DOCTOR_FAILURE_WORD_RE = re.compile(
    r"(?<!no )(?<!zero )(?<!0 )\b(fail(?:ed|ure|ures)?|broken)\b",
    re.IGNORECASE,
)
_DAEMON_NEGATIVE_RE = re.compile(
    r"\b(not[\s-]+running|no[\s-]+daemon|stopped|failed|broken|dead)\b", re.IGNORECASE
)
_DAEMON_POSITIVE_RE = re.compile(r"\b(running|healthy|ready)\b", re.IGNORECASE)


def _doctor_has_failure(probes: list[dict[str, Any]]) -> bool:
    doctor = _probe_named(probes, "doctor_pal")
    if doctor is None:
        return False
    if doctor.get("ok") is False:
        return True
    payload = doctor.get("json")
    for value in _walk_values(payload):
        if isinstance(value, dict):
            state = str(value.get("state") or value.get("status") or value.get("level") or "").lower()
            if state in {"fail", "failed", "broken", "error", "critical"}:
                return True
        elif isinstance(value, str) and value.lower() in {"fail", "failed", "broken", "critical"}:
            return True
    text = str(doctor.get("stdout") or "") + "\n" + str(doctor.get("stderr") or "")
    return bool(_DOCTOR_FAILURE_WORD_RE.search(text))


def _daemon_running_from_status(probes: list[dict[str, Any]]) -> bool | None:
    status = _probe_named(probes, "status")
    if status is None:
        return None
    payload = status.get("json")
    for value in _walk_values(payload):
        if isinstance(value, dict):
            for key in ("daemon_running", "running", "healthy", "ok"):
                if value.get(key) is True:
                    return True
                if value.get(key) is False:
                    return False
            state = str(value.get("state") or value.get("status") or value.get("health") or "").lower()
            if state in {"running", "healthy", "ok", "ready"}:
                return True
            if state in {"stopped", "not-running", "dead", "failed", "broken"}:
                return False
    if status.get("ok") is False:
        return False
    text = str(status.get("stdout") or "") + "\n" + str(status.get("stderr") or "")
    if _DAEMON_NEGATIVE_RE.search(text):
        return False
    if _DAEMON_POSITIVE_RE.search(text):
        return True
    return None


def _activity_seen(probes: list[dict[str, Any]]) -> bool:
    for probe_id in ("stats", "blame"):
        probe = _probe_named(probes, probe_id)
        if probe is None or probe.get("ok") is not True:
            continue
        payload = probe.get("json")
        for value in _walk_values(payload):
            if isinstance(value, dict):
                for key in ("events", "decisions", "deletions", "candidates", "processes", "agents"):
                    raw = value.get(key)
                    if isinstance(raw, list) and raw:
                        return True
                    if isinstance(raw, (int, float)) and raw > 0:
                        return True
        if str(probe.get("stdout") or "").strip():
            return True
    return False


def _protected_path_policies(home: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for spec in PROTECTED_BUCKETS:
        display_path = spec["path"]
        path = home if display_path == "~" else home / display_path[2:] if display_path.startswith("~/") else Path(display_path)
        entries.append(
            {
                "id": spec["id"],
                "display_path": display_path,
                "path": str(path),
                "policy": "hard_veto",
                "reason": spec["reason"],
            }
        )
    return entries


def _review_path_policies(home: Path) -> list[dict[str, str | None]]:
    entries: list[dict[str, str | None]] = []
    for spec in REVIEW_ONLY_CANDIDATES:
        display_path = spec["path"]
        path = home if display_path == "~" else home / display_path[2:] if display_path.startswith("~/") else Path(display_path)
        entries.append(
            {
                "id": spec["id"],
                "display_path": display_path,
                "path": str(path),
                "policy": "review_only",
                "class": spec.get("class"),
                "reason": spec["reason"],
            }
        )
    return entries


def protected_path_veto(path: str | Path, protected_paths: list[dict[str, Any]]) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    try:
        resolved_candidate = candidate.resolve(strict=False)
    except OSError:
        resolved_candidate = candidate.absolute()

    for entry in protected_paths:
        protected = Path(str(entry.get("path") or "")).expanduser()
        try:
            resolved_protected = protected.resolve(strict=False)
        except OSError:
            resolved_protected = protected.absolute()
        if resolved_candidate == resolved_protected or resolved_protected in resolved_candidate.parents:
            return {
                "allowed": False,
                "matched": entry.get("id"),
                "policy": entry.get("policy"),
                "reason": entry.get("reason"),
            }
    return {"allowed": True, "matched": None, "policy": None, "reason": None}


def _binary_from_arg_env_or_path(binary: str | None) -> tuple[str | None, str | None]:
    if binary:
        path = str(Path(binary).expanduser())
        return path, "argument" if Path(path).exists() else "argument-missing"
    configured = os.environ.get("SKILLBOX_SBH_BIN", "").strip()
    if configured:
        path = str(Path(configured).expanduser())
        return path, "env" if Path(path).exists() else "env-missing"
    found = shutil.which("sbh")
    return (found, "path") if found else (None, None)


def classify_sbh_posture(binary_present: bool, probes: list[dict[str, Any]]) -> dict[str, Any]:
    if not binary_present:
        return {
            "state": "not-configured",
            "daemon_state": "missing-daemon",
            "observer_state": "inactive",
            "failed_probe_ids": [],
            "observe_first": True,
        }

    failed_probe_ids = [str(probe.get("id")) for probe in probes if not probe.get("ok")]
    daemon_running = _daemon_running_from_status(probes)
    text = _text_for_probes(probes)

    if _doctor_has_failure(probes):
        state = "remediation"
    elif daemon_running is True:
        state = "observer-ready"
    elif daemon_running is False or "not running" in text or "no daemon" in text:
        state = "remediation"
    elif failed_probe_ids:
        state = "remediation"
    else:
        state = "unknown"

    if daemon_running is True:
        daemon_state = "running"
    elif daemon_running is False:
        daemon_state = "not-running"
    else:
        daemon_state = "unknown"

    return {
        "state": state,
        "daemon_state": daemon_state,
        "observer_state": "observing" if state == "observer-ready" else "inactive",
        "failed_probe_ids": failed_probe_ids,
        "activity_seen": _activity_seen(probes),
        "observe_first": True,
    }


def _sbh_next_actions(
    binary_present: bool,
    posture: dict[str, Any],
    release_caveats: list[dict[str, Any]] | None = None,
) -> list[str]:
    if not binary_present:
        actions = [
            "SBH is not installed here; keep storage guard in planning mode.",
            "Do not run `sbh install`, `sbh clean`, or ballast mutation commands without explicit approval.",
            "Use `python3 .env-manager/manage.py pressure-report --format json` for the current read-only disk view.",
        ]
    elif posture.get("state") == "observer-ready":
        actions = [
            "Use `sbh status --json`, `sbh stats --window 24h`, and `sbh blame --json` for observation.",
            "Keep cleanup and ballast actions approval-gated until a separate enforce/canary issue exists.",
        ]
    else:
        actions = [
            "Run `sbh doctor --pal` and inspect the reported platform/service blockers.",
            "Use `sbh protect --list` and the Skillbox protected path policy before any cleanup planning.",
            "Keep SBH in observe-first mode; do not run `sbh clean` or ballast release/provision commands without approval.",
        ]

    for caveat in release_caveats or []:
        if caveat.get("status") == "known_bad":
            actions.append(
                f"Do not promote SBH {caveat.get('version')} Linux assets here: "
                f"{caveat.get('affected_asset')} was observed as {caveat.get('observed_file_type')}."
            )
    return actions


def collect_sbh_report(
    root_dir: Path,
    *,
    home: Path | None = None,
    binary: str | None = None,
    run_probes: bool = True,
    timeout_seconds: float = 5.0,
    decision_id: str | None = None,
) -> dict[str, Any]:
    resolved_home = (home or Path.home()).expanduser().resolve()
    protected_paths = _protected_path_policies(resolved_home)
    review_only_paths = _review_path_policies(resolved_home)
    configured_binary, source = _binary_from_arg_env_or_path(binary)
    binary_present = bool(configured_binary and source not in {"argument-missing", "env-missing"})
    probes: list[dict[str, Any]] = []
    if binary_present and run_probes:
        probes = [
            _run_sbh_probe(str(configured_binary), probe_id, args, timeout_seconds)
            for probe_id, args in SBH_SAFE_PROBES
        ]
        if decision_id:
            probes.append(_run_sbh_probe(str(configured_binary), "explain", ("explain", "--id", decision_id), timeout_seconds))

    posture = classify_sbh_posture(binary_present, probes)
    return {
        "ok": True,
        "mode": "read_only",
        "mutates": False,
        "root_dir": str(root_dir),
        "home": str(resolved_home),
        "binary": {
            "path": configured_binary,
            "present": binary_present,
            "source": source,
        },
        "policy": {
            "rollout_mode": "observe_first",
            "auto_delete_allowed": False,
            "clean_allowed": False,
            "ballast_mutation_allowed": False,
            "service_install_allowed": False,
            "protect_marker_write_allowed": False,
            "requires_explicit_approval_for_mutation": True,
            "release_asset_promotion_allowed": False,
            "release_asset_promotion_requires_file_check": True,
            "linux_x86_64_canary_pin": "v0.4.22",
        },
        "release_caveats": SBH_RELEASE_CAVEATS,
        "protected_paths": protected_paths,
        "review_only_paths": review_only_paths,
        "safe_probe_commands": [
            "sbh doctor --pal",
            "sbh status --json",
            "sbh stats --window 24h",
            "sbh blame --json",
            "sbh explain --id <decision-id>",
        ],
        "blocked_mutation_commands": [
            "sbh install --auto",
            "sbh clean --target-free <gib>",
            "sbh ballast provision",
            "sbh ballast release <gib>",
            "sbh protect <path>",
            "sbh uninstall",
        ],
        "posture": posture,
        "probes": probes,
        "next_actions": _sbh_next_actions(binary_present, posture, SBH_RELEASE_CAVEATS),
    }


def sbh_report_text_lines(payload: dict[str, Any]) -> list[str]:
    binary = payload.get("binary") or {}
    posture = payload.get("posture") or {}
    policy = payload.get("policy") or {}
    lines = [
        "sbh report: read-only",
        f"binary: {'present' if binary.get('present') else 'missing'} {binary.get('path') or ''}".rstrip(),
        f"state: {posture.get('state')} daemon={posture.get('daemon_state')}",
        f"auto delete allowed: {policy.get('auto_delete_allowed')}",
        f"ballast mutation allowed: {policy.get('ballast_mutation_allowed')}",
        f"release asset promotion allowed: {policy.get('release_asset_promotion_allowed')}",
        "protected:",
    ]
    for entry in payload.get("protected_paths") or []:
        lines.append(f"  - {entry.get('id')}: {entry.get('display_path')} ({entry.get('policy')})")
    caveats = payload.get("release_caveats") or []
    if caveats:
        lines.append("release caveats:")
        for caveat in caveats:
            lines.append(
                f"  - {caveat.get('version')} {caveat.get('affected_asset')}: "
                f"{caveat.get('status')} ({caveat.get('observed_file_type')})"
            )
    lines.append("next:")
    lines.extend(f"  - {action}" for action in payload.get("next_actions") or [])
    return lines
