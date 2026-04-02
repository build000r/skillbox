#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import selectors
import signal
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
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


PACKAGE_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = PACKAGE_DIR.parent
DEFAULT_ROOT_DIR = SCRIPT_DIR.parent.resolve()
SCRIPTS_DIR = DEFAULT_ROOT_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.runtime_model import (  # noqa: E402
    build_runtime_model,
    client_config_host_dir,
    client_config_runtime_dir,
    client_configs_host_root,
    host_path_to_absolute_path,
    load_yaml,
    load_runtime_env,
    runtime_manifest_path,
    runtime_path_to_host_path,
)


VALID_REPO_SOURCE_KINDS = {"bind", "directory", "git", "manual"}
VALID_SYNC_MODES = {"external", "ensure-directory", "clone-if-missing", "manual"}
VALID_ARTIFACT_SOURCE_KINDS = {"file", "manual", "url"}
VALID_ARTIFACT_SYNC_MODES = {"copy-if-missing", "download-if-missing", "manual"}
VALID_ENV_FILE_SOURCE_KINDS = {"file", "manual"}
VALID_ENV_FILE_SYNC_MODES = {"write", "manual"}
VALID_SKILL_SYNC_MODES = {"unpack-bundles"}
VALID_HEALTHCHECK_TYPES = {"http", "path_exists", "process_running"}
VALID_CHECK_TYPES = {"path_exists"}
VALID_TASK_SUCCESS_TYPES = {"path_exists"}
LOCKFILE_VERSION = 1
CONTEXT_CLAUDE_REL = Path("home") / ".claude" / "CLAUDE.md"
CONTEXT_CODEX_REL = Path("home") / ".codex" / "AGENTS.md"
CONTEXT_SYMLINK_TARGET = os.path.join("..", ".claude", "CLAUDE.md")
CLIENT_PROJECTS_REL = Path("builds") / "clients"
CLIENT_OPEN_ROOT_REL = Path("sand")
CLIENT_PROJECTION_VERSION = 1
CLIENT_PROJECT_RUNTIME_MODEL_REL = Path("runtime-model.json")
CLIENT_PROJECTION_METADATA_REL = Path("projection.json")
CLIENT_PUBLISH_VERSION = 1
CLIENT_ACCEPTANCE_VERSION = 1
CLIENT_PUBLISH_ROOT_REL = Path("clients")
CLIENT_PUBLISH_CURRENT_REL = Path("current")
CLIENT_PUBLISH_METADATA_REL = Path("publish.json")
CLIENT_ACCEPTANCE_METADATA_REL = Path("acceptance.json")
DEFAULT_PRIVATE_REPO_REL = Path("..") / "skillbox-config"
CLIENT_PLANNING_SKILL_TEMPLATE_REL = Path("workspace") / "client-planning-skills"
CLIENT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BLUEPRINT_VARIABLE_PATTERN = re.compile(r"^[A-Z0-9_]+$")
SCAFFOLD_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
SHA256_HEX_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
DEFAULT_SERVICE_START_WAIT_SECONDS = 10.0
DEFAULT_SERVICE_STOP_WAIT_SECONDS = 5.0
DEFAULT_LOG_TAIL_LINES = 40
PATH_LIKE_ENV_KEYS = {
    "SKILLBOX_WORKSPACE_ROOT",
    "SKILLBOX_REPOS_ROOT",
    "SKILLBOX_SKILLS_ROOT",
    "SKILLBOX_LOG_ROOT",
    "SKILLBOX_HOME_ROOT",
    "SKILLBOX_MONOSERVER_ROOT",
    "SKILLBOX_CLIENTS_ROOT",
    "SKILLBOX_SWIMMERS_REPO",
    "SKILLBOX_SWIMMERS_INSTALL_DIR",
    "SKILLBOX_SWIMMERS_BIN",
    "SKILLBOX_DCG_BIN",
}
HARDENED_SHARED_DEFAULT_SKILLS = [
    "ask-cascade",
    "build-vs-clone",
    "describe",
    "reproduce",
    "commit",
    "dev-sanity",
    "skillbox-operator",
]
HARDENED_CLIENT_PLANNING_SKILLS = [
    "domain-planner",
    "domain-reviewer",
    "domain-scaffolder",
    "divide-and-conquer",
]
HARDENED_CLIENT_PLAN_PATHS = {
    "plan_root": "plans/released",
    "plan_draft": "plans/draft",
    "plan_index": "plans/INDEX.md",
    "session_plans": "plans/sessions",
}


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
    payload: dict[str, Any] = {
        "error": {
            "type": error_type,
            "message": message,
            "recoverable": recoverable,
        },
    }
    if recovery_hint is not None:
        payload["error"]["recovery_hint"] = recovery_hint
    if next_actions is not None:
        payload["next_actions"] = next_actions
    return payload


def classify_error(exc: RuntimeError, command: str) -> dict[str, Any]:
    """Map a RuntimeError to a structured error payload with contextual recovery hints."""
    msg = str(exc)

    if "client-init requires" in msg:
        return structured_error(
            msg,
            error_type="missing_argument",
            recovery_hint="Provide a client_id argument or use --list-blueprints.",
            next_actions=["client-init --list-blueprints --format json"],
        )
    if "blueprint" in msg.lower() and "not found" in msg.lower():
        return structured_error(
            msg,
            error_type="blueprint_not_found",
            recovery_hint="List available blueprints, then retry with a valid name or path.",
            next_actions=["client-init --list-blueprints --format json"],
        )
    if (
        ("required" in msg.lower() and "variable" in msg.lower())
        or "missing required values" in msg.lower()
    ):
        return structured_error(
            msg,
            error_type="missing_variable",
            recovery_hint="Add the missing --set KEY=VALUE assignments and retry.",
        )
    if (
        "already exists" in msg.lower()
        or "without force" in msg.lower()
        or "already_exists" in msg.lower()
        or "non-projection output directory" in msg.lower()
    ):
        return structured_error(
            msg,
            error_type="conflict",
            recovery_hint="Use --force to overwrite existing files, or choose a different client id.",
        )
    if "target repo has a dirty working tree" in msg.lower():
        return structured_error(
            msg,
            error_type="conflict",
            recovery_hint="Commit or discard changes in the target repo, then retry.",
        )
    if "no private publish target configured" in msg.lower():
        return structured_error(
            msg,
            error_type="missing_target_repo",
            recovery_hint="Run private-init to attach a private repo, or pass --target-dir explicitly.",
            next_actions=["private-init --format json"],
        )
    if "target must be a git repo" in msg.lower():
        return structured_error(
            msg,
            error_type="invalid_target_repo",
            recovery_hint="Initialize the target repo with git before publishing.",
        )
    if "env file" in msg.lower() and ("missing" in msg.lower() or "unresolved" in msg.lower()):
        return structured_error(
            msg,
            error_type="missing_env_file",
            recovery_hint="Create the env source file or run sync first.",
            next_actions=[f"sync --format json"],
        )
    if "failed to become healthy" in msg.lower():
        return structured_error(
            msg,
            error_type="service_health_failure",
            recovery_hint="Check service logs for the root cause, then restart.",
            next_actions=["logs --format json", "doctor --format json"],
        )
    if "invalid client id" in msg.lower():
        return structured_error(
            msg,
            error_type="invalid_client_id",
            recoverable=True,
            recovery_hint="Client IDs must be lowercase alphanumeric with single hyphens: my-project.",
        )

    # Contextual fallback based on command
    fallback_next: list[str] = []
    if command in ("sync", "up", "bootstrap", "restart", "focus"):
        fallback_next = ["doctor --format json", "status --format json"]
    elif command == "client-init":
        fallback_next = ["client-init --list-blueprints --format json"]
    elif command == "client-open":
        fallback_next = ["focus --format json", "doctor --format json"]
    elif command == "first-box":
        fallback_next = ["status --format json", "doctor --format json"]
    elif command in ("down",):
        fallback_next = ["status --format json"]

    return structured_error(
        msg,
        recovery_hint="Run doctor to diagnose, then check logs for details.",
        next_actions=fallback_next or ["doctor --format json"],
    )


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


def next_actions_for_status(status_payload: dict[str, Any]) -> list[str]:
    actions: list[str] = []

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


def repo_rel(root_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root_dir))
    except ValueError:
        return str(path)


def resolve_context_dir(root_dir: Path, raw_context_dir: str | None) -> Path | None:
    value = str(raw_context_dir or "").strip()
    if not value:
        return None
    return host_path_to_absolute_path(root_dir, value)


def context_output_paths(root_dir: Path, context_dir: Path | None) -> tuple[Path, Path, str]:
    if context_dir is None:
        return (
            root_dir / CONTEXT_CLAUDE_REL,
            root_dir / CONTEXT_CODEX_REL,
            CONTEXT_SYMLINK_TARGET,
        )

    target_dir = context_dir.resolve()
    return (
        target_dir / "CLAUDE.md",
        target_dir / "AGENTS.md",
        "CLAUDE.md",
    )


def client_overlay_location(root_dir: Path, client_id: str) -> tuple[dict[str, str], Path, Path]:
    env_values = load_runtime_env(root_dir)
    host_dir = client_config_host_dir(root_dir, env_values, client_id)
    runtime_dir = client_config_runtime_dir(env_values, client_id)
    return env_values, host_dir / "overlay.yaml", runtime_dir / "overlay.yaml"


