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

EXIT_OK = 0

EXIT_ERROR = 1

EXIT_DRIFT = 2

EXIT_NEEDS_INPUT = 3

@dataclass
class CheckResult:
    status: str
    code: str
    message: str
    details: dict[str, Any] | None = None

def structured_error(
    message: str,
    *,
    error_type: str = "runtime_error",
    recoverable: bool = True,
    recovery_hint: str | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    # Single CARRIER for the back-compat error envelope. ``error_type`` IS the
    # stable code, mirrored into the new ``error.code`` and the legacy top-level
    # ``error_code`` so a snapshot test can pin them together. Legacy keys
    # (``error.type``, ``error.recoverable``, top-level ``error_code``) and the
    # ``deprecation`` marker coexist with the new shape for one release.
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": error_type,
            "type": error_type,
            "message": message,
            "recoverable": recoverable,
        },
        "error_code": error_type,
        "deprecation": copy.deepcopy(DEPRECATION_MARKER),
    }
    if recovery_hint is not None:
        payload["error"]["recovery_hint"] = recovery_hint
    if next_actions is not None:
        payload["error"]["next_actions"] = next_actions
        payload["next_actions"] = next_actions
    return payload

def _classify_persistence_error(
    exc: RuntimeError, msg: str, persistence_code: str,
) -> dict[str, Any] | None:
    if not (isinstance(exc, PersistenceContractError) or persistence_code in PERSISTENCE_ERROR_CODES):
        return None
    return structured_error(
        msg,
        error_type=persistence_code or "PERSISTENCE_CONFIG_INVALID",
        recoverable=True,
        recovery_hint=(
            "Fix workspace/persistence.yaml or the related SKILLBOX_STORAGE_* / "
            "SKILLBOX_STATE_ROOT values, then retry."
        ),
        next_actions=["render --format json", "doctor --format json"],
    )

def _classify_message_pattern(msg: str, lower_msg: str) -> dict[str, Any] | None:
    """Match the message body against a fixed table of known error patterns."""
    rules: list[tuple[Callable[[], bool], dict[str, Any]]] = [
        (lambda: "client-init requires" in msg,
         dict(error_type="missing_argument",
              recovery_hint="Provide a client_id argument or use --list-blueprints.",
              next_actions=["client-init --list-blueprints --format json"])),
        (lambda: "blueprint" in lower_msg and "not found" in lower_msg,
         dict(error_type="blueprint_not_found",
              recovery_hint="List available blueprints, then retry with a valid name or path.",
              next_actions=["client-init --list-blueprints --format json"])),
        (lambda: ("required" in lower_msg and "variable" in lower_msg) or "missing required values" in lower_msg,
         dict(error_type="missing_variable",
              recovery_hint="Add the missing --set KEY=VALUE assignments and retry.")),
        (lambda: ("already exists" in lower_msg or "without force" in lower_msg
                  or "already_exists" in lower_msg or "non-projection output directory" in lower_msg),
         dict(error_type="conflict",
              recovery_hint="Use --force to overwrite existing files, or choose a different client id.")),
        (lambda: "target repo has a dirty working tree" in lower_msg,
         dict(error_type="conflict",
              recovery_hint="Commit or discard changes in the target repo, then retry.")),
        (lambda: "no private publish target configured" in lower_msg,
         dict(error_type="missing_target_repo",
              recovery_hint="Run private-init to attach a private repo, or pass --target-dir explicitly.",
              next_actions=["private-init --format json"])),
        (lambda: "target must be a git repo" in lower_msg,
         dict(error_type="invalid_target_repo",
              recovery_hint="Initialize the target repo with git before publishing.")),
        (lambda: "env file" in lower_msg and ("missing" in lower_msg or "unresolved" in lower_msg),
         dict(error_type="missing_env_file",
              recovery_hint="Create the env source file or run sync first.",
              next_actions=["sync --format json"])),
        (lambda: "failed to become healthy" in lower_msg,
         dict(error_type="service_health_failure",
              recovery_hint="Check service logs for the root cause, then restart.",
              next_actions=["logs --format json", "doctor --format json"])),
        (lambda: "invalid client id" in lower_msg,
         dict(error_type="invalid_client_id", recoverable=True,
              recovery_hint="Client IDs must be lowercase alphanumeric with single hyphens: my-project.")),
        (lambda: "unknown client scaffold pack" in lower_msg,
         dict(error_type="invalid_scaffold_pack", recoverable=True,
              recovery_hint="Use a supported scaffold pack such as `planning`, `skill-builder`, or `hybrid`.",
              next_actions=["client-init --list-blueprints --format json"])),
        (lambda: "session_id is required" in lower_msg or "session event_type is required" in lower_msg,
         dict(error_type="missing_argument", recoverable=True,
              recovery_hint="Provide the required session_id and event_type arguments, then retry.")),
        (lambda: "session not found" in lower_msg,
         dict(error_type="session_not_found", recoverable=True,
              recovery_hint="List recent sessions for the client, then retry with a valid session_id.",
              next_actions=["session-status <client> --format json", "focus <client> --format json"])),
        (lambda: ("session is not active" in lower_msg
                  or "session is already active" in lower_msg
                  or "unsupported session status" in lower_msg),
         dict(error_type="session_state_conflict", recoverable=True,
              recovery_hint="Inspect the session state first, then resume or end it with a valid transition.",
              next_actions=["session-status <client> --format json"])),
        (lambda: "has no overlay at" in lower_msg,
         dict(error_type="client_overlay_missing", recoverable=True,
              recovery_hint=(
                  "This client has no overlay yet. Client overlays are operator-owned "
                  "private config — the `personal` examples in the README assume one is "
                  "attached. Scaffold it with `onboard <id>` (or `client-init <id>`) "
                  "before focus/sync/status target it."
              ),
              next_actions=[
                  "client-init --list-blueprints --format json",
                  "render --format json",
              ])),
        (lambda: "unknown runtime client" in lower_msg and "available clients: (none)" in lower_msg,
         dict(error_type="unknown_client", recoverable=True,
              recovery_hint=(
                  "No client overlays are attached in this checkout. Clients are "
                  "operator-owned private config, not part of a default clone — the "
                  "`personal` examples in the README assume you have attached one. "
                  "Create one with `client-init <id>` (then `first-box <id>`), or run "
                  "the command without `--client` to use the core scope."
              ),
              next_actions=[
                  "client-init --list-blueprints --format json",
                  "render --format json",
              ])),
        (lambda: "unknown runtime client" in lower_msg,
         dict(error_type="unknown_client", recoverable=True,
              recovery_hint=(
                  "The requested client is not declared in this checkout. Use one of "
                  "the available clients named in the message, or attach it with "
                  "`client-init <id>`."
              ),
              next_actions=[
                  "render --format json",
                  "client-init --list-blueprints --format json",
              ])),
    ]
    for predicate, kwargs in rules:
        if predicate():
            return structured_error(msg, **kwargs)
    return None

