from __future__ import annotations

# Generated mechanically from runtime_manager/shared.py; keep logic changes out of this split.
# ruff: noqa: F401
import argparse as argparse
import copy
import datetime
import fcntl
import hashlib
import json
import os
import re
import selectors as selectors
import shlex
import signal as signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

PACKAGE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PACKAGE_DIR.parent
DEFAULT_ROOT_DIR = SCRIPT_DIR.parent.resolve()
SCRIPTS_DIR = DEFAULT_ROOT_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from runtime_manager.errors import DEPRECATION_MARKER  # noqa: E402
except ImportError:  # loaded standalone without a package
    if str(PACKAGE_DIR) not in sys.path:
        sys.path.insert(0, str(PACKAGE_DIR))
    from errors import DEPRECATION_MARKER  # type: ignore[no-redef]  # noqa: E402

from lib.runtime_model import (  # noqa: E402
    LOOPBACK_BIND_HOSTS as LOOPBACK_BIND_HOSTS,
    PERSISTENCE_ERROR_CODES,
    PersistenceContractError,
    RUNTIME_ID_INVALID,
    RUNTIME_ID_PATTERN,
    RUNTIME_ID_PATTERN_TEXT,
    RuntimeIdValidationError as RuntimeIdValidationError,
    WILDCARD_BIND_HOSTS as WILDCARD_BIND_HOSTS,
    build_runtime_model,
    classify_bind_scope as classify_bind_scope,
    client_config_host_dir,
    client_config_runtime_dir,
    client_configs_host_root,
    compile_persistence_summary,
    extract_command_port as extract_command_port,
    extract_host_port as extract_host_port,
    host_path_to_absolute_path,
    load_yaml,
    load_runtime_env,
    runtime_manifest_path,
    runtime_path_to_host_path as runtime_path_to_host_path,
    storage_binding_by_id,
    validate_runtime_id as validate_runtime_id,
)
from lib.redaction import REDACTION_MARKER as REDACTION_MARKER  # noqa: E402
from lib.redaction import SECRET_KEY_PATTERN as SECRET_KEY_PATTERN  # noqa: E402
from lib.redaction import is_secret_key as is_secret_key  # noqa: E402
from lib.redaction import redact_text as redact_text  # noqa: E402
from lib.redaction import redact_value as redact_value  # noqa: E402
from .errors import CheckResult

def next_actions_for_doctor(results: list["CheckResult"]) -> list[str]:
    has_fail = any(r.status == "fail" for r in results)
    has_warn = any(r.status == "warn" for r in results)
    actions: list[str] = []
    if has_fail or has_warn:
        actions.append("sync --format json")
    if has_fail:
        actions.append("status --format json")
    if not has_fail and not has_warn:
        actions.append("status --format json")
    return actions

def is_elevated_pressure_level(level: Any) -> bool:
    """True when a local-disk pressure level is in the advisory-worthy band.

    Shared so the advisory warning builder, stewardship risk gate, and
    pressure-report next-actions all classify ``critical``/``high``/``elevated``
    identically and never drift apart.
    """
    return str(level or "").strip() in {"critical", "high", "elevated"}

def pressure_advisory_warning_messages(advisory: dict[str, Any]) -> list[str]:
    """Build the agent-facing pressure/offload advisory warning strings.

    Single source of truth for the advisory ``warnings`` text consumed by
    status, context, pulse, stewardship, and evidence surfaces. The exact
    strings and ordering here are the byte-for-byte contract; surfaces only
    render the resulting list, they do not reconstruct it.
    """
    local_disk = advisory.get("local_disk") or {}
    rch = advisory.get("rch") or {}
    sbh = advisory.get("sbh") or {}
    warnings: list[str] = []
    level = str(local_disk.get("pressure_level") or "").strip()
    if is_elevated_pressure_level(level):
        warnings.append(
            "Local disk pressure is "
            f"{level}; avoid expensive local build storms and inspect pressure-report first."
        )
    if rch.get("state") in {"not-configured", "remediation"}:
        warnings.append("RCH build offload is not worker-ready; expensive builds may run locally.")
    if sbh.get("state") in {"not-configured", "remediation"}:
        warnings.append("SBH storage guard is not observing; cleanup remains manual review only.")
    if sbh.get("release_caveats"):
        warnings.append("SBH latest Linux release asset has a known mismatch; keep the verified canary pin.")
    if advisory.get("protected_paths"):
        warnings.append("Protected paths are hard vetoes; do not delete agent state or SSH material.")
    return warnings