def client_context_location(root_dir: Path, client_id: str) -> tuple[dict[str, str], Path, Path]:
    env_values = load_runtime_env(root_dir)
    host_dir = client_config_host_dir(root_dir, env_values, client_id)
    runtime_dir = client_config_runtime_dir(env_values, client_id)
    return env_values, host_dir / "context.yaml", runtime_dir / "context.yaml"


def run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def human_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0 or unit == "GiB":
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{num_bytes}B"


# ---------------------------------------------------------------------------
# Event journal (append-only JSONL)
# ---------------------------------------------------------------------------

JOURNAL_REL = Path("logs") / "runtime" / "journal.jsonl"


def emit_event(
    event_type: str,
    subject: str,
    detail: dict[str, Any] | None = None,
    root_dir: Path = DEFAULT_ROOT_DIR,
) -> None:
    """Append a single structured event to the runtime journal."""
    journal_path = root_dir / JOURNAL_REL
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "type": event_type,
        "subject": subject,
        "detail": detail or {},
    }
    try:
        with journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
    except OSError:
        pass  # Journal write is best-effort; never break a real operation.


def query_journal(
    root_dir: Path = DEFAULT_ROOT_DIR,
    *,
    since: float | None = None,
    event_type: str | None = None,
    subject: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read recent journal events, newest first, with optional filters."""
    journal_path = root_dir / JOURNAL_REL
    if not journal_path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since is not None and ev.get("ts", 0) < since:
            continue
        if event_type is not None and ev.get("type") != event_type:
            continue
        if subject is not None and ev.get("subject") != subject:
            continue
        events.append(ev)
    events.reverse()
    return events[:limit]


# ---------------------------------------------------------------------------
# Event acknowledgement
# ---------------------------------------------------------------------------

ACKS_REL = Path("logs") / "runtime" / "journal.acks.json"
DEFAULT_ACK_EXPIRY_HOURS = 24


def read_acks(root_dir: Path = DEFAULT_ROOT_DIR) -> dict[str, Any]:
    """Read the ack store. Keys are stringified event timestamps."""
    acks_path = root_dir / ACKS_REL
    if not acks_path.is_file():
        return {}
    try:
        return json.loads(acks_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_acks(acks: dict[str, Any], root_dir: Path = DEFAULT_ROOT_DIR) -> None:
    """Write the ack store."""
    acks_path = root_dir / ACKS_REL
    acks_path.parent.mkdir(parents=True, exist_ok=True)
    acks_path.write_text(json.dumps(acks, indent=2), encoding="utf-8")


def is_acked(
    acks: dict[str, Any],
    event_ts: float,
    expiry_hours: float = DEFAULT_ACK_EXPIRY_HOURS,
) -> bool:
    """Check whether a journal event has been acknowledged and the ack is still live."""
    key = str(event_ts)
    entry = acks.get(key)
    if not entry:
        return False
    acked_at = entry.get("at", 0)
    now = time.time()
    if expiry_hours > 0 and (now - acked_at) > (expiry_hours * 3600):
        return False
    return True


def ack_events(
    root_dir: Path,
    *,
    event_type: str | None = None,
    subject: str | None = None,
    ts: float | None = None,
    ack_all: bool = False,
    reason: str = "",
    since_hours: float = DEFAULT_ACK_EXPIRY_HOURS,
) -> list[dict[str, Any]]:
    """Acknowledge matching journal events. Returns list of newly acked items."""
    since = time.time() - (since_hours * 3600) if not ack_all else None
    events = query_journal(root_dir, since=since, limit=500)
    acks = read_acks(root_dir)
    now = time.time()
    newly_acked: list[dict[str, Any]] = []

    for ev in events:
        ev_ts = ev.get("ts", 0)
        key = str(ev_ts)
        if key in acks:
            continue
        if ts is not None:
            if ev_ts != ts:
                continue
        elif not ack_all:
            if event_type and ev.get("type") != event_type:
                continue
            if subject and ev.get("subject") != subject:
                continue

        acks[key] = {"at": now, "reason": reason}
        newly_acked.append({
            "ts": ev_ts,
            "type": ev.get("type"),
            "subject": ev.get("subject"),
        })

    if newly_acked:
        save_acks(acks, root_dir)

    return newly_acked


def prune_expired_acks(
    root_dir: Path,
    expiry_hours: float = DEFAULT_ACK_EXPIRY_HOURS,
) -> int:
    """Remove expired acks from the store. Returns count of pruned entries."""
    acks = read_acks(root_dir)
    now = time.time()
    cutoff = now - (expiry_hours * 3600)
    expired_keys = [k for k, v in acks.items() if v.get("at", 0) < cutoff]
    for k in expired_keys:
        del acks[k]
    if expired_keys:
        save_acks(acks, root_dir)
    return len(expired_keys)


def resolve_root_dir(raw_root: str | None) -> Path:
    if raw_root:
        return Path(raw_root).resolve()
    return DEFAULT_ROOT_DIR


def titleize_client_id(client_id: str) -> str:
    return " ".join(part.capitalize() for part in client_id.split("-"))


def validate_client_id(client_id: str) -> str:
    normalized = client_id.strip()
    if not CLIENT_ID_PATTERN.fullmatch(normalized):
        raise RuntimeError(
            f"Invalid client id {client_id!r}. Use lowercase letters, numbers, and single hyphens."
        )
    return normalized


def write_text_file(path: Path, content: str, dry_run: bool) -> None:
    ensure_directory(path.parent, dry_run)
    if dry_run:
        return
    path.write_text(content, encoding="utf-8")


def normalize_host_rel_path(root_dir: Path, path: Path) -> str:
    rel_path = os.path.relpath(path, root_dir)
    if rel_path == ".":
        return rel_path
    if rel_path.startswith("."):
        return rel_path
    return f"./{rel_path}"


def upsert_env_file_values(path: Path, updates: dict[str, str]) -> bool:
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing_text.splitlines()
    updated_keys: set[str] = set()
    new_lines: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            raw_key, _raw_value = stripped.split("=", 1)
            key = raw_key.strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(raw_line)

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    serialized = "\n".join(new_lines).rstrip()
    if serialized:
        serialized += "\n"
    if serialized == existing_text:
        return False
    path.write_text(serialized, encoding="utf-8")
    return True


def require_yaml(feature: str) -> Any:
    if yaml is None:
        raise RuntimeError(
            f"Missing PyYAML. Install `python3-yaml` or `pip install pyyaml` to {feature}."
        )
    return yaml


def client_blueprint_dir(root_dir: Path) -> Path:
    return root_dir / "workspace" / "client-blueprints"


def render_yaml_document(document: Any) -> str:
    yaml_mod = require_yaml("render client blueprints")
    return yaml_mod.safe_dump(document, sort_keys=False).rstrip() + "\n"


def parse_key_value_assignments(raw_assignments: list[str], option_name: str) -> list[tuple[str, str]]:
    assignments: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for raw_assignment in raw_assignments:
        if "=" not in raw_assignment:
            raise RuntimeError(f"{option_name} expects KEY=VALUE assignments, got {raw_assignment!r}.")
        raw_key, raw_value = raw_assignment.split("=", 1)
        key = raw_key.strip()
        if not key or not BLUEPRINT_VARIABLE_PATTERN.fullmatch(key):
            raise RuntimeError(
                f"{option_name} key {raw_key!r} is invalid. Use uppercase letters, numbers, and underscores."
            )
        if key in seen_keys:
            raise RuntimeError(f"Duplicate {option_name} assignment for {key}.")
        assignments.append((key, raw_value))
        seen_keys.add(key)
    return assignments


def resolve_known_placeholders(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        def replacer(match: re.Match[str]) -> str:
            key = match.group(1)
            return mapping.get(key, match.group(0))

        return SCAFFOLD_PLACEHOLDER_PATTERN.sub(replacer, value)
    if isinstance(value, list):
        return [resolve_known_placeholders(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: resolve_known_placeholders(item, mapping) for key, item in value.items()}
    return value


def split_csv_values(raw_value: Any) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        return [value.strip() for value in raw_value.split(",") if value.strip()]
    if isinstance(raw_value, list):
        values: list[str] = []
        for item in raw_value:
            if isinstance(item, str):
                values.extend(split_csv_values(item))
                continue
            text = str(item).strip()
            if text:
                values.append(text)
        return values
    text = str(raw_value).strip()
    return [text] if text else []


def normalize_client_connector_entries(
    raw_connectors: Any,
    *,
    client_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if raw_connectors is None:
        return [], []
    if isinstance(raw_connectors, str):
        return [{"id": connector_id} for connector_id in split_csv_values(raw_connectors)], []
    if not isinstance(raw_connectors, list):
        return [], [f"client {client_id} connectors must be a comma-separated string or a list"]

    entries: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, raw_entry in enumerate(raw_connectors, start=1):
        if isinstance(raw_entry, str):
            connector_id = raw_entry.strip()
            if not connector_id:
                issues.append(f"client {client_id} connectors[{index}] is empty")
                continue
            entries.append({"id": connector_id})
            continue
        if not isinstance(raw_entry, dict):
            issues.append(
                f"client {client_id} connectors[{index}] must be a string or mapping, got {type(raw_entry).__name__}"
            )
            continue

        entry = copy.deepcopy(raw_entry)
        connector_id = str(entry.get("id", "")).strip()
        if not connector_id:
            issues.append(f"client {client_id} connectors[{index}] is missing id")
            continue
        entry["id"] = connector_id

        capabilities = entry.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, list):
                issues.append(f"client {client_id} connector {connector_id!r} capabilities must be a list")
            else:
                entry["capabilities"] = split_csv_values(capabilities)

        scopes = entry.get("scopes")
        if scopes is not None and not isinstance(scopes, dict):
            issues.append(f"client {client_id} connector {connector_id!r} scopes must be a mapping")

        entries.append(entry)

    return entries, issues


def scaffold_connector_entries(raw_connectors: Any, values: dict[str, str], *, client_id: str) -> list[dict[str, Any]]:
    entries, issues = normalize_client_connector_entries(raw_connectors, client_id=client_id)
    if issues:
        raise RuntimeError("Invalid client connector declaration in blueprint: " + "; ".join(issues))

    slack_capabilities = split_csv_values(values.get("SLACK_CAPABILITIES", ""))
    slack_channels = split_csv_values(values.get("SLACK_CHANNELS", ""))

    normalized_entries: list[dict[str, Any]] = []
    for entry in entries:
        normalized_entry = copy.deepcopy(entry)
        if normalized_entry["id"] == "slack":
            if slack_capabilities and "capabilities" not in normalized_entry:
                normalized_entry["capabilities"] = slack_capabilities
            if slack_channels:
                scopes = copy.deepcopy(normalized_entry.get("scopes") or {})
                scopes["channels"] = slack_channels
                normalized_entry["scopes"] = scopes
        normalized_entries.append(normalized_entry)
    return normalized_entries


def list_client_blueprints(root_dir: Path) -> list[dict[str, Any]]:
    blueprint_root = client_blueprint_dir(root_dir)
    if not blueprint_root.is_dir():
        return []

    blueprints: list[dict[str, Any]] = []
    for path in sorted(blueprint_root.glob("*.yaml")):
        blueprints.append(load_client_blueprint(path))
    return blueprints


def resolve_client_blueprint_path(root_dir: Path, raw_blueprint: str) -> Path:
    candidate = Path(raw_blueprint).expanduser()
    blueprint_root = client_blueprint_dir(root_dir)
    attempts: list[Path] = []

    if candidate.is_absolute():
        attempts.append(candidate)
    else:
        attempts.append((root_dir / candidate).resolve())
        attempts.append((blueprint_root / candidate).resolve())
        if not candidate.suffix:
            attempts.append((blueprint_root / f"{candidate}.yaml").resolve())

    for path in attempts:
        if path.is_file():
            return path

    available = ", ".join(item["id"] for item in list_client_blueprints(root_dir)) or "(none)"
    raise RuntimeError(
        f"Client blueprint {raw_blueprint!r} was not found. Available blueprints: {available}"
    )


def load_client_blueprint(path: Path) -> dict[str, Any]:
    yaml_mod = require_yaml("use client blueprints")

    try:
        raw = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Client blueprint not found: {path}") from exc
    except Exception as exc:  # pragma: no cover - defensive parse path
        raise RuntimeError(f"Failed to parse client blueprint {path}: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Expected a YAML object in client blueprint {path}")

    version = raw.get("version", 1)
    if version != 1:
        raise RuntimeError(f"Unsupported client blueprint version in {path}: {version!r}")

    raw_client = raw.get("client")
    if raw_client is None:
        client = {}
    elif isinstance(raw_client, dict):
        client = raw_client
    else:
        raise RuntimeError(f"Expected `client` to be a mapping in {path}")

    raw_variables = raw.get("variables") or []
    if not isinstance(raw_variables, list):
        raise RuntimeError(f"Expected `variables` to be a list in {path}")

    variables: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw_variable in raw_variables:
        if not isinstance(raw_variable, dict):
            raise RuntimeError(f"Expected every variable entry to be a mapping in {path}")
        name = str(raw_variable.get("name", "")).strip()
        if not name or not BLUEPRINT_VARIABLE_PATTERN.fullmatch(name):
            raise RuntimeError(
                f"Invalid client blueprint variable {name!r} in {path}. "
                "Use uppercase letters, numbers, and underscores."
            )
        if name in seen_names:
            raise RuntimeError(f"Duplicate client blueprint variable {name!r} in {path}")
        seen_names.add(name)
        variables.append(
            {
                "name": name,
                "required": bool(raw_variable.get("required")),
                "default": None if "default" not in raw_variable else str(raw_variable.get("default", "")),
                "description": str(raw_variable.get("description", "")).strip(),
            }
        )

    return {
        "id": path.stem,
        "path": str(path),
        "description": str(raw.get("description", "")).strip(),
        "variables": variables,
        "client": client,
    }


def base_client_overlay(
    client_id: str,
    client_label: str,
    client_root: str,
    client_default_cwd: str,
) -> dict[str, Any]:
    return {
        "id": client_id,
        "label": client_label,
        "default_cwd": client_default_cwd,
        "repo_roots": [
            {
                "id": f"{client_id}-root",
                "kind": "repo-root",
                "path": client_root,
                "required": True,
                "profiles": ["core"],
                "source": {"kind": "bind"},
                "sync": {"mode": "external"},
                "notes": "Client root mounted from the shared monoserver tree.",
            }
        ],
        "skills": [
            {
                "id": f"{client_id}-skills",
                "kind": "packaged-skill-set",
                "required": False,
                "profiles": ["core"],
                "bundle_dir": f"${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/bundles",
                "manifest": f"${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skills.manifest",
                "sources_config": f"${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skills.sources.yaml",
                "lock_path": f"${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skills.lock.json",
                "sync": {"mode": "unpack-bundles"},
                "install_targets": [
                    {
                        "id": "claude",
                        "path": "${SKILLBOX_HOME_ROOT}/.claude/skills",
                    },
                    {
                        "id": "codex",
                        "path": "${SKILLBOX_HOME_ROOT}/.codex/skills",
                    },
                ],
                "notes": "Client-scoped skills layered on top of the shared defaults.",
            }
        ],
        "logs": [
            {
                "id": client_id,
                "path": f"${{SKILLBOX_LOG_ROOT}}/clients/{client_id}",
                "required": False,
                "profiles": ["core"],
                "retention_days": 14,
                "notes": f"Client-scoped logs for the {client_id} overlay.",
            }
        ],
        "context": {
            "cwd_match": [client_default_cwd],
            "plans": copy.deepcopy(HARDENED_CLIENT_PLAN_PATHS),
        },
        "checks": [
            {
                "id": f"{client_id}-root",
                "type": "path_exists",
                "path": client_root,
                "required": True,
                "profiles": ["core"],
                "notes": f"The {client_id} overlay expects the client root to be mounted.",
            }
        ],
    }


def render_client_skill_manifest(
    client_label: str,
    required_skills: list[str],
    existing_skills: list[str] | None = None,
) -> str:
    ordered: list[str] = []
    seen: set[str] = set()
    for skill_name in [*required_skills, *(existing_skills or [])]:
        normalized = str(skill_name).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)

    lines = [f"# {client_label} client-specific skills."]
    if ordered:
        lines.append("")
        lines.extend(ordered)
    return "\n".join(lines) + "\n"


def render_client_plan_index(client_label: str) -> str:
    return (
        f"# {client_label} Plan Index\n"
        "\n"
        "| Slice | Tag | Status | Summary |\n"
        "|---|---|---|---|\n"
    )


def client_plan_seed_files(overlay_dir: Path, client_label: str) -> dict[Path, str]:
    plan_dir = overlay_dir / "plans"
    return {
        plan_dir / "INDEX.md": render_client_plan_index(client_label),
        plan_dir / "draft" / ".gitkeep": "",
        plan_dir / "released" / ".gitkeep": "",
        plan_dir / "sessions" / ".gitkeep": "",
    }


def client_planning_skill_template_root() -> Path:
    return (DEFAULT_ROOT_DIR / CLIENT_PLANNING_SKILL_TEMPLATE_REL).resolve()


def ensure_client_planning_skill_sources(
    root_dir: Path,
    overlay_dir: Path,
    *,
    dry_run: bool,
) -> list[str]:
    template_root = client_planning_skill_template_root()
    actions: list[str] = []
    if not template_root.is_dir():
        raise RuntimeError(f"Missing client planning skill templates at {template_root}")

    skills_root = overlay_dir / "skills"
    ensure_directory(skills_root, dry_run=dry_run)
    for skill_name in HARDENED_CLIENT_PLANNING_SKILLS:
        source_dir = template_root / skill_name
        if not source_dir.is_dir():
            raise RuntimeError(f"Missing planning skill template for {skill_name} at {source_dir}")
        target_dir = skills_root / skill_name
        if target_dir.exists():
            continue
        if not dry_run:
            shutil.copytree(source_dir, target_dir)
        actions.append(f"copy-skill-template: {repo_rel(root_dir, target_dir)}")
    return actions


def ensure_client_planning_skill_bundles(
    root_dir: Path,
    overlay_dir: Path,
    *,
    dry_run: bool,
) -> list[str]:
    actions: list[str] = []
    output_dir = overlay_dir / "bundles"
    ensure_directory(output_dir, dry_run=dry_run)
    packager = DEFAULT_ROOT_DIR / "scripts" / "package_skill.py"

    for skill_name in HARDENED_CLIENT_PLANNING_SKILLS:
        source_dir = overlay_dir / "skills" / skill_name
        bundle_path = output_dir / f"{skill_name}.skill"
        if bundle_path.is_file():
            continue
        if dry_run:
            actions.append(f"package-skill-bundle: {repo_rel(root_dir, bundle_path)}")
            continue
        result = run_command(
            [
                sys.executable,
                str(packager),
                str(source_dir),
                str(output_dir),
            ],
            cwd=DEFAULT_ROOT_DIR,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"failed to package {skill_name}")
        actions.append(f"package-skill-bundle: {repo_rel(root_dir, bundle_path)}")
    return actions


def default_client_scaffold_files(
    root_dir: Path,
    env_values: dict[str, str],
    client_id: str,
    client_label: str,
    client_root: str,
    client_default_cwd: str,
) -> dict[Path, str]:
    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)
    bundle_dir = overlay_dir / "bundles"
    skills_dir = overlay_dir / "skills"

    overlay_path = overlay_dir / "overlay.yaml"
    manifest_path = overlay_dir / "skills.manifest"
    sources_path = overlay_dir / "skills.sources.yaml"
    bundle_readme_path = bundle_dir / "README.md"
    skills_keep_path = skills_dir / ".gitkeep"

    target_files = {
        overlay_path: render_yaml_document(
            {
                "version": 1,
                "client": base_client_overlay(
                    client_id=client_id,
                    client_label=client_label,
                    client_root=client_root,
                    client_default_cwd=client_default_cwd,
                ),
            }
        ),
        manifest_path: render_client_skill_manifest(
            client_label,
            HARDENED_CLIENT_PLANNING_SKILLS,
        ),
        sources_path: (
            "version: 1\n"
            "\n"
            "sources:\n"
            "  - kind: local\n"
            "    path: \"./skills\"\n"
        ),
        bundle_readme_path: (
            f"Generated `.skill` bundles for the `{client_id}` client overlay land here.\n"
        ),
        skills_keep_path: "",
    }
    target_files.update(client_plan_seed_files(overlay_dir, client_label))
    return target_files


def merge_client_overlay(base_client: dict[str, Any], blueprint_client: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base_client)
    additive_sections = ("repo_roots", "repos", "artifacts", "env_files", "skills", "tasks", "services", "logs", "checks")
    scalar_items = dict(blueprint_client)

    for key in additive_sections:
        if key not in scalar_items:
            continue
        raw_items = scalar_items.pop(key)
        if raw_items is None:
            continue
        if not isinstance(raw_items, list):
            raise RuntimeError(f"Expected blueprint client.{key} to be a list.")
        merged.setdefault(key, [])
        merged[key].extend(copy.deepcopy(raw_items))

    for key, value in scalar_items.items():
        merged[key] = copy.deepcopy(value)

    return merged


def build_blueprinted_client_scaffold_files(
    root_dir: Path,
    env_values: dict[str, str],
    client_id: str,
    client_label: str,
    client_root: str,
    client_default_cwd: str,
    explicit_label: bool,
    explicit_default_cwd: bool,
    blueprint: dict[str, Any],
    blueprint_assignments: list[tuple[str, str]],
) -> dict[Path, str]:
    values = {
        "CLIENT_ID": client_id,
        "CLIENT_LABEL": client_label,
        "CLIENT_ROOT": client_root,
        "CLIENT_DEFAULT_CWD": client_default_cwd,
    }
    for key, raw_value in blueprint_assignments:
        values[key] = str(resolve_known_placeholders(raw_value, values))

    missing_required: list[str] = []
    for variable in blueprint["variables"]:
        name = str(variable["name"])
        if name in values and values[name].strip():
            continue
        default = variable.get("default")
        if default is not None:
            values[name] = str(resolve_known_placeholders(default, values))
            continue
        if variable.get("required"):
            missing_required.append(name)
            continue
        values[name] = ""

    if missing_required:
        raise RuntimeError(
            "Client blueprint is missing required values for: "
            + ", ".join(sorted(missing_required))
        )

    rendered_client = resolve_known_placeholders(copy.deepcopy(blueprint["client"]), values)
    if not isinstance(rendered_client, dict):
        raise RuntimeError("Expected rendered blueprint client to be a mapping.")

    overlay_client = merge_client_overlay(
        base_client_overlay(
            client_id=client_id,
            client_label=client_label,
            client_root=client_root,
            client_default_cwd=client_default_cwd,
        ),
        rendered_client,
    )
    overlay_client["id"] = client_id
    if explicit_label:
        overlay_client["label"] = client_label
    else:
        overlay_client.setdefault("label", client_label)
    if explicit_default_cwd:
        overlay_client["default_cwd"] = client_default_cwd
    else:
        overlay_client.setdefault("default_cwd", client_default_cwd)
    if "connectors" in overlay_client:
        scaffolded_connectors = scaffold_connector_entries(
            overlay_client.get("connectors"),
            values,
            client_id=client_id,
        )
        if scaffolded_connectors:
            overlay_client["connectors"] = scaffolded_connectors
        else:
            overlay_client.pop("connectors", None)

    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)
    bundle_dir = overlay_dir / "bundles"
    skills_dir = overlay_dir / "skills"

    overlay_path = overlay_dir / "overlay.yaml"
    manifest_path = overlay_dir / "skills.manifest"
    sources_path = overlay_dir / "skills.sources.yaml"
    bundle_readme_path = bundle_dir / "README.md"
    skills_keep_path = skills_dir / ".gitkeep"

    target_files = {
        overlay_path: render_yaml_document({"version": 1, "client": overlay_client}),
        manifest_path: render_client_skill_manifest(
            str(overlay_client["label"]),
            HARDENED_CLIENT_PLANNING_SKILLS,
        ),
        sources_path: render_yaml_document(
            {
                "version": 1,
                "sources": [
                    {
                        "kind": "local",
                        "path": "./skills",
                    }
                ],
            }
        ),
        bundle_readme_path: f"Generated `.skill` bundles for the `{client_id}` client overlay land here.\n",
        skills_keep_path: "",
    }
    target_files.update(client_plan_seed_files(overlay_dir, str(overlay_client["label"])))
    return target_files


def scaffold_client_overlay(
    root_dir: Path,
    client_id: str,
    label: str | None,
    default_cwd: str | None,
    root_path: str | None,
    blueprint_name: str | None,
    blueprint_assignments: list[tuple[str, str]],
    dry_run: bool,
    force: bool,
) -> tuple[list[str], dict[str, Any] | None]:
    client_id = validate_client_id(client_id)
    env_values = load_runtime_env(root_dir)
    client_label = (label or titleize_client_id(client_id)).strip()
    client_root = (root_path or f"${{SKILLBOX_MONOSERVER_ROOT}}/{client_id}").strip()
    client_default_cwd = (default_cwd or client_root).strip()

    if blueprint_name and not blueprint_assignments:
        blueprint_assignments = []

    blueprint_metadata: dict[str, Any] | None = None
    if blueprint_name:
        blueprint_path = resolve_client_blueprint_path(root_dir, blueprint_name)
        blueprint = load_client_blueprint(blueprint_path)
        target_files = build_blueprinted_client_scaffold_files(
            root_dir=root_dir,
            env_values=env_values,
            client_id=client_id,
            client_label=client_label,
            client_root=client_root,
            client_default_cwd=client_default_cwd,
            explicit_label=label is not None,
            explicit_default_cwd=default_cwd is not None,
            blueprint=blueprint,
            blueprint_assignments=blueprint_assignments,
        )
        blueprint_metadata = {
            "id": blueprint["id"],
            "path": blueprint["path"],
        }
    else:
        if blueprint_assignments:
            raise RuntimeError("`--set` requires `--blueprint`.")
        target_files = default_client_scaffold_files(
            root_dir=root_dir,
            env_values=env_values,
            client_id=client_id,
            client_label=client_label,
            client_root=client_root,
            client_default_cwd=client_default_cwd,
        )

    existing_paths = sorted(
        repo_rel(root_dir, path)
        for path in target_files
        if path.exists()
    )
    if existing_paths and not force:
        raise RuntimeError(
            "Client scaffold already exists for "
            f"{client_id}: {', '.join(existing_paths)}. Re-run with --force to overwrite."
        )

    actions: list[str] = []
    for path, content in target_files.items():
        write_text_file(path, content, dry_run=dry_run)
        actions.append(f"write-file: {repo_rel(root_dir, path)}")

    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)
    actions.extend(
        ensure_client_planning_skill_sources(
            root_dir,
            overlay_dir,
            dry_run=dry_run,
        )
    )
    actions.extend(
        ensure_client_planning_skill_bundles(
            root_dir,
            overlay_dir,
            dry_run=dry_run,
        )
    )

    return actions, blueprint_metadata




def find_duplicates(items: list[dict[str, Any]], field: str) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        value = str(item.get(field, "")).strip()
        if not value:
            continue
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def ensure_directory(path: Path, dry_run: bool) -> None:
    if dry_run:
        return
    path.mkdir(parents=True, exist_ok=True)


def artifact_source_configured(artifact: dict[str, Any]) -> bool:
    source = artifact.get("source") or {}
    source_kind = source.get("kind", "manual")
    if source_kind == "url":
        return bool(str(source.get("url") or "").strip())
    if source_kind == "file":
        return bool(str(source.get("host_path") or source.get("path") or "").strip())
    return False


def normalize_sha256(raw_value: Any, *, label: str) -> str:
    value = str(raw_value or "").strip().lower()
    if not value:
        raise RuntimeError(f"{label} is missing")
    if not SHA256_HEX_PATTERN.fullmatch(value):
        raise RuntimeError(f"{label} must be a 64-character hex SHA-256 digest")
    return value


def validate_url_download_source(source: dict[str, Any], *, artifact_id: str) -> tuple[str, str]:
    url = str(source.get("url") or "").strip()
    if not url:
        raise RuntimeError(f"artifact {artifact_id} is url-backed but missing source.url")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise RuntimeError(f"artifact {artifact_id} download url must use https: {url}")

    sha256 = normalize_sha256(source.get("sha256"), label=f"artifact {artifact_id} source.sha256")
    return url, sha256


def remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def tree_hash(entries: list[tuple[str, str]]) -> str:
    hasher = hashlib.sha256()
    for rel_path, digest in sorted(entries):
        hasher.update(rel_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def directory_tree_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_dir():
        return None

    entries: list[tuple[str, str]] = []
    for file_path in sorted(child for child in path.rglob("*") if child.is_file()):
        rel_path = file_path.relative_to(path).as_posix()
        entries.append((rel_path, file_sha256(file_path)))
    return tree_hash(entries)


def read_manifest_skills(path: Path) -> list[str]:
    seen: set[str] = set()
    skills: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line in seen:
            continue
        skills.append(line)
        seen.add(line)
    return skills


def bundle_members(bundle_path: Path, expected_skill_name: str | None = None) -> tuple[str, list[tuple[str, str]]]:
    members: list[tuple[str, str]] = []
    top_levels: set[str] = set()

    with zipfile.ZipFile(bundle_path, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue

            member_path = PurePosixPath(info.filename.replace("\\", "/"))
            if member_path.is_absolute() or ".." in member_path.parts or len(member_path.parts) < 2:
                raise RuntimeError(f"Invalid bundle member in {bundle_path}: {info.filename}")

            top_level = member_path.parts[0]
            top_levels.add(top_level)
            if expected_skill_name and top_level != expected_skill_name:
                raise RuntimeError(
                    f"Bundle {bundle_path.name} does not unpack to the expected skill root {expected_skill_name}"
                )

            rel_path = PurePosixPath(*member_path.parts[1:]).as_posix()
            members.append((rel_path, digest_bytes(archive.read(info))))

    if not members:
        raise RuntimeError(f"Bundle {bundle_path} is empty")
    if len(top_levels) != 1:
        raise RuntimeError(f"Bundle {bundle_path} must contain exactly one top-level skill directory")

    return next(iter(top_levels)), members


def bundle_metadata(bundle_path: Path, expected_skill_name: str | None = None) -> dict[str, Any]:
    archive_root, members = bundle_members(bundle_path, expected_skill_name=expected_skill_name)
    return {
        "name": bundle_path.stem,
        "filename": bundle_path.name,
        "host_path": str(bundle_path),
        "bundle_sha256": file_sha256(bundle_path),
        "bundle_tree_sha256": tree_hash(members),
        "archive_root": archive_root,
        "file_count": len(members),
    }


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return raw


def write_json_file(path: Path, payload: dict[str, Any]) -> bool:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ensure_directory(path.parent, dry_run=False)
    if path.exists() and path.read_text(encoding="utf-8") == serialized:
        return False
    path.write_text(serialized, encoding="utf-8")
    return True


def resolve_client_projection_output_dir(
    root_dir: Path,
    client_id: str,
    raw_output_dir: str | None,
) -> Path:
    if raw_output_dir:
        output_dir = Path(raw_output_dir).expanduser()
        if not output_dir.is_absolute():
            output_dir = (root_dir / output_dir).resolve()
        else:
            output_dir = output_dir.resolve()
        return output_dir
    return (root_dir / CLIENT_PROJECTS_REL / client_id).resolve()


def resolve_client_open_output_dir(
    root_dir: Path,
    client_id: str,
    raw_output_dir: str | None,
) -> Path:
    return resolve_optional_host_dir(
        root_dir,
        raw_output_dir,
        default_rel=CLIENT_OPEN_ROOT_REL / client_id,
    )


def runtime_path_to_projection_rel_path(env_values: dict[str, str], raw_path: str) -> Path:
    workspace_root = Path(env_values["SKILLBOX_WORKSPACE_ROOT"])
    runtime_path = Path(raw_path)
    try:
        relative = runtime_path.relative_to(workspace_root)
    except ValueError as exc:
        raise RuntimeError(
            "client-project only supports runtime files that live under "
            f"{workspace_root}, got {runtime_path}"
        ) from exc
    return Path(relative.as_posix())


def prepare_client_projection_output_dir(
    root_dir: Path,
    output_dir: Path,
    *,
    dry_run: bool,
    force: bool,
) -> list[str]:
    actions: list[str] = []
    default_root = (root_dir / CLIENT_PROJECTS_REL).resolve()
    protected_paths = {
        root_dir.resolve(),
        (root_dir / "workspace").resolve(),
        (root_dir / "default-skills").resolve(),
        (root_dir / ".env-manager").resolve(),
    }

    if output_dir in protected_paths:
        raise RuntimeError(f"Refusing to use protected output directory for client-project: {output_dir}")

    if output_dir.exists():
        if output_dir.is_dir():
            has_contents = any(output_dir.iterdir())
        else:
            has_contents = True

        if has_contents and not force:
            raise RuntimeError(
                f"client-project output already exists at {output_dir}. Re-run with --force to replace it."
            )

        if has_contents and force:
            allow_replace = (output_dir / CLIENT_PROJECTION_METADATA_REL).is_file()
            try:
                output_dir.relative_to(default_root)
                allow_replace = True
            except ValueError:
                pass
            if not allow_replace:
                raise RuntimeError(
                    "client-project output already exists at "
                    f"{output_dir} and is not a projection directory under the default build root."
                )
            actions.append(f"remove-output-dir: {repo_rel(root_dir, output_dir)}")
            if not dry_run:
                remove_path(output_dir)

    if not dry_run:
        ensure_directory(output_dir, dry_run=False)
    return actions


def add_projection_source_file(
    files: dict[str, dict[str, Any]],
    destination_rel: Path,
    source_path: Path,
) -> None:
    normalized_dest = destination_rel.as_posix()
    if normalized_dest in files:
        existing = files[normalized_dest]
        if existing.get("type") == "copy" and Path(str(existing["source_path"])) == source_path:
            return
        raise RuntimeError(f"client-project attempted to write duplicate output file {normalized_dest}")
    if not source_path.is_file():
        raise RuntimeError(f"Required projection source file missing: {source_path}")
    files[normalized_dest] = {
        "type": "copy",
        "destination_rel": normalized_dest,
        "source_path": source_path,
    }


def add_projection_text_file(
    files: dict[str, dict[str, Any]],
    destination_rel: Path,
    content: str,
) -> None:
    normalized_dest = destination_rel.as_posix()
    if normalized_dest in files:
        existing = files[normalized_dest]
        if existing.get("type") == "text" and existing.get("content") == content:
            return
        raise RuntimeError(f"client-project attempted to write duplicate output file {normalized_dest}")
    files[normalized_dest] = {
        "type": "text",
        "destination_rel": normalized_dest,
        "content": content,
    }


def sanitize_projection_env(env_values: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in env_values.items():
        key_upper = str(key).upper()
        if key_upper in {"SKILLBOX_CLIENTS_HOST_ROOT", "SKILLBOX_MONOSERVER_HOST_ROOT"}:
            continue
        if any(marker in key_upper for marker in ("TOKEN", "SECRET", "PASSWORD")):
            continue
        sanitized[str(key)] = value
    return sanitized


def sanitize_projection_source(source: dict[str, Any]) -> dict[str, Any]:
    kind = str(source.get("kind") or "").strip()
    sanitized: dict[str, Any] = {}
    for key, value in source.items():
        key_text = str(key)
        key_upper = key_text.upper()
        if key_text == "host_path":
            continue
        if key_text == "path" and kind in {"bind", "directory", "file", "local", "manual"}:
            continue
        if any(marker in key_upper for marker in ("TOKEN", "SECRET", "PASSWORD")):
            continue
        sanitized[key_text] = sanitize_projection_value(value, key=key_text)
    return sanitized


def sanitize_projection_value(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        if key == "env":
            return sanitize_projection_env(value)
        if key == "source":
            return sanitize_projection_source(value)

        sanitized: dict[str, Any] = {}
        for child_key, child_value in value.items():
            child_key_text = str(child_key)
            child_key_upper = child_key_text.upper()
            if child_key_text.startswith("_"):
                continue
            if child_key_text in {"root_dir", "manifest_file", "host_path"} or child_key_text.endswith("_host_path"):
                continue
            if any(marker in child_key_upper for marker in ("TOKEN", "SECRET", "PASSWORD")):
                continue
            sanitized[child_key_text] = sanitize_projection_value(child_value, key=child_key_text)
        return sanitized

    if isinstance(value, list):
        return [sanitize_projection_value(item, key=key) for item in value]

    return value


def build_projected_runtime_manifest(
    root_dir: Path,
    client_id: str,
    *,
    overlay_present: bool,
) -> dict[str, Any]:
    runtime_doc = copy.deepcopy(load_yaml(runtime_manifest_path(root_dir)))
    selection = runtime_doc.get("selection")
    if selection is None:
        selection = {}
    if not isinstance(selection, dict):
        raise RuntimeError("Expected runtime manifest selection to be a mapping")

    selection_copy = copy.deepcopy(selection)
    selection_copy["default_client"] = client_id
    runtime_doc["selection"] = selection_copy

    raw_clients = runtime_doc.get("clients")
    if raw_clients is not None:
        if not isinstance(raw_clients, list):
            raise RuntimeError("Expected runtime manifest clients to be a list")
        if overlay_present:
            runtime_doc.pop("clients", None)
        else:
            filtered_clients = [
                copy.deepcopy(item)
                for item in raw_clients
                if isinstance(item, dict) and str(item.get("id", "")).strip() == client_id
            ]
            if filtered_clients:
                runtime_doc["clients"] = filtered_clients
            else:
                runtime_doc.pop("clients", None)

    return runtime_doc


def collect_client_projection_files(
    root_dir: Path,
    model: dict[str, Any],
    client_id: str,
) -> tuple[dict[str, dict[str, Any]], str]:
    from .validation import collect_skill_inventory

    env_values = load_runtime_env(root_dir)
    files: dict[str, dict[str, Any]] = {}

    client_overlay_host_path = client_config_host_dir(root_dir, env_values, client_id) / "overlay.yaml"
    overlay_present = client_overlay_host_path.is_file()
    runtime_doc = build_projected_runtime_manifest(
        root_dir,
        client_id,
        overlay_present=overlay_present,
    )
    add_projection_text_file(
        files,
        Path("workspace") / "runtime.yaml",
        render_yaml_document(runtime_doc),
    )

    for optional_rel_path in (
        Path(".env.example"),
        Path("workspace") / "sandbox.yaml",
        Path("workspace") / "dependencies.yaml",
    ):
        source_path = root_dir / optional_rel_path
        if source_path.is_file():
            add_projection_source_file(files, optional_rel_path, source_path)

    if overlay_present:
        overlay_runtime_path = client_config_runtime_dir(env_values, client_id) / "overlay.yaml"
        add_projection_source_file(
            files,
            runtime_path_to_projection_rel_path(env_values, str(overlay_runtime_path)),
            client_overlay_host_path,
        )

    for skillset in model.get("skills") or []:
        inventory = collect_skill_inventory(skillset)
        manifest_host_path = Path(str(skillset["manifest_host_path"]))
        sources_config_host_path = Path(str(skillset["sources_config_host_path"]))
        add_projection_source_file(
            files,
            runtime_path_to_projection_rel_path(env_values, str(skillset["manifest"])),
            manifest_host_path,
        )
        add_projection_source_file(
            files,
            runtime_path_to_projection_rel_path(env_values, str(skillset["sources_config"])),
            sources_config_host_path,
        )

        bundle_dir_runtime_path = PurePosixPath(str(skillset["bundle_dir"]))
        bundle_dir_host_path = Path(str(skillset["bundle_dir_host_path"]))
        bundle_readme_path = bundle_dir_host_path / "README.md"
        if bundle_readme_path.is_file():
            add_projection_source_file(
                files,
                runtime_path_to_projection_rel_path(env_values, str(bundle_dir_runtime_path / "README.md")),
                bundle_readme_path,
            )

        missing_bundles = [
            skill_name
            for skill_name in inventory["expected_skills"]
            if skill_name not in inventory["bundles"]
        ]
        if missing_bundles:
            raise RuntimeError(
                f"Skill set {skillset['id']} is missing bundles for: {', '.join(sorted(missing_bundles))}"
            )

        for skill_name in inventory["expected_skills"]:
            bundle_record = inventory["bundles"][skill_name]
            bundle_filename = str(bundle_record["filename"])
            add_projection_source_file(
                files,
                runtime_path_to_projection_rel_path(env_values, str(bundle_dir_runtime_path / bundle_filename)),
                Path(str(bundle_record["host_path"])),
            )

    sanitized_model = sanitize_projection_value(copy.deepcopy(model))
    add_projection_text_file(
        files,
        CLIENT_PROJECT_RUNTIME_MODEL_REL,
        json.dumps(sanitized_model, indent=2, sort_keys=True) + "\n",
    )

    overlay_mode = "overlay" if overlay_present else "inline"
    return files, overlay_mode


def materialize_client_projection(
    root_dir: Path,
    output_dir: Path,
    files: dict[str, dict[str, Any]],
    *,
    dry_run: bool,
    force: bool,
) -> tuple[list[str], list[tuple[str, str]]]:
    actions = prepare_client_projection_output_dir(
        root_dir,
        output_dir,
        dry_run=dry_run,
        force=force,
    )
    entries: list[tuple[str, str]] = []

    for destination_rel, spec in sorted(files.items()):
        destination_path = output_dir / destination_rel
        ensure_directory(destination_path.parent, dry_run)
        if spec["type"] == "copy":
            source_path = Path(str(spec["source_path"]))
            digest = file_sha256(source_path)
            actions.append(
                f"copy-file: {repo_rel(root_dir, source_path)} -> {repo_rel(root_dir, destination_path)}"
            )
            if not dry_run:
                shutil.copy2(source_path, destination_path)
        else:
            content = str(spec["content"])
            digest = digest_bytes(content.encode("utf-8"))
            actions.append(f"write-file: {repo_rel(root_dir, destination_path)}")
            if not dry_run:
                write_text_file(destination_path, content, dry_run=False)
        entries.append((destination_rel, digest))

    return actions, entries


def project_client_bundle(
    root_dir: Path,
    client_id: str,
    *,
    profiles: list[str] | None = None,
    output_dir_arg: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    from .validation import filter_model, normalize_active_clients, normalize_active_profiles

    cid = validate_client_id(client_id)
    model = build_runtime_model(root_dir)
    active_profiles = normalize_active_profiles(profiles or [])
    active_clients = normalize_active_clients(model, [cid])
    filtered_model = filter_model(model, active_profiles, active_clients)
    output_dir = resolve_client_projection_output_dir(root_dir, cid, output_dir_arg)
    files, overlay_mode = collect_client_projection_files(root_dir, filtered_model, cid)
    actions, payload_entries = materialize_client_projection(
        root_dir,
        output_dir,
        files,
        dry_run=dry_run,
        force=force,
    )
    payload_tree_sha256 = tree_hash(payload_entries)
    projection_payload: dict[str, Any] = {
        "version": CLIENT_PROJECTION_VERSION,
        "client_id": cid,
        "active_profiles": filtered_model.get("active_profiles", []),
        "active_clients": filtered_model.get("active_clients", []),
        "default_client": str((filtered_model.get("selection") or {}).get("default_client") or cid),
        "overlay_mode": overlay_mode,
        "runtime_manifest": "workspace/runtime.yaml",
        "runtime_model": CLIENT_PROJECT_RUNTIME_MODEL_REL.as_posix(),
        "payload_tree_sha256": payload_tree_sha256,
        "files": [
            {"path": rel_path, "sha256": digest}
            for rel_path, digest in sorted(payload_entries)
        ],
    }

    metadata_path = output_dir / CLIENT_PROJECTION_METADATA_REL
    actions.append(f"write-file: {repo_rel(root_dir, metadata_path)}")
    if not dry_run:
        write_json_file(metadata_path, projection_payload)

    return {
        "client_id": cid,
        "output_dir": str(output_dir),
        "dry_run": dry_run,
        "force": force,
        "overlay_mode": overlay_mode,
        "active_profiles": filtered_model.get("active_profiles", []),
        "active_clients": filtered_model.get("active_clients", []),
        "file_count": len(payload_entries),
        "payload_tree_sha256": payload_tree_sha256,
        "files": projection_payload["files"],
        "actions": actions,
        "next_actions": next_actions_for_client_project(cid),
    }


def resolve_optional_host_dir(root_dir: Path, raw_path: str | None, *, default_rel: Path) -> Path:
    value = str(raw_path or "").strip()
    resolved = Path(value) if value else default_rel
    resolved = resolved.expanduser()
    if not resolved.is_absolute():
        return (root_dir / resolved).resolve()
    return resolved.resolve()


def inferred_private_target_dir(root_dir: Path, env_values: dict[str, str] | None = None) -> Path | None:
    resolved_env = env_values or load_runtime_env(root_dir)
    clients_root = client_configs_host_root(root_dir, resolved_env).resolve()
    default_clients_root = (root_dir / "workspace" / "clients").resolve()
    if clients_root == default_clients_root:
        return None
    return clients_root.parent


def ensure_git_repo(path: Path) -> bool:
    from .runtime_ops import git_repo_state

    ensure_directory(path, dry_run=False)
    state = git_repo_state(path)
    if state.get("git"):
        return False

    init_result = run_command(["git", "init"], cwd=path)
    if init_result.returncode != 0:
        raise RuntimeError(init_result.stderr.strip() or init_result.stdout.strip() or f"git init failed for {path}")

    branch_result = run_command(["git", "branch", "-M", "main"], cwd=path)
    if branch_result.returncode != 0:
        raise RuntimeError(
            branch_result.stderr.strip() or branch_result.stdout.strip() or f"git branch setup failed for {path}"
        )
    return True


def migrate_client_overlay_tree(root_dir: Path, source_root: Path, target_root: Path) -> tuple[list[str], list[str]]:
    actions: list[str] = []
    migrated_clients: list[str] = []
    ensure_directory(target_root, dry_run=False)
    if not source_root.is_dir() or source_root.resolve() == target_root.resolve():
        return actions, migrated_clients

    for child in sorted(source_root.iterdir()):
        if not child.is_dir():
            continue
        dest = target_root / child.name
        if dest.exists():
            actions.append(f"skip-client-existing: {repo_rel(root_dir, dest)}")
            continue
        shutil.copytree(child, dest)
        migrated_clients.append(child.name)
        actions.append(f"copy-client: {repo_rel(root_dir, dest)}")
    return actions, migrated_clients


def migrate_client_subtree(
    root_dir: Path,
    source_root: Path,
    target_clients_root: Path,
    *,
    subdir_name: str,
) -> list[str]:
    actions: list[str] = []
    ensure_directory(target_clients_root, dry_run=False)
    if not source_root.is_dir():
        return actions

    for child in sorted(source_root.iterdir()):
        if not child.is_dir():
            continue
        dest = target_clients_root / child.name / subdir_name
        ensure_directory(dest, dry_run=False)
        copied_any = False
        for entry in sorted(child.iterdir()):
            entry_dest = dest / entry.name
            if entry_dest.exists():
                actions.append(f"skip-client-{subdir_name}-entry-existing: {repo_rel(root_dir, entry_dest)}")
                continue
            if entry.is_dir():
                shutil.copytree(entry, entry_dest)
            else:
                shutil.copy2(entry, entry_dest)
            copied_any = True
            actions.append(f"copy-client-{subdir_name}-entry: {repo_rel(root_dir, entry_dest)}")
        if not copied_any and not any(dest.iterdir()):
            actions.append(f"ensure-client-{subdir_name}: {repo_rel(root_dir, dest)}")
    return actions


def ensure_client_overlay_skillset_shape(client_doc: dict[str, Any], client_id: str) -> None:
    skillset_template = copy.deepcopy(base_client_overlay(
        client_id=client_id,
        client_label=titleize_client_id(client_id),
        client_root=f"${{SKILLBOX_MONOSERVER_ROOT}}/{client_id}",
        client_default_cwd=f"${{SKILLBOX_MONOSERVER_ROOT}}/{client_id}",
    )["skills"][0])

    raw_skills = client_doc.setdefault("skills", [])
    if not isinstance(raw_skills, list):
        raise RuntimeError("Expected client.skills to be a list.")

    target_skillset: dict[str, Any] | None = None
    for skillset in raw_skills:
        if not isinstance(skillset, dict):
            continue
        skillset_id = str(skillset.get("id") or "").strip()
        if skillset_id == f"{client_id}-skills" or str(skillset.get("kind") or "").strip() == "packaged-skill-set":
            target_skillset = skillset
            break

    if target_skillset is None:
        target_skillset = {}
        raw_skills.append(target_skillset)

    for key, value in skillset_template.items():
        if key in {"install_targets", "sync"}:
            target_skillset[key] = copy.deepcopy(value)
        elif key not in target_skillset:
            target_skillset[key] = copy.deepcopy(value)
        else:
            target_skillset[key] = copy.deepcopy(value)


def ensure_client_overlay_context_shape(client_doc: dict[str, Any], client_default_cwd: str) -> None:
    raw_context = client_doc.setdefault("context", {})
    if not isinstance(raw_context, dict):
        raise RuntimeError("Expected client.context to be a mapping.")

    raw_cwd_match = raw_context.get("cwd_match")
    if not isinstance(raw_cwd_match, list):
        raw_cwd_match = []
    normalized_cwd_match = [
        str(value).strip()
        for value in raw_cwd_match
        if str(value).strip()
    ]
    if client_default_cwd not in normalized_cwd_match:
        normalized_cwd_match.append(client_default_cwd)
    raw_context["cwd_match"] = normalized_cwd_match or [client_default_cwd]

    raw_plans = raw_context.setdefault("plans", {})
    if not isinstance(raw_plans, dict):
        raise RuntimeError("Expected client.context.plans to be a mapping.")
    for key, value in HARDENED_CLIENT_PLAN_PATHS.items():
        raw_plans[key] = value


def ensure_client_overlay_sources_shape(sources_path: Path) -> str:
    if sources_path.is_file():
        raw = load_yaml(sources_path)
        if not isinstance(raw, dict):
            raise RuntimeError(f"Expected a mapping in {sources_path}")
        version = int(raw.get("version") or 1)
        raw_sources = raw.get("sources") or []
        if not isinstance(raw_sources, list):
            raise RuntimeError(f"Expected `sources` to be a list in {sources_path}")
        sources: list[dict[str, Any]] = []
        has_local_skills = False
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            normalized = copy.deepcopy(item)
            if (
                str(normalized.get("kind") or "").strip() == "local"
                and str(normalized.get("path") or "").strip() == "./skills"
            ):
                has_local_skills = True
            sources.append(normalized)
        if not has_local_skills:
            sources.insert(0, {"kind": "local", "path": "./skills"})
        payload = {"version": version, "sources": sources}
        return render_yaml_document(payload)

    return render_yaml_document(
        {
            "version": 1,
            "sources": [{"kind": "local", "path": "./skills"}],
        }
    )


def normalize_client_overlay_shape(root_dir: Path, overlay_dir: Path) -> list[str]:
    overlay_path = overlay_dir / "overlay.yaml"
    if not overlay_path.is_file():
        return []

    overlay_doc = load_yaml(overlay_path)
    if not isinstance(overlay_doc, dict):
        raise RuntimeError(f"Expected a mapping in {overlay_path}")
    client_doc = overlay_doc.setdefault("client", {})
    if not isinstance(client_doc, dict):
        raise RuntimeError(f"Expected a mapping at client in {overlay_path}")

    client_id = validate_client_id(str(client_doc.get("id") or overlay_dir.name))
    client_label = str(client_doc.get("label") or titleize_client_id(client_id)).strip() or titleize_client_id(client_id)
    client_default_cwd = str(
        client_doc.get("default_cwd")
        or f"${{SKILLBOX_MONOSERVER_ROOT}}/{client_id}"
    ).strip()
    client_doc["id"] = client_id
    client_doc["label"] = client_label
    client_doc["default_cwd"] = client_default_cwd

    ensure_client_overlay_skillset_shape(client_doc, client_id)
    ensure_client_overlay_context_shape(client_doc, client_default_cwd)

    actions: list[str] = []
    rendered_overlay = render_yaml_document(overlay_doc)
    existing_overlay = overlay_path.read_text(encoding="utf-8")
    if existing_overlay != rendered_overlay:
        overlay_path.write_text(rendered_overlay, encoding="utf-8")
        actions.append(f"normalize-overlay: {repo_rel(root_dir, overlay_path)}")

    manifest_path = overlay_dir / "skills.manifest"
    existing_skills = read_manifest_skills(manifest_path) if manifest_path.is_file() else []
    manifest_text = render_client_skill_manifest(
        client_label,
        HARDENED_CLIENT_PLANNING_SKILLS,
        existing_skills,
    )
    if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != manifest_text:
        ensure_directory(manifest_path.parent, dry_run=False)
        manifest_path.write_text(manifest_text, encoding="utf-8")
        actions.append(f"write-file: {repo_rel(root_dir, manifest_path)}")

    sources_path = overlay_dir / "skills.sources.yaml"
    sources_text = ensure_client_overlay_sources_shape(sources_path)
    if not sources_path.is_file() or sources_path.read_text(encoding="utf-8") != sources_text:
        ensure_directory(sources_path.parent, dry_run=False)
        sources_path.write_text(sources_text, encoding="utf-8")
        actions.append(f"write-file: {repo_rel(root_dir, sources_path)}")

    bundle_readme_path = overlay_dir / "bundles" / "README.md"
    bundle_readme_text = f"Generated `.skill` bundles for the `{client_id}` client overlay land here.\n"
    ensure_directory(bundle_readme_path.parent, dry_run=False)
    if not bundle_readme_path.is_file() or bundle_readme_path.read_text(encoding="utf-8") != bundle_readme_text:
        bundle_readme_path.write_text(bundle_readme_text, encoding="utf-8")
        actions.append(f"write-file: {repo_rel(root_dir, bundle_readme_path)}")

    skills_keep_path = overlay_dir / "skills" / ".gitkeep"
    ensure_directory(skills_keep_path.parent, dry_run=False)
    if not skills_keep_path.exists():
        skills_keep_path.write_text("", encoding="utf-8")
        actions.append(f"write-file: {repo_rel(root_dir, skills_keep_path)}")

    actions.extend(
        ensure_client_planning_skill_sources(
            root_dir,
            overlay_dir,
            dry_run=False,
        )
    )
    actions.extend(
        ensure_client_planning_skill_bundles(
            root_dir,
            overlay_dir,
            dry_run=False,
        )
    )

    for seed_path, content in client_plan_seed_files(overlay_dir, client_label).items():
        ensure_directory(seed_path.parent, dry_run=False)
        if seed_path.is_file():
            if seed_path.name == "INDEX.md" and seed_path.read_text(encoding="utf-8").strip():
                continue
            if seed_path.read_text(encoding="utf-8") == content:
                continue
        seed_path.write_text(content, encoding="utf-8")
        actions.append(f"write-file: {repo_rel(root_dir, seed_path)}")

    return actions


def init_private_repo(root_dir: Path, *, target_dir_arg: str | None = None) -> dict[str, Any]:
    env_values = load_runtime_env(root_dir)
    current_clients_root = client_configs_host_root(root_dir, env_values).resolve()
    target_dir = resolve_optional_host_dir(root_dir, target_dir_arg, default_rel=DEFAULT_PRIVATE_REPO_REL)
    target_clients_root = (target_dir / "clients").resolve()

    actions: list[str] = []
    ensure_directory(target_dir, dry_run=False)
    actions.append(f"ensure-dir: {repo_rel(root_dir, target_dir)}")
    if ensure_git_repo(target_dir):
        actions.append(f"git-init: {repo_rel(root_dir, target_dir)}")
    else:
        actions.append(f"git-repo-present: {repo_rel(root_dir, target_dir)}")

    ensure_directory(target_clients_root, dry_run=False)
    actions.append(f"ensure-dir: {repo_rel(root_dir, target_clients_root)}")

    migrate_actions, migrated_clients = migrate_client_overlay_tree(
        root_dir,
        current_clients_root,
        target_clients_root,
    )
    actions.extend(migrate_actions)
    actions.extend(
        migrate_client_subtree(
            root_dir,
            root_dir / "default-skills" / "clients",
            target_clients_root,
            subdir_name="bundles",
        )
    )
    actions.extend(
        migrate_client_subtree(
            root_dir,
            root_dir / "skills" / "clients",
            target_clients_root,
            subdir_name="skills",
        )
    )
    for child in sorted(target_clients_root.iterdir()):
        if not child.is_dir():
            continue
        actions.extend(normalize_client_overlay_shape(root_dir, child))

    clients_host_root_value = normalize_host_rel_path(root_dir, target_clients_root)
    env_changed = upsert_env_file_values(
        root_dir / ".env",
        {"SKILLBOX_CLIENTS_HOST_ROOT": clients_host_root_value},
    )
    actions.append(f"{'write' if env_changed else 'keep'}-env: .env")

    return {
        "target_dir": str(target_dir),
        "clients_host_root": str(target_clients_root),
        "env_updates": {"SKILLBOX_CLIENTS_HOST_ROOT": clients_host_root_value},
        "migrated_clients": migrated_clients,
        "actions": actions,
        "next_actions": next_actions_for_private_init(),
    }


def resolve_client_publish_target_dir(root_dir: Path, raw_target_dir: str | None) -> Path:
    target_value = str(raw_target_dir or "").strip()
    if target_value:
        return resolve_optional_host_dir(root_dir, target_value, default_rel=DEFAULT_PRIVATE_REPO_REL)

    inferred = inferred_private_target_dir(root_dir)
    if inferred is None:
        raise RuntimeError(
            "No private publish target configured. Run private-init to attach a private repo or pass --target-dir."
        )
    return inferred


def resolve_client_publish_bundle_dir(root_dir: Path, raw_bundle_dir: str) -> Path:
    bundle_dir = Path(raw_bundle_dir).expanduser()
    if not bundle_dir.is_absolute():
        bundle_dir = (root_dir / bundle_dir).resolve()
    else:
        bundle_dir = bundle_dir.resolve()
    return bundle_dir


def git_head_commit(path: Path) -> str | None:
    result = run_command(["git", "rev-parse", "HEAD"], cwd=path)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def git_dirty_paths(path: Path) -> list[str]:
    result = run_command(["git", "status", "--short"], cwd=path)
    if result.returncode != 0:
        return []

    paths: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1].strip()
        if entry:
            paths.append(entry)
    return paths


def directory_file_entries(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if not path.is_dir():
        return entries

    for file_path in sorted(child for child in path.rglob("*") if child.is_file()):
        entries.append((file_path.relative_to(path).as_posix(), file_sha256(file_path)))
    return entries


def normalize_bundle_rel_path(raw_value: Any, *, label: str) -> str:
    value = str(raw_value or "").strip()
    if not value:
        raise RuntimeError(f"{label} is missing")

    rel_path = PurePosixPath(value)
    if rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.parts:
        raise RuntimeError(f"{label} must be a relative path inside the bundle")
    return rel_path.as_posix()


def load_client_projection_bundle(bundle_dir: Path, *, expected_client_id: str) -> dict[str, Any]:
    if not bundle_dir.is_dir():
        raise RuntimeError(f"Bundle directory not found: {bundle_dir}")

    projection_path = bundle_dir / CLIENT_PROJECTION_METADATA_REL
    if not projection_path.is_file():
        raise RuntimeError(f"Bundle directory is missing projection.json: {bundle_dir}")

    projection_payload = load_json_file(projection_path)
    bundle_client_id = str(projection_payload.get("client_id") or "").strip()
    if bundle_client_id != expected_client_id:
        raise RuntimeError(
            f"Bundle at {bundle_dir} is for client {bundle_client_id or '(unknown)'!r}, "
            f"not {expected_client_id!r}"
        )

    payload_tree_sha256 = normalize_sha256(
        projection_payload.get("payload_tree_sha256"),
        label=f"bundle {bundle_dir} payload_tree_sha256",
    )

    raw_files = projection_payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise RuntimeError(f"Bundle projection metadata is missing files[]: {projection_path}")

    payload_entries: list[tuple[str, str]] = []
    for index, raw_item in enumerate(raw_files):
        if not isinstance(raw_item, dict):
            raise RuntimeError(f"Bundle projection file entry {index} must be an object")

        rel_path = normalize_bundle_rel_path(
            raw_item.get("path"),
            label=f"bundle {bundle_dir} files[{index}].path",
        )
        expected_sha = normalize_sha256(
            raw_item.get("sha256"),
            label=f"bundle {bundle_dir} files[{index}].sha256",
        )
        file_path = bundle_dir / Path(*PurePosixPath(rel_path).parts)
        if not file_path.is_file():
            raise RuntimeError(f"Bundle payload file is missing: {rel_path}")

        actual_sha = file_sha256(file_path)
        if actual_sha != expected_sha:
            raise RuntimeError(f"Bundle payload file hash mismatch for {rel_path}")

        payload_entries.append((rel_path, actual_sha))

    if tree_hash(payload_entries) != payload_tree_sha256:
        raise RuntimeError(f"Bundle payload tree hash mismatch for {bundle_dir}")

    runtime_manifest_rel = normalize_bundle_rel_path(
        projection_payload.get("runtime_manifest", Path("workspace") / "runtime.yaml"),
        label=f"bundle {bundle_dir} runtime_manifest",
    )
    runtime_model_rel = normalize_bundle_rel_path(
        projection_payload.get("runtime_model", CLIENT_PROJECT_RUNTIME_MODEL_REL),
        label=f"bundle {bundle_dir} runtime_model",
    )

    for required_rel in (
        CLIENT_PROJECTION_METADATA_REL.as_posix(),
        runtime_manifest_rel,
        runtime_model_rel,
    ):
        required_path = bundle_dir / Path(*PurePosixPath(required_rel).parts)
        if not required_path.is_file():
            raise RuntimeError(f"Bundle file is missing: {required_rel}")

    all_entries = directory_file_entries(bundle_dir)
    if not all_entries:
        raise RuntimeError(f"Bundle directory is empty: {bundle_dir}")

    return {
        "bundle_dir": str(bundle_dir),
        "client_id": expected_client_id,
        "projection": projection_payload,
        "payload_entries": payload_entries,
        "payload_tree_sha256": payload_tree_sha256,
        "runtime_manifest_rel": runtime_manifest_rel,
        "runtime_model_rel": runtime_model_rel,
        "all_entries": all_entries,
    }


CLIENT_RUNTIME_DIFF_SECTIONS = (
    "clients",
    "repos",
    "artifacts",
    "env_files",
    "skills",
    "tasks",
    "services",
    "logs",
    "checks",
)

CLIENT_PUBLISH_METADATA_COMPARE_FIELDS = (
    "version",
    "client_id",
    "source_commit",
    "projection_version",
    "overlay_mode",
    "active_profiles",
    "active_clients",
    "default_client",
    "payload_tree_sha256",
    "file_count",
    "current_dir",
    "projection",
    "runtime_manifest",
    "runtime_model",
)
CLIENT_ACCEPTANCE_MATCH_FIELDS = (
    "version",
    "client_id",
    "source_commit",
    "payload_tree_sha256",
    "active_profiles",
    "ready",
    "doctor_post",
    "services",
    "mcp_servers",
)












def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))






if __name__ == "__main__":
    sys.exit(main())