_COMMAND_FALLBACK_NEXT_ACTIONS: dict[str, list[str]] = {
    "sync": ["doctor --format json", "status --format json"],
    "up": ["doctor --format json", "status --format json"],
    "bootstrap": ["doctor --format json", "status --format json"],
    "restart": ["doctor --format json", "status --format json"],
    "focus": ["doctor --format json", "status --format json"],
    "client-init": ["client-init --list-blueprints --format json"],
    "client-open": ["focus --format json", "doctor --format json"],
    "first-box": ["status --format json", "doctor --format json"],
    "down": ["status --format json"],
    "session-start": ["focus --format json", "status --format json"],
    "session-event": ["focus --format json", "status --format json"],
    "session-end": ["focus --format json", "status --format json"],
    "session-resume": ["focus --format json", "status --format json"],
    "session-status": ["focus --format json", "status --format json"],
    "stewardship-report": ["focus --format json", "status --format json"],
}

def classify_error(exc: RuntimeError, command: str) -> dict[str, Any]:
    """Map a RuntimeError to a structured error payload with contextual recovery hints."""
    msg = str(exc)
    lower_msg = msg.lower()
    code = str(getattr(exc, "code", "") or "").strip()

    if code == RUNTIME_ID_INVALID:
        return structured_error(
            msg,
            error_type=RUNTIME_ID_INVALID,
            recoverable=True,
            recovery_hint=(
                f"Runtime ids must match {RUNTIME_ID_PATTERN_TEXT}. Rename the offending id in "
                "workspace/runtime.yaml or the client overlay.yaml, then re-render."
            ),
            next_actions=["render --format json", "doctor --format json"],
        )

    persistence_payload = _classify_persistence_error(exc, msg, code)
    if persistence_payload is not None:
        return persistence_payload

    pattern_payload = _classify_message_pattern(msg, lower_msg)
    if pattern_payload is not None:
        return pattern_payload

    return structured_error(
        msg,
        recovery_hint="Run doctor to diagnose, then check logs for details.",
        next_actions=_COMMAND_FALLBACK_NEXT_ACTIONS.get(command) or ["doctor --format json"],
    )