def next_actions_for_status(status_payload: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    pressure_advisory = status_payload.get("pressure_advisory") or {}
    pressure_warnings = pressure_advisory.get("warnings") or []

    stopped_services = [
        s for s in status_payload.get("services", [])
        if s.get("state") == "stopped" or s.get("state") == "not-running"
    ]
    pending_tasks = [
        t for t in status_payload.get("tasks", [])
        if t.get("state") == "pending"
    ]
    missing_repos = [
        r for r in status_payload.get("repos", [])
        if not r.get("present", True)
    ]

    if missing_repos:
        actions.append("sync --format json")
    if pending_tasks:
        actions.append("bootstrap --format json")
    if stopped_services:
        actions.append("up --format json")
    if pressure_warnings:
        for action in (
            "pressure-report --format json",
            "rch-report --format json",
            "sbh-report --format json",
        ):
            if action not in actions:
                actions.append(action)
    if not actions:
        actions.append("doctor --format json")
    return actions

def next_actions_for_sync() -> list[str]:
    return ["doctor --format json", "status --format json"]

def next_actions_for_up(service_results: list[dict[str, Any]]) -> list[str]:
    has_failed = any(s.get("result") == "failed" for s in service_results)
    if has_failed:
        return ["logs --format json", "doctor --format json"]
    return ["status --format json"]

def next_actions_for_down() -> list[str]:
    return ["status --format json"]

def next_actions_for_bootstrap(task_results: list[dict[str, Any]]) -> list[str]:
    has_failed = any(t.get("result") == "failed" for t in task_results)
    if has_failed:
        return ["logs --format json", "doctor --format json"]
    return ["up --format json", "status --format json"]

def next_actions_for_context() -> list[str]:
    return ["doctor --format json"]

def next_actions_for_private_init() -> list[str]:
    return [
        "client-init <client> --format json",
        "client-diff <client> --format json",
    ]

def next_actions_for_client_init(client_id: str) -> list[str]:
    return [
        f"sync --client {client_id} --format json",
        f"focus {client_id} --format json",
        f"client-diff {client_id} --format json",
        f"client-publish {client_id} --acceptance --format json",
    ]

def next_actions_for_focus(
    client_id: str,
    has_fail: bool,
    live_services: list[dict[str, Any]] | None = None,
) -> list[str]:
    if has_fail:
        return [
            f"doctor --client {client_id} --format json",
            f"logs --client {client_id} --format json",
        ]
    actions = [f"status --client {client_id} --format json"]
    for service in live_services or []:
        service_id = str(service.get("id") or "").strip()
        if service_id:
            actions.append(f"logs --service {service_id} --client {client_id} --format json")
            break
    return actions

def next_actions_for_session_start(client_id: str, session_id: str) -> list[str]:
    return [
        f"session-status {client_id} --session-id {session_id} --format json",
        f"focus {client_id} --format json",
    ]

def next_actions_for_session_status(client_id: str, session_id: str | None = None) -> list[str]:
    actions = [f"focus {client_id} --format json"]
    if session_id:
        actions.insert(0, f"session-event {client_id} --session-id {session_id} --event-type note --message '<message>' --format json")
    return actions

def next_actions_for_session_event(client_id: str, session_id: str) -> list[str]:
    return [
        f"session-status {client_id} --session-id {session_id} --format json",
        f"session-end {client_id} --session-id {session_id} --format json",
    ]

def next_actions_for_session_end(client_id: str, session_id: str) -> list[str]:
    return [
        f"session-status {client_id} --session-id {session_id} --format json",
        f"session-resume {client_id} --session-id {session_id} --format json",
    ]

def next_actions_for_session_resume(client_id: str, session_id: str) -> list[str]:
    return [
        f"session-status {client_id} --session-id {session_id} --format json",
        f"session-end {client_id} --session-id {session_id} --format json",
    ]

def format_profile_args(profiles: list[str] | None) -> str:
    return "".join(f" --profile {profile}" for profile in profiles or [])

def next_actions_for_acceptance_success(client_id: str, profiles: list[str] | None) -> list[str]:
    profile_args = format_profile_args(profiles)
    return [f"status --client {client_id}{profile_args} --format json"]

def next_actions_for_acceptance_mcp_failure(
    profiles: list[str] | None,
    failed_services: list[str],
) -> list[str]:
    actions = [f"sync{format_profile_args(profiles)} --format json"]
    for service_id in failed_services:
        actions.append(f"logs --service {service_id} --format json")
    return actions

def next_actions_for_client_project(client_id: str) -> list[str]:
    return [
        f"render --client {client_id} --format json",
        f"sync --client {client_id} --format json",
    ]

def next_actions_for_client_publish(client_id: str) -> list[str]:
    return [
        f"client-project {client_id} --format json",
        f"render --client {client_id} --format json",
    ]

def next_actions_for_client_diff(client_id: str, target_dir: Path) -> list[str]:
    return [
        f"client-publish {client_id} --target-dir {target_dir} --format json",
        f"client-project {client_id} --format json",
    ]

def next_actions_for_client_open(client_id: str) -> list[str]:
    return [
        f"client-diff {client_id} --format json",
        f"client-publish {client_id} --format json",
    ]

def next_actions_for_first_box(client_id: str, profiles: list[str] | None) -> list[str]:
    profile_args = format_profile_args(profiles)
    return [
        f"status --client {client_id}{profile_args} --format json",
        f"client-diff {client_id}{profile_args} --format json",
        f"client-publish {client_id} --acceptance{profile_args} --format json",
    ]
