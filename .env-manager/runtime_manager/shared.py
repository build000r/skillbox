#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import datetime
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
    PERSISTENCE_ERROR_CODES,
    PersistenceContractError,
    build_runtime_model,
    client_config_host_dir,
    client_config_runtime_dir,
    client_configs_host_root,
    compile_persistence_summary,
    host_path_to_absolute_path,
    load_yaml,
    load_runtime_env,
    runtime_manifest_path,
    runtime_path_to_host_path,
    storage_binding_by_id,
)


VALID_REPO_SOURCE_KINDS = {"bind", "directory", "git", "manual"}
VALID_SYNC_MODES = {"external", "ensure-directory", "clone-if-missing", "manual"}
VALID_ARTIFACT_SOURCE_KINDS = {"file", "manual", "url"}
VALID_ARTIFACT_SYNC_MODES = {"copy-if-missing", "download-if-missing", "manual"}
VALID_ENV_FILE_SOURCE_KINDS = {"file", "manual"}
VALID_ENV_FILE_SYNC_MODES = {"write", "manual"}
VALID_SKILL_SYNC_MODES = {"clone-and-install"}
VALID_HEALTHCHECK_TYPES = {"http", "path_exists", "process_running", "port"}
VALID_CHECK_TYPES = {"path_exists"}
VALID_TASK_SUCCESS_TYPES = {"path_exists", "all_outputs_exist", "port_listening"}
LOCKFILE_VERSION = 1
SKILL_REPOS_LOCKFILE_VERSION = 2
SKILL_REPOS_CONFIG_VERSION = 2
DEFAULT_SKILLIGNORE_PATTERNS = [
    ".git/",
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
    "modes/",
    "briefs/",
]
CLONE_DIR_ROOT_REL = Path("workspace") / "skill-repos"
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
CLIENT_DEPLOY_VERSION = 1
CLIENT_PUBLISH_ROOT_REL = Path("clients")
CLIENT_PUBLISH_CURRENT_REL = Path("current")
CLIENT_PUBLISH_METADATA_REL = Path("publish.json")
CLIENT_ACCEPTANCE_METADATA_REL = Path("acceptance.json")
CLIENT_DEPLOY_METADATA_REL = Path("deploy.json")
CLIENT_DEPLOY_ARTIFACTS_REL = Path("artifacts")
DEFAULT_PRIVATE_REPO_REL = Path("..") / "skillbox-config"
CLIENT_PLANNING_SKILL_TEMPLATE_REL = Path("workspace") / "client-planning-skills"
CLIENT_SKILL_BUILDER_TEMPLATE_REL = Path("workspace") / "client-skill-builder-skills"
CLIENT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BLUEPRINT_VARIABLE_PATTERN = re.compile(r"^[A-Z0-9_]+$")
SCAFFOLD_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
SHA256_HEX_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
DEFAULT_SERVICE_START_WAIT_SECONDS = 30.0
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
    "SKILLBOX_INGRESS_ROUTE_FILE",
    "SKILLBOX_INGRESS_NGINX_CONFIG",
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
HARDENED_CLIENT_SKILL_BUILDER_SKILLS = [
    "skill-issue",
    "prompt-reviewer",
]
HARDENED_CLIENT_HYBRID_SKILLS = (
    HARDENED_CLIENT_PLANNING_SKILLS
    + HARDENED_CLIENT_SKILL_BUILDER_SKILLS
)
HARDENED_CLIENT_PLAN_PATHS = {
    "plan_root": "plans/released",
    "plan_draft": "plans/draft",
    "plan_index": "plans/INDEX.md",
    "session_plans": "plans/sessions",
}
SESSION_SCHEMA_VERSION = 1
SESSION_ACTIVE_STATUS = "active"
SESSION_TERMINAL_STATUSES = {"completed", "failed", "abandoned"}
HARDENED_CLIENT_SKILL_BUILDER_CONTEXT = {
    "workflow_builder": {
        "workflow_root": "workflows",
        "workflow_index": "workflows/INDEX.md",
        "evaluation_root": "evaluations",
        "evaluation_notes": "evaluations/README.md",
        "invocation_root": "invocations",
        "invocation_notes": "invocations/README.md",
        "observability_root": "observability",
        "observability_notes": "observability/README.md",
        "extraction_rule": "workflows/EXTRACTION.md",
    }
}
CLIENT_OVERLAY_PROJECTION_ROOT_FILES = (
    "skill-repos.lock.json",
)
CLIENT_OVERLAY_PROJECTION_DIRS = (
    "skills",
    "plans",
    "workflows",
    "evaluations",
    "invocations",
    "observability",
)


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
    lower_msg = msg.lower()
    persistence_code = str(getattr(exc, "code", "") or "").strip()

    if isinstance(exc, PersistenceContractError) or persistence_code in PERSISTENCE_ERROR_CODES:
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

    if "client-init requires" in msg:
        return structured_error(
            msg,
            error_type="missing_argument",
            recovery_hint="Provide a client_id argument or use --list-blueprints.",
            next_actions=["client-init --list-blueprints --format json"],
        )
    if "blueprint" in lower_msg and "not found" in lower_msg:
        return structured_error(
            msg,
            error_type="blueprint_not_found",
            recovery_hint="List available blueprints, then retry with a valid name or path.",
            next_actions=["client-init --list-blueprints --format json"],
        )
    if (
        ("required" in lower_msg and "variable" in lower_msg)
        or "missing required values" in lower_msg
    ):
        return structured_error(
            msg,
            error_type="missing_variable",
            recovery_hint="Add the missing --set KEY=VALUE assignments and retry.",
        )
    if (
        "already exists" in lower_msg
        or "without force" in lower_msg
        or "already_exists" in lower_msg
        or "non-projection output directory" in lower_msg
    ):
        return structured_error(
            msg,
            error_type="conflict",
            recovery_hint="Use --force to overwrite existing files, or choose a different client id.",
        )
    if "target repo has a dirty working tree" in lower_msg:
        return structured_error(
            msg,
            error_type="conflict",
            recovery_hint="Commit or discard changes in the target repo, then retry.",
        )
    if "no private publish target configured" in lower_msg:
        return structured_error(
            msg,
            error_type="missing_target_repo",
            recovery_hint="Run private-init to attach a private repo, or pass --target-dir explicitly.",
            next_actions=["private-init --format json"],
        )
    if "target must be a git repo" in lower_msg:
        return structured_error(
            msg,
            error_type="invalid_target_repo",
            recovery_hint="Initialize the target repo with git before publishing.",
        )
    if "env file" in lower_msg and ("missing" in lower_msg or "unresolved" in lower_msg):
        return structured_error(
            msg,
            error_type="missing_env_file",
            recovery_hint="Create the env source file or run sync first.",
            next_actions=[f"sync --format json"],
        )
    if "failed to become healthy" in lower_msg:
        return structured_error(
            msg,
            error_type="service_health_failure",
            recovery_hint="Check service logs for the root cause, then restart.",
            next_actions=["logs --format json", "doctor --format json"],
        )
    if "invalid client id" in lower_msg:
        return structured_error(
            msg,
            error_type="invalid_client_id",
            recoverable=True,
            recovery_hint="Client IDs must be lowercase alphanumeric with single hyphens: my-project.",
        )
    if "unknown client scaffold pack" in lower_msg:
        return structured_error(
            msg,
            error_type="invalid_scaffold_pack",
            recoverable=True,
            recovery_hint="Use a supported scaffold pack such as `planning`, `skill-builder`, or `hybrid`.",
            next_actions=["client-init --list-blueprints --format json"],
        )
    if "session_id is required" in lower_msg or "session event_type is required" in lower_msg:
        return structured_error(
            msg,
            error_type="missing_argument",
            recoverable=True,
            recovery_hint="Provide the required session_id and event_type arguments, then retry.",
        )
    if "session not found" in lower_msg:
        return structured_error(
            msg,
            error_type="session_not_found",
            recoverable=True,
            recovery_hint="List recent sessions for the client, then retry with a valid session_id.",
            next_actions=["session-status <client> --format json", "focus <client> --format json"],
        )
    if (
        "session is not active" in lower_msg
        or "session is already active" in lower_msg
        or "unsupported session status" in lower_msg
    ):
        return structured_error(
            msg,
            error_type="session_state_conflict",
            recoverable=True,
            recovery_hint="Inspect the session state first, then resume or end it with a valid transition.",
            next_actions=["session-status <client> --format json"],
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
    elif command in ("session-start", "session-event", "session-end", "session-resume", "session-status"):
        fallback_next = ["focus --format json", "status --format json"]

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
# Runtime event log (plain-text, replaces structured JSONL journal)
# ---------------------------------------------------------------------------

RUNTIME_LOG_REL = Path("logs") / "runtime" / "runtime.log"
MCP_EVENT_CONTEXT_ENV = "SKILLBOX_MCP_EVENT_CONTEXT"
DEFAULT_EVENT_FEED_LIMIT = 50
DEFAULT_EVENT_FEED_POLL_INTERVAL_SECONDS = 0.25


def current_mcp_event_context() -> dict[str, Any]:
    raw = str(os.environ.get(MCP_EVENT_CONTEXT_ENV) or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def merge_runtime_event_detail(detail: dict[str, Any] | None = None) -> dict[str, Any] | None:
    merged = dict(detail or {})
    context = current_mcp_event_context()
    for key, value in context.items():
        merged.setdefault(str(key), value)
    return merged or None


def log_runtime_event(
    event_type: str,
    subject: str,
    detail: dict[str, Any] | None = None,
    root_dir: Path = DEFAULT_ROOT_DIR,
) -> None:
    """Append a human-readable line to the runtime log. Best-effort."""
    log_path = root_dir / RUNTIME_LOG_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    detail = merge_runtime_event_detail(detail)
    detail_str = f" {json.dumps(detail, separators=(',', ':'), default=str)}" if detail else ""
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} {event_type} {subject}{detail_str}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Durable session runtime
# ---------------------------------------------------------------------------

def _session_now() -> float:
    return time.time()


def _session_subject(client_id: str, session_id: str) -> str:
    return f"{client_id}:{session_id}"


def _normalize_session_event_type(event_type: str) -> str:
    normalized = str(event_type or "").strip()
    if not normalized:
        raise RuntimeError("session event_type is required")
    if not normalized.startswith("session."):
        normalized = f"session.{normalized}"
    return normalized


def _generate_session_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{stamp}-{os.urandom(3).hex()}"


def _ensure_client_exists(root_dir: Path, client_id: str) -> str:
    cid = validate_client_id(client_id)
    _env_values, overlay_path, overlay_runtime_path = client_overlay_location(root_dir, cid)
    if not overlay_path.is_file():
        raise RuntimeError(
            f"Client '{cid}' has no overlay at {overlay_runtime_path}. "
            f"Use 'onboard {cid}' to scaffold it first."
        )
    return cid


def resolve_client_log_root(root_dir: Path, client_id: str) -> Path:
    cid = _ensure_client_exists(root_dir, client_id)
    default_path = root_dir / "logs" / "clients" / cid
    try:
        model = build_runtime_model(root_dir)
    except RuntimeError:
        return default_path

    candidates = [
        Path(str(log_item["host_path"]))
        for log_item in model.get("logs") or []
        if str(log_item.get("client") or "").strip() == cid and log_item.get("host_path")
    ]
    if not candidates:
        return default_path

    ranked = sorted(
        candidates,
        key=lambda path: (0 if path.name == cid else 1, len(path.parts)),
    )
    return ranked[0]


def resolve_client_session_root(root_dir: Path, client_id: str) -> Path:
    return resolve_client_log_root(root_dir, client_id) / "sessions"


def session_paths(root_dir: Path, client_id: str, session_id: str) -> dict[str, Path]:
    sessions_root = resolve_client_session_root(root_dir, client_id)
    session_dir = sessions_root / session_id
    return {
        "sessions_root": sessions_root,
        "session_dir": session_dir,
        "meta_path": session_dir / "meta.json",
        "events_path": session_dir / "events.jsonl",
        "handoff_path": session_dir / "handoff.md",
    }


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    ensure_directory(path.parent, dry_run=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")


def _write_session_handoff(
    path: Path,
    *,
    client_id: str,
    session_id: str,
    label: str,
    cwd: str,
    goal: str,
    summary: str = "",
) -> None:
    lines = [
        f"# Session Handoff: {session_id}",
        "",
        f"- Client: {client_id}",
        f"- Label: {label or '-'}",
        f"- CWD: {cwd or '-'}",
        f"- Goal: {goal or '-'}",
    ]
    if summary:
        lines += ["", "## Summary", "", summary]
    write_text_file(path, "\n".join(lines).rstrip() + "\n", dry_run=False)


def _read_session_meta(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Session not found: {path.parent.name}")
    return load_json_file(path)


def _read_session_events(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    if limit <= 0:
        return events
    return events[-limit:]


def _session_payload(
    root_dir: Path,
    meta: dict[str, Any],
    *,
    include_recent_events: bool = True,
    recent_event_limit: int = 10,
) -> dict[str, Any]:
    client_id = str(meta.get("client_id") or "").strip()
    session_id = str(meta.get("session_id") or "").strip()
    paths = session_paths(root_dir, client_id, session_id)
    payload = dict(meta)
    payload["paths"] = {
        "dir": repo_rel(root_dir, paths["session_dir"]),
        "meta": repo_rel(root_dir, paths["meta_path"]),
        "events": repo_rel(root_dir, paths["events_path"]),
        "handoff": repo_rel(root_dir, paths["handoff_path"]),
    }
    if include_recent_events:
        payload["recent_events"] = _read_session_events(paths["events_path"], limit=recent_event_limit)
    return payload


def list_client_sessions(
    root_dir: Path,
    client_id: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    cid = _ensure_client_exists(root_dir, client_id)
    sessions_root = resolve_client_session_root(root_dir, cid)
    if not sessions_root.is_dir():
        return []

    sessions: list[dict[str, Any]] = []
    for meta_path in sessions_root.glob("*/meta.json"):
        try:
            meta = _read_session_meta(meta_path)
        except RuntimeError:
            continue
        if str(meta.get("client_id") or "").strip() != cid:
            continue
        sessions.append(_session_payload(root_dir, meta, include_recent_events=False))

    sessions.sort(
        key=lambda item: float(item.get("updated_at") or item.get("started_at") or 0),
        reverse=True,
    )
    if limit <= 0:
        return sessions
    return sessions[:limit]


def read_client_session(
    root_dir: Path,
    client_id: str,
    session_id: str,
    *,
    recent_event_limit: int = 10,
) -> dict[str, Any]:
    cid = _ensure_client_exists(root_dir, client_id)
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise RuntimeError("session_id is required")
    paths = session_paths(root_dir, cid, normalized_session_id)
    meta = _read_session_meta(paths["meta_path"])
    if str(meta.get("client_id") or "").strip() != cid:
        raise RuntimeError(f"Session not found: {normalized_session_id}")
    return _session_payload(
        root_dir,
        meta,
        include_recent_events=True,
        recent_event_limit=recent_event_limit,
    )


def _persist_session_event(
    root_dir: Path,
    meta: dict[str, Any],
    event_type: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_type = _normalize_session_event_type(event_type)
    now = _session_now()
    client_id = str(meta["client_id"])
    session_id = str(meta["session_id"])
    paths = session_paths(root_dir, client_id, session_id)
    detail = merge_runtime_event_detail(detail)
    payload = {
        "ts": now,
        "type": normalized_type,
        "client_id": client_id,
        "session_id": session_id,
        "detail": detail or {},
    }
    _append_jsonl(paths["events_path"], payload)

    meta["updated_at"] = now
    meta["last_event_at"] = now
    meta["last_heartbeat_at"] = now
    meta["last_event_type"] = normalized_type
    meta["event_count"] = int(meta.get("event_count") or 0) + 1
    message = str((detail or {}).get("message") or "").strip()
    if message:
        meta["last_message"] = message
    write_json_file(paths["meta_path"], meta)

    journal_detail = {"client_id": client_id, "session_id": session_id}
    if detail:
        journal_detail.update(detail)
    log_runtime_event(normalized_type, _session_subject(client_id, session_id), journal_detail, root_dir)
    return payload


def start_client_session(
    root_dir: Path,
    client_id: str,
    *,
    label: str = "",
    cwd: str = "",
    goal: str = "",
    actor: str = "",
) -> dict[str, Any]:
    cid = _ensure_client_exists(root_dir, client_id)
    session_id = _generate_session_id()
    paths = session_paths(root_dir, cid, session_id)
    if paths["session_dir"].exists():
        raise RuntimeError(f"Session already exists: {session_id}")

    now = _session_now()
    meta = {
        "version": SESSION_SCHEMA_VERSION,
        "session_id": session_id,
        "client_id": cid,
        "status": SESSION_ACTIVE_STATUS,
        "label": str(label or "").strip(),
        "cwd": str(cwd or "").strip(),
        "goal": str(goal or "").strip(),
        "actor": str(actor or "").strip(),
        "created_at": now,
        "started_at": now,
        "updated_at": now,
        "last_event_at": now,
        "last_heartbeat_at": now,
        "completed_at": None,
        "event_count": 0,
        "resume_count": 0,
        "last_event_type": "",
        "last_message": "",
        "summary": "",
    }
    ensure_directory(paths["session_dir"], dry_run=False)
    write_json_file(paths["meta_path"], meta)
    _write_session_handoff(
        paths["handoff_path"],
        client_id=cid,
        session_id=session_id,
        label=meta["label"],
        cwd=meta["cwd"],
        goal=meta["goal"],
    )
    _persist_session_event(
        root_dir,
        meta,
        "session.started",
        {
            "label": meta["label"],
            "cwd": meta["cwd"],
            "goal": meta["goal"],
            "actor": meta["actor"],
        },
    )
    return {
        "client_id": cid,
        "session": read_client_session(root_dir, cid, session_id),
        "next_actions": next_actions_for_session_start(cid, session_id),
    }


def append_client_session_event(
    root_dir: Path,
    client_id: str,
    session_id: str,
    *,
    event_type: str,
    message: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cid = _ensure_client_exists(root_dir, client_id)
    normalized_session_id = str(session_id or "").strip()
    paths = session_paths(root_dir, cid, normalized_session_id)
    meta = _read_session_meta(paths["meta_path"])
    if str(meta.get("client_id") or "").strip() != cid:
        raise RuntimeError(f"Session not found: {normalized_session_id}")
    if str(meta.get("status") or "") != SESSION_ACTIVE_STATUS:
        raise RuntimeError(f"Session is not active: {normalized_session_id}")

    event_detail = dict(detail or {})
    if message:
        event_detail["message"] = message
    _persist_session_event(root_dir, meta, event_type, event_detail)
    return {
        "client_id": meta["client_id"],
        "session": read_client_session(root_dir, str(meta["client_id"]), normalized_session_id),
        "next_actions": next_actions_for_session_event(str(meta["client_id"]), normalized_session_id),
    }


def end_client_session(
    root_dir: Path,
    client_id: str,
    session_id: str,
    *,
    final_status: str = "completed",
    summary: str = "",
) -> dict[str, Any]:
    normalized_status = str(final_status or "").strip() or "completed"
    if normalized_status not in SESSION_TERMINAL_STATUSES:
        raise RuntimeError(
            f"Unsupported session status {normalized_status!r}. "
            f"Use one of: {', '.join(sorted(SESSION_TERMINAL_STATUSES))}."
        )

    cid = _ensure_client_exists(root_dir, client_id)
    normalized_session_id = str(session_id or "").strip()
    paths = session_paths(root_dir, cid, normalized_session_id)
    meta = _read_session_meta(paths["meta_path"])
    if str(meta.get("client_id") or "").strip() != cid:
        raise RuntimeError(f"Session not found: {normalized_session_id}")
    if str(meta.get("status") or "") != SESSION_ACTIVE_STATUS:
        raise RuntimeError(f"Session is not active: {normalized_session_id}")

    now = _session_now()
    meta["status"] = normalized_status
    meta["completed_at"] = now
    meta["summary"] = str(summary or "").strip()
    write_json_file(paths["meta_path"], meta)
    _persist_session_event(
        root_dir,
        meta,
        "session.ended",
        {"status": normalized_status, "message": meta["summary"]},
    )
    _write_session_handoff(
        paths["handoff_path"],
        client_id=str(meta["client_id"]),
        session_id=normalized_session_id,
        label=str(meta.get("label") or ""),
        cwd=str(meta.get("cwd") or ""),
        goal=str(meta.get("goal") or ""),
        summary=str(meta.get("summary") or ""),
    )
    return {
        "client_id": meta["client_id"],
        "session": read_client_session(root_dir, str(meta["client_id"]), normalized_session_id),
        "next_actions": next_actions_for_session_end(str(meta["client_id"]), normalized_session_id),
    }


def resume_client_session(
    root_dir: Path,
    client_id: str,
    session_id: str,
    *,
    actor: str = "",
    message: str = "",
) -> dict[str, Any]:
    cid = _ensure_client_exists(root_dir, client_id)
    normalized_session_id = str(session_id or "").strip()
    paths = session_paths(root_dir, cid, normalized_session_id)
    meta = _read_session_meta(paths["meta_path"])
    if str(meta.get("client_id") or "").strip() != cid:
        raise RuntimeError(f"Session not found: {normalized_session_id}")
    if str(meta.get("status") or "") == SESSION_ACTIVE_STATUS:
        raise RuntimeError(f"Session is already active: {normalized_session_id}")

    previous_status = str(meta.get("status") or "unknown")
    meta["status"] = SESSION_ACTIVE_STATUS
    meta["completed_at"] = None
    meta["resume_count"] = int(meta.get("resume_count") or 0) + 1
    meta["last_resumed_from"] = previous_status
    if actor:
        meta["actor"] = actor
    write_json_file(paths["meta_path"], meta)
    detail = {"from": previous_status}
    if actor:
        detail["actor"] = actor
    if message:
        detail["message"] = message
    _persist_session_event(root_dir, meta, "session.resumed", detail)
    return {
        "client_id": meta["client_id"],
        "session": read_client_session(root_dir, str(meta["client_id"]), normalized_session_id),
        "next_actions": next_actions_for_session_resume(str(meta["client_id"]), normalized_session_id),
    }


def session_status_payload(
    root_dir: Path,
    client_id: str,
    *,
    session_id: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    cid = _ensure_client_exists(root_dir, client_id)
    if session_id:
        return {
            "client_id": cid,
            "session": read_client_session(root_dir, cid, session_id),
            "next_actions": next_actions_for_session_status(cid, session_id),
        }

    sessions = list_client_sessions(root_dir, cid, limit=limit)
    return {
        "client_id": cid,
        "sessions": sessions,
        "count": len(sessions),
        "next_actions": next_actions_for_session_status(cid, None),
    }


def _runtime_log_timestamp(raw_ts: str) -> float:
    dt = datetime.datetime.strptime(raw_ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def parse_runtime_log_line(line: str, *, line_number: int) -> dict[str, Any] | None:
    text = str(line or "").strip()
    if not text:
        return None
    parts = text.split(" ", 3)
    if len(parts) < 3:
        return None
    raw_ts, event_type, subject = parts[:3]
    try:
        ts = _runtime_log_timestamp(raw_ts)
    except ValueError:
        return None

    detail: dict[str, Any] = {}
    if len(parts) == 4 and parts[3].strip():
        try:
            parsed = json.loads(parts[3])
            if isinstance(parsed, dict):
                detail = parsed
        except json.JSONDecodeError:
            detail = {"raw_detail": parts[3]}

    client_id = str(detail.get("client_id") or "").strip()
    session_id = str(detail.get("session_id") or "").strip()
    message = str(detail.get("message") or "").strip()
    return {
        "source": "runtime_log",
        "line_number": line_number,
        "ts": ts,
        "time": raw_ts + "Z",
        "type": event_type,
        "subject": subject,
        "client_id": client_id or None,
        "session_id": session_id or None,
        "message": message or None,
        "detail": detail,
    }


def _event_matches_scope(
    event: dict[str, Any],
    *,
    client_id: str | None,
    session_id: str | None,
) -> bool:
    target_client = str(client_id or "").strip()
    target_session = str(session_id or "").strip()
    event_client = str(event.get("client_id") or "").strip()
    event_session = str(event.get("session_id") or "").strip()
    subject = str(event.get("subject") or "").strip()

    if target_session:
        return (
            event_session == target_session
            or subject.endswith(f":{target_session}")
        )
    if target_client:
        return (
            event_client == target_client
            or subject == target_client
            or subject.startswith(f"{target_client}:")
        )
    return True


def read_runtime_log_events(
    root_dir: Path,
    *,
    client_id: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    log_path = root_dir / RUNTIME_LOG_REL
    if not log_path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(log_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        parsed = parse_runtime_log_line(raw_line, line_number=line_number)
        if parsed is None:
            continue
        if _event_matches_scope(parsed, client_id=client_id, session_id=session_id):
            events.append(parsed)
    return events


def _session_event_roots(root_dir: Path, client_id: str | None = None) -> list[Path]:
    normalized_client = str(client_id or "").strip()
    if normalized_client:
        try:
            return [resolve_client_session_root(root_dir, normalized_client)]
        except RuntimeError:
            return []

    roots: list[Path] = []
    for base in (
        root_dir / ".skillbox-state" / "logs" / "clients",
        root_dir / "logs" / "clients",
    ):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir():
                roots.append(child / "sessions")
    return roots


def read_durable_session_events(
    root_dir: Path,
    *,
    client_id: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    target_session = str(session_id or "").strip()
    events: list[dict[str, Any]] = []
    for sessions_root in _session_event_roots(root_dir, client_id=client_id):
        if not sessions_root.is_dir():
            continue
        pattern = f"{target_session}/events.jsonl" if target_session else "*/events.jsonl"
        for events_path in sessions_root.glob(pattern):
            meta_path = events_path.parent / "meta.json"
            meta: dict[str, Any] = {}
            if meta_path.is_file():
                try:
                    meta = load_json_file(meta_path)
                except RuntimeError:
                    meta = {}
            fallback_client = str(meta.get("client_id") or events_path.parent.parent.parent.name).strip()
            fallback_session = str(meta.get("session_id") or events_path.parent.name).strip()
            for line_number, raw_line in enumerate(
                events_path.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                detail = item.get("detail")
                if not isinstance(detail, dict):
                    detail = {}
                event = {
                    "source": "session",
                    "line_number": line_number,
                    "ts": float(item.get("ts") or 0.0),
                    "time": datetime.datetime.fromtimestamp(
                        float(item.get("ts") or 0.0),
                        tz=datetime.timezone.utc,
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "type": str(item.get("type") or "").strip(),
                    "subject": f"{fallback_client}:{fallback_session}",
                    "client_id": str(item.get("client_id") or fallback_client).strip() or None,
                    "session_id": str(item.get("session_id") or fallback_session).strip() or None,
                    "message": str(detail.get("message") or "").strip() or None,
                    "detail": detail,
                    "paths": {
                        "events": repo_rel(root_dir, events_path),
                        "meta": repo_rel(root_dir, meta_path),
                    },
                }
                if _event_matches_scope(event, client_id=client_id, session_id=session_id):
                    events.append(event)
    return events


def event_feed_payload(
    root_dir: Path,
    *,
    client_id: str | None = None,
    session_id: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_EVENT_FEED_LIMIT,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    normalized_client = str(client_id or "").strip() or None
    normalized_session = str(session_id or "").strip() or None
    try:
        cursor_index = max(0, int(str(cursor or "0")))
    except ValueError:
        cursor_index = 0
    max_items = max(1, int(limit or DEFAULT_EVENT_FEED_LIMIT))
    deadline = time.monotonic() + max(0.0, float(wait_seconds))

    while True:
        events = read_runtime_log_events(
            root_dir,
            client_id=normalized_client,
            session_id=normalized_session,
        ) + read_durable_session_events(
            root_dir,
            client_id=normalized_client,
            session_id=normalized_session,
        )
        events.sort(
            key=lambda item: (
                float(item.get("ts") or 0.0),
                str(item.get("source") or ""),
                int(item.get("line_number") or 0),
                str(item.get("subject") or ""),
            )
        )
        total = len(events)
        safe_cursor = min(cursor_index, total)
        if total > safe_cursor or time.monotonic() >= deadline or wait_seconds <= 0:
            window = events[safe_cursor:safe_cursor + max_items]
            next_cursor = safe_cursor + len(window)
            return {
                "client_id": normalized_client,
                "session_id": normalized_session,
                "cursor": str(safe_cursor),
                "next_cursor": str(next_cursor),
                "returned": len(window),
                "total_events": total,
                "has_more": total > next_cursor,
                "events": window,
            }
        time.sleep(DEFAULT_EVENT_FEED_POLL_INTERVAL_SECONDS)


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

    raw_scaffold = raw.get("scaffold") or {}
    if raw_scaffold is None:
        scaffold = {}
    elif isinstance(raw_scaffold, dict):
        scaffold = copy.deepcopy(raw_scaffold)
    else:
        raise RuntimeError(f"Expected `scaffold` to be a mapping in {path}")

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
        "scaffold": scaffold,
        "client": client,
    }


def base_client_overlay(
    client_id: str,
    client_label: str,
    client_root: str,
    client_default_cwd: str,
    *,
    scaffold_pack: str = "planning",
) -> dict[str, Any]:
    overlay = {
        "id": client_id,
        "label": client_label,
        "default_cwd": client_default_cwd,
        "scaffold": {
            "pack": scaffold_pack,
        },
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
                "kind": "skill-repo-set",
                "required": False,
                "profiles": ["core"],
                "skill_repos_config": f"${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skill-repos.yaml",
                "lock_path": f"${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skill-repos.lock.json",
                "clone_root": "${SKILLBOX_WORKSPACE_ROOT}/workspace/skill-repos",
                "sync": {"mode": "clone-and-install"},
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
    ensure_client_overlay_context_shape(overlay, client_default_cwd, scaffold_pack)
    return overlay


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


def render_skill_builder_index(client_label: str) -> str:
    return (
        f"# {client_label} Workflow Index\n"
        "\n"
        "| Workflow | Status | Scope | Notes |\n"
        "|---|---|---|---|\n"
    )


def render_skill_builder_extraction_rule() -> str:
    return (
        "# Workflow Extraction Rule\n"
        "\n"
        "Use this rule when deciding whether a workflow stays product-local or moves upward.\n"
        "\n"
        "## Keep In The Product Repo\n"
        "\n"
        "- The workflow uses product-specific nouns, data contracts, or client policies.\n"
        "- The workflow is only proven in one product or one client.\n"
        "- The runtime depends on product-specific repo structure or business logic.\n"
        "\n"
        "## Extract To `opensource/skills`\n"
        "\n"
        "- The reusable part is a portable agent workflow, review loop, or operator playbook.\n"
        "- A second real consumer exists, or reuse pressure is already causing duplicated maintenance.\n"
        "- The skill contract can be described without product-specific business nouns.\n"
        "\n"
        "## Keep In `skillbox`\n"
        "\n"
        "- The problem is runtime behavior: installation, sync, bundle curation, client overlays, box behavior, or operator tooling.\n"
        "- The reusable piece is connector/runtime delivery rather than the portable skill contract itself.\n"
        "\n"
        "## Use A Cross-Repo Slice\n"
        "\n"
        "- Put the portable skill contract in `opensource/skills`.\n"
        "- Put runtime/distribution/FWC integration in `skillbox`.\n"
        "- Keep product-specific workflow execution and business data in the product repo.\n"
    )


def client_skill_builder_seed_files(overlay_dir: Path, client_label: str) -> dict[Path, str]:
    return {
        overlay_dir / "workflows" / "INDEX.md": render_skill_builder_index(client_label),
        overlay_dir / "workflows" / "EXTRACTION.md": render_skill_builder_extraction_rule(),
        overlay_dir / "evaluations" / "README.md": (
            "# Evaluations\n"
            "\n"
            "Store scorecards, evaluation runs, regression notes, and acceptance snapshots here.\n"
        ),
        overlay_dir / "invocations" / "README.md": (
            "# Invocations\n"
            "\n"
            "Track copied transcript slices, invocation summaries, or pointers to raw workflow runs here.\n"
        ),
        overlay_dir / "observability" / "README.md": (
            "# Observability\n"
            "\n"
            "Record connector probes, health notes, drift findings, and workflow diagnostics here.\n"
        ),
    }


def client_skill_builder_template_root() -> Path:
    return (DEFAULT_ROOT_DIR / CLIENT_SKILL_BUILDER_TEMPLATE_REL).resolve()


def client_scaffold_pack(pack_name: str | None) -> str:
    pack = str(pack_name or "planning").strip() or "planning"
    if pack in {"planning", "skill-builder", "hybrid"}:
        return pack
    raise RuntimeError(
        f"Unknown client scaffold pack: {pack}. Supported packs: planning, skill-builder, hybrid."
    )


def client_scaffold_pack_required_skills(pack_name: str | None) -> list[str]:
    pack = client_scaffold_pack(pack_name)
    if pack == "planning":
        return copy.deepcopy(HARDENED_CLIENT_PLANNING_SKILLS)
    if pack == "skill-builder":
        return copy.deepcopy(HARDENED_CLIENT_SKILL_BUILDER_SKILLS)
    return copy.deepcopy(HARDENED_CLIENT_HYBRID_SKILLS)


def client_scaffold_pack_skill_templates(pack_name: str | None) -> list[tuple[str, Path]]:
    pack = client_scaffold_pack(pack_name)
    template_pairs: list[tuple[str, Path]] = []
    if pack in {"planning", "hybrid"}:
        planning_root = client_planning_skill_template_root()
        template_pairs.extend(
            (skill_name, planning_root / skill_name)
            for skill_name in HARDENED_CLIENT_PLANNING_SKILLS
        )
    if pack in {"skill-builder", "hybrid"}:
        skill_builder_root = client_skill_builder_template_root()
        template_pairs.extend(
            (skill_name, skill_builder_root / skill_name)
            for skill_name in HARDENED_CLIENT_SKILL_BUILDER_SKILLS
        )
    return template_pairs


def client_scaffold_seed_files(
    overlay_dir: Path,
    client_label: str,
    pack_name: str | None,
) -> dict[Path, str]:
    pack = client_scaffold_pack(pack_name)
    if pack == "planning":
        return client_plan_seed_files(overlay_dir, client_label)
    if pack == "skill-builder":
        return client_skill_builder_seed_files(overlay_dir, client_label)
    seed_files = client_plan_seed_files(overlay_dir, client_label)
    seed_files.update(client_skill_builder_seed_files(overlay_dir, client_label))
    return seed_files


def sync_client_scaffold_seed_files(
    root_dir: Path,
    overlay_dir: Path,
    client_label: str,
    scaffold_pack: str,
    *,
    dry_run: bool,
) -> list[str]:
    actions: list[str] = []
    for seed_path, content in client_scaffold_seed_files(overlay_dir, client_label, scaffold_pack).items():
        ensure_directory(seed_path.parent, dry_run=dry_run)
        if seed_path.is_file():
            existing = seed_path.read_text(encoding="utf-8")
            if existing == content:
                continue
            if existing.strip():
                continue
        if not dry_run:
            seed_path.write_text(content, encoding="utf-8")
        actions.append(f"write-file: {repo_rel(root_dir, seed_path)}")
    return actions


def ensure_client_scaffold_skill_sources(
    root_dir: Path,
    overlay_dir: Path,
    scaffold_pack: str,
    *,
    dry_run: bool,
) -> list[str]:
    actions: list[str] = []
    skills_root = overlay_dir / "skills"
    ensure_directory(skills_root, dry_run=dry_run)
    for skill_name, source_dir in client_scaffold_pack_skill_templates(scaffold_pack):
        if not source_dir.is_dir():
            raise RuntimeError(f"Missing scaffold skill template for {skill_name} at {source_dir}")
        target_dir = skills_root / skill_name
        if target_dir.exists():
            continue
        if not dry_run:
            shutil.copytree(source_dir, target_dir)
        actions.append(f"copy-skill-template: {repo_rel(root_dir, target_dir)}")
    return actions


def default_client_scaffold_files(
    root_dir: Path,
    env_values: dict[str, str],
    client_id: str,
    client_label: str,
    client_root: str,
    client_default_cwd: str,
) -> tuple[dict[Path, str], str]:
    scaffold_pack = "planning"
    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)
    skills_dir = overlay_dir / "skills"

    overlay_path = overlay_dir / "overlay.yaml"
    skill_repos_path = overlay_dir / "skill-repos.yaml"
    skills_keep_path = skills_dir / ".gitkeep"
    overlay_client = base_client_overlay(
        client_id=client_id,
        client_label=client_label,
        client_root=client_root,
        client_default_cwd=client_default_cwd,
        scaffold_pack=scaffold_pack,
    )

    required_skills = client_scaffold_pack_required_skills(scaffold_pack)
    pick_line = ", ".join(required_skills)
    target_files = {
        overlay_path: render_yaml_document({"version": 1, "client": overlay_client}),
        skill_repos_path: (
            f"# {client_label} client-specific skill repos.\n"
            "\n"
            "version: 2\n"
            "\n"
            "skill_repos:\n"
            "  - path: ./skills\n"
            f"    pick: [{pick_line}]\n"
        ),
        skills_keep_path: "",
    }
    target_files.update(client_scaffold_seed_files(overlay_dir, client_label, scaffold_pack))
    return target_files, scaffold_pack


def merge_client_overlay(base_client: dict[str, Any], blueprint_client: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base_client)
    additive_sections = (
        "repo_roots",
        "repos",
        "artifacts",
        "env_files",
        "skills",
        "tasks",
        "services",
        "logs",
        "checks",
        "ingress_routes",
    )
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
) -> tuple[dict[Path, str], str]:
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
    scaffold = copy.deepcopy(blueprint.get("scaffold") or {})
    if scaffold:
        overlay_client["scaffold"] = scaffold
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
    scaffold_pack = ensure_client_overlay_scaffold_shape(overlay_client)
    ensure_client_overlay_skillset_shape(overlay_client, client_id)
    ensure_client_overlay_context_shape(
        overlay_client,
        str(overlay_client["default_cwd"]),
        scaffold_pack,
    )

    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)
    skills_dir = overlay_dir / "skills"

    overlay_path = overlay_dir / "overlay.yaml"
    skill_repos_path = overlay_dir / "skill-repos.yaml"
    skills_keep_path = skills_dir / ".gitkeep"

    required_skills = client_scaffold_pack_required_skills(scaffold_pack)
    pick_line = ", ".join(required_skills)
    target_files = {
        overlay_path: render_yaml_document({"version": 1, "client": overlay_client}),
        skill_repos_path: (
            f"# {str(overlay_client['label'])} client-specific skill repos.\n"
            "\n"
            "version: 2\n"
            "\n"
            "skill_repos:\n"
            "  - path: ./skills\n"
            f"    pick: [{pick_line}]\n"
        ),
        skills_keep_path: "",
    }
    target_files.update(
        client_scaffold_seed_files(
            overlay_dir,
            str(overlay_client["label"]),
            scaffold_pack,
        )
    )
    return target_files, scaffold_pack


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
    client_root = (root_path or "${SKILLBOX_MONOSERVER_ROOT}").strip()
    client_default_cwd = (default_cwd or client_root).strip()

    if blueprint_name and not blueprint_assignments:
        blueprint_assignments = []

    blueprint_metadata: dict[str, Any] | None = None
    if blueprint_name:
        blueprint_path = resolve_client_blueprint_path(root_dir, blueprint_name)
        blueprint = load_client_blueprint(blueprint_path)
        target_files, scaffold_pack = build_blueprinted_client_scaffold_files(
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
        target_files, scaffold_pack = default_client_scaffold_files(
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
        ensure_client_scaffold_skill_sources(
            root_dir,
            overlay_dir,
            scaffold_pack,
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


# ---------------------------------------------------------------------------
# Skill repo config: loading, validation, clone, filtered-copy, lock
# ---------------------------------------------------------------------------


def load_skill_repos_config(config_path: Path) -> dict[str, Any]:
    """Load and validate a skill_repos YAML config file."""
    if not config_path.is_file():
        raise RuntimeError(f"SKILL_CONFIG_INVALID: config file missing at {config_path}")
    raw = load_yaml(config_path)
    if not isinstance(raw, dict):
        raise RuntimeError(f"SKILL_CONFIG_INVALID: expected a YAML mapping in {config_path}")
    version = raw.get("version")
    if version != SKILL_REPOS_CONFIG_VERSION:
        raise RuntimeError(
            f"SKILL_CONFIG_INVALID: expected version {SKILL_REPOS_CONFIG_VERSION}, got {version!r} in {config_path}"
        )
    entries = raw.get("skill_repos")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise RuntimeError(f"SKILL_CONFIG_INVALID: skill_repos must be a list in {config_path}")

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"SKILL_CONFIG_INVALID: skill_repos[{i}] must be a mapping")
        has_repo = bool(entry.get("repo"))
        has_path = bool(entry.get("path"))
        if has_repo == has_path:
            raise RuntimeError(
                f"SKILL_CONFIG_INVALID: skill_repos[{i}] must have exactly one of 'repo' or 'path'"
            )
        if has_repo and not entry.get("ref"):
            raise RuntimeError(f"SKILL_CONFIG_INVALID: skill_repos[{i}] repo entry requires a 'ref'")
        pick = entry.get("pick")
        if pick is not None and not isinstance(pick, list):
            raise RuntimeError(f"SKILL_CONFIG_INVALID: skill_repos[{i}] pick must be a list")

    return raw


def clone_dir_name(repo: str) -> str:
    """Convert 'owner/repo' to 'owner-repo' for clone directory naming."""
    return repo.replace("/", "-")


def _load_skillignore(skill_dir: Path) -> list[str]:
    """Load .skillignore patterns from a skill directory, falling back to defaults."""
    patterns = list(DEFAULT_SKILLIGNORE_PATTERNS)
    ignore_file = skill_dir / ".skillignore"
    if ignore_file.is_file():
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in patterns:
                patterns.append(line)
    return patterns


def _matches_skillignore(rel_path: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any skillignore pattern."""
    import fnmatch

    parts = rel_path.split("/")
    for pattern in patterns:
        if pattern.endswith("/"):
            dir_pattern = pattern.rstrip("/")
            for part in parts[:-1]:
                if fnmatch.fnmatch(part, dir_pattern):
                    return True
            if fnmatch.fnmatch(parts[-1], dir_pattern) and len(parts) > 0:
                pass
        else:
            if fnmatch.fnmatch(parts[-1], pattern):
                return True
            if fnmatch.fnmatch(rel_path, pattern):
                return True
    return False


def filtered_copy_skill(source_dir: Path, target_dir: Path) -> str:
    """Copy a skill directory to target, respecting .skillignore. Returns tree SHA."""
    patterns = _load_skillignore(source_dir)

    remove_path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    for source_file in sorted(source_dir.rglob("*")):
        if not source_file.is_file():
            continue
        rel = source_file.relative_to(source_dir).as_posix()

        if _matches_skillignore(rel, patterns):
            continue

        if rel == ".skillignore":
            continue

        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source_file), str(dest))

    tree_sha = directory_tree_sha256(target_dir)
    if tree_sha is None:
        raise RuntimeError(f"Failed to hash installed skill directory {target_dir}")
    return tree_sha


def _resolve_skill_dirs(
    entry: dict[str, Any],
    source_root: Path,
    repo_name: str,
) -> list[tuple[str, Path]]:
    """Resolve skill name -> source directory pairs from a config entry.

    Returns list of (skill_name, skill_source_dir) tuples.
    """
    pick = entry.get("pick")
    if pick:
        results = []
        for skill_name in pick:
            skill_dir = source_root / skill_name
            if not (skill_dir / "SKILL.md").is_file():
                raise RuntimeError(
                    f"SKILL_NOT_FOUND_IN_REPO: skill '{skill_name}' not found in {source_root} "
                    f"(no SKILL.md at {skill_dir})"
                )
            results.append((skill_name, skill_dir))
        return results

    if (source_root / "SKILL.md").is_file():
        return [(repo_name, source_root)]

    raise RuntimeError(
        f"SKILL_CONFIG_INVALID: repo {entry.get('repo', source_root)} has no pick list "
        "and no SKILL.md at root. Add a pick list or ensure SKILL.md exists at the repo root."
    )


def _clone_or_fetch_repo(
    repo: str,
    ref: str,
    clone_root: Path,
    *,
    dry_run: bool,
) -> tuple[str, Path, str | None]:
    """Clone or fetch a repo. Returns (action, clone_path, resolved_commit_or_None)."""
    dir_name = clone_dir_name(repo)
    clone_path = clone_root / dir_name

    if clone_path.is_dir():
        if not dry_run:
            status_result = run_command(["git", "status", "--porcelain"], cwd=clone_path)
            if status_result.returncode == 0 and status_result.stdout.strip():
                return ("SKILL_REPO_DIRTY", clone_path, None)

            fetch_result = run_command(["git", "fetch", "origin"], cwd=clone_path)
            if fetch_result.returncode != 0:
                raise RuntimeError(
                    f"SKILL_REPO_CLONE_FAILED: git fetch failed for {repo}: "
                    f"{fetch_result.stderr.strip()}"
                )

            checkout_result = run_command(["git", "checkout", ref], cwd=clone_path)
            if checkout_result.returncode != 0:
                run_command(["git", "checkout", f"origin/{ref}"], cwd=clone_path)

            pull_result = run_command(["git", "pull", "--ff-only"], cwd=clone_path)

            rev_result = run_command(["git", "rev-parse", "HEAD"], cwd=clone_path)
            commit = rev_result.stdout.strip() if rev_result.returncode == 0 else None
        else:
            commit = None
        return ("fetched", clone_path, commit)

    if dry_run:
        return ("cloned", clone_path, None)

    clone_root.mkdir(parents=True, exist_ok=True)
    clone_url = f"https://github.com/{repo}.git"
    clone_result = run_command(
        ["git", "clone", clone_url, str(clone_path)],
    )
    if clone_result.returncode != 0:
        ssh_url = f"git@github.com:{repo}.git"
        clone_result = run_command(
            ["git", "clone", ssh_url, str(clone_path)],
        )
        if clone_result.returncode != 0:
            raise RuntimeError(
                f"SKILL_REPO_UNREACHABLE: failed to clone {repo}: "
                f"{clone_result.stderr.strip()}"
            )

    if ref != "main" and ref != "master":
        run_command(["git", "checkout", ref], cwd=clone_path)

    rev_result = run_command(["git", "rev-parse", "HEAD"], cwd=clone_path)
    commit = rev_result.stdout.strip() if rev_result.returncode == 0 else None
    return ("cloned", clone_path, commit)


def sync_skill_repo_sets(model: dict[str, Any], dry_run: bool) -> list[str]:
    """Sync skill-repo-set skill sets: clone repos, filtered-copy skills, write lock."""
    actions: list[str] = []

    for skillset in model["skills"]:
        if skillset.get("kind") != "skill-repo-set":
            continue
        sync_mode = (skillset.get("sync") or {}).get("mode", "")
        if sync_mode != "clone-and-install":
            continue

        config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
        lock_path = Path(str(skillset.get("lock_path_host_path", "")))
        clone_root = Path(str(skillset.get("clone_root_host_path", "")))

        config = load_skill_repos_config(config_path)
        entries = config.get("skill_repos") or []

        for target in skillset.get("install_targets") or []:
            target_root = Path(str(target["host_path"]))
            ensure_directory(target_root, dry_run)

        repo_actions: list[dict[str, Any]] = []
        skills_installed: list[dict[str, Any]] = []
        lock_skills: list[dict[str, Any]] = []

        for entry in entries:
            has_repo = bool(entry.get("repo"))

            if has_repo:
                repo = entry["repo"]
                ref = entry["ref"]
                action, clone_path, commit = _clone_or_fetch_repo(
                    repo, ref, clone_root, dry_run=dry_run,
                )
                repo_actions.append({
                    "repo": repo,
                    "action": action,
                    "ref": ref,
                    "commit": commit,
                })
                actions.append(f"skill-repo-{action}: {repo}")

                if action == "SKILL_REPO_DIRTY":
                    actions.append(f"SKILL_REPO_DIRTY: {repo} — skipping (uncommitted changes)")
                    continue

                source_root = clone_path
                repo_name = repo.split("/")[-1] if "/" in repo else repo

                if not source_root.is_dir():
                    pick = entry.get("pick") or [repo_name]
                    for skill_name in pick:
                        for target in skillset.get("install_targets") or []:
                            target_root = Path(str(target["host_path"]))
                            actions.append(f"install-skill: {skill_name} -> {target_root / skill_name}")
                    continue
            else:
                local_path = entry["path"]
                if not Path(local_path).is_absolute():
                    source_root = (config_path.parent / local_path).resolve()
                else:
                    source_root = Path(local_path)

                if not source_root.is_dir():
                    if dry_run:
                        actions.append(f"skip-local-path: {source_root} (not found)")
                        continue
                    raise RuntimeError(
                        f"SKILL_CONFIG_INVALID: local path does not exist: {source_root}"
                    )
                repo_name = source_root.name
                commit = None
                repo = None

            skill_dirs = _resolve_skill_dirs(entry, source_root, repo_name)

            for skill_name, skill_source in skill_dirs:
                targets_installed: list[str] = []
                install_tree_shas: dict[str, str] = {}

                for target in skillset.get("install_targets") or []:
                    target_root = Path(str(target["host_path"]))
                    install_dir = target_root / skill_name

                    if dry_run:
                        actions.append(f"install-skill: {skill_name} -> {install_dir}")
                        targets_installed.append(target["id"])
                        continue

                    tree_sha = filtered_copy_skill(skill_source, install_dir)
                    install_tree_shas[target["id"]] = tree_sha
                    targets_installed.append(target["id"])
                    actions.append(f"install-skill: {skill_name} -> {install_dir}")

                skills_installed.append({
                    "name": skill_name,
                    "source": repo or str(entry.get("path", "")),
                    "targets": targets_installed,
                })

                lock_entry: dict[str, Any] = {
                    "name": skill_name,
                    "declared_ref": entry.get("ref"),
                    "resolved_commit": commit,
                }
                if repo:
                    lock_entry["repo"] = repo
                else:
                    lock_entry["source_path"] = str(entry.get("path", ""))

                if not dry_run and install_tree_shas:
                    first_target = next(iter(install_tree_shas))
                    lock_entry["install_tree_sha"] = install_tree_shas[first_target]

                lock_skills.append(lock_entry)

        if dry_run:
            actions.append(f"write-lockfile: {lock_path}")
            continue

        new_config_sha = file_sha256(config_path)
        synced_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Preserve synced_at from existing lock when semantic content is unchanged
        if lock_path.is_file():
            try:
                existing_lock = json.loads(lock_path.read_text(encoding="utf-8"))
                existing_skills = {
                    (s.get("name"), s.get("resolved_commit"), s.get("install_tree_sha"))
                    for s in existing_lock.get("skills") or []
                }
                new_skills = {
                    (s.get("name"), s.get("resolved_commit"), s.get("install_tree_sha"))
                    for s in lock_skills
                }
                if (
                    existing_lock.get("config_sha") == new_config_sha
                    and existing_skills == new_skills
                ):
                    synced_at = existing_lock.get("synced_at", synced_at)
            except (json.JSONDecodeError, KeyError):
                pass

        lock_payload = {
            "version": SKILL_REPOS_LOCKFILE_VERSION,
            "config_sha": new_config_sha,
            "synced_at": synced_at,
            "skills": lock_skills,
        }
        changed = write_json_file(lock_path, lock_payload)
        actions.append(f"{'write-lockfile' if changed else 'lockfile-unchanged'}: {lock_path}")

    return actions


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


def add_projection_source_tree(
    files: dict[str, dict[str, Any]],
    destination_root_rel: Path,
    source_root: Path,
) -> None:
    if not source_root.exists():
        return
    if source_root.is_file():
        add_projection_source_file(files, destination_root_rel, source_root)
        return
    for source_path in sorted(child for child in source_root.rglob("*") if child.is_file()):
        add_projection_source_file(
            files,
            destination_root_rel / source_path.relative_to(source_root),
            source_path,
        )


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
        Path("workspace") / "persistence.yaml",
    ):
        source_path = root_dir / optional_rel_path
        if source_path.is_file():
            add_projection_source_file(files, optional_rel_path, source_path)

    if overlay_present:
        client_overlay_host_dir = client_overlay_host_path.parent
        overlay_runtime_path = client_config_runtime_dir(env_values, client_id) / "overlay.yaml"
        overlay_runtime_dir = client_config_runtime_dir(env_values, client_id)
        overlay_projection_dir = runtime_path_to_projection_rel_path(env_values, str(overlay_runtime_dir))
        add_projection_source_file(
            files,
            runtime_path_to_projection_rel_path(env_values, str(overlay_runtime_path)),
            client_overlay_host_path,
        )
        for file_name in CLIENT_OVERLAY_PROJECTION_ROOT_FILES:
            source_path = client_overlay_host_dir / file_name
            if source_path.is_file():
                add_projection_source_file(
                    files,
                    overlay_projection_dir / file_name,
                    source_path,
                )
        for dir_name in CLIENT_OVERLAY_PROJECTION_DIRS:
            source_dir = client_overlay_host_dir / dir_name
            if source_dir.exists():
                add_projection_source_tree(
                    files,
                    overlay_projection_dir / dir_name,
                    source_dir,
                )

    for skillset in model.get("skills") or []:
        if skillset.get("kind") == "skill-repo-set":
            config_host_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
            if config_host_path.is_file():
                add_projection_source_file(
                    files,
                    runtime_path_to_projection_rel_path(env_values, str(skillset["skill_repos_config"])),
                    config_host_path,
                )
            lock_host_path = Path(str(skillset.get("lock_path_host_path", "")))
            if lock_host_path.is_file():
                add_projection_source_file(
                    files,
                    runtime_path_to_projection_rel_path(env_values, str(skillset["lock_path"])),
                    lock_host_path,
                )
            continue

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
    if isinstance(sanitized_model.get("storage"), dict):
        storage_summary = sanitized_model["storage"]
        raw_state_root = str(storage_summary.get("raw_state_root") or "").strip()
        if raw_state_root:
            storage_summary["state_root"] = raw_state_root
        else:
            storage_summary.pop("state_root", None)
    persistence_manifest = root_dir / "workspace" / "persistence.yaml"
    if persistence_manifest.is_file():
        sanitized_model["persistence_manifest_file"] = "/workspace/persistence.yaml"
    else:
        sanitized_model.pop("persistence_manifest_file", None)
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
    default_clients_roots = {
        (root_dir / "workspace" / "clients").resolve(),
    }
    try:
        storage = compile_persistence_summary(root_dir, resolved_env)
    except RuntimeError:
        storage = None
    binding = storage_binding_by_id(storage, "clients-root")
    if binding is not None:
        relative_path = str(binding.get("relative_path") or "").strip()
        state_root = str(storage.get("state_root") or "").strip() if storage else ""
        if relative_path and state_root:
            default_clients_roots.add((Path(state_root) / Path(relative_path)).resolve())
    if clients_root in default_clients_roots:
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


def ensure_client_overlay_scaffold_shape(client_doc: dict[str, Any]) -> str:
    raw_scaffold = client_doc.get("scaffold") or {}
    if raw_scaffold is None:
        raw_scaffold = {}
    if not isinstance(raw_scaffold, dict):
        raise RuntimeError("Expected client.scaffold to be a mapping.")

    scaffold_pack = client_scaffold_pack(raw_scaffold.get("pack"))
    raw_scaffold["pack"] = scaffold_pack
    client_doc["scaffold"] = raw_scaffold
    return scaffold_pack


def ensure_client_overlay_context_shape(
    client_doc: dict[str, Any],
    client_default_cwd: str,
    scaffold_pack: str,
) -> None:
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

    scaffold_pack = client_scaffold_pack(scaffold_pack)
    if scaffold_pack in {"planning", "hybrid"}:
        raw_plans = raw_context.setdefault("plans", {})
        if not isinstance(raw_plans, dict):
            raise RuntimeError("Expected client.context.plans to be a mapping.")
        for key, value in HARDENED_CLIENT_PLAN_PATHS.items():
            raw_plans[key] = value
        if scaffold_pack == "planning":
            raw_context.pop("workflow_builder", None)
            return

    if scaffold_pack in {"skill-builder", "hybrid"}:
        raw_workflow_builder = raw_context.setdefault("workflow_builder", {})
        if not isinstance(raw_workflow_builder, dict):
            raise RuntimeError("Expected client.context.workflow_builder to be a mapping.")
        for key, value in HARDENED_CLIENT_SKILL_BUILDER_CONTEXT["workflow_builder"].items():
            raw_workflow_builder[key] = value
        if scaffold_pack == "skill-builder":
            raw_context.pop("plans", None)


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
        or "${SKILLBOX_MONOSERVER_ROOT}"
    ).strip()
    client_doc["id"] = client_id
    client_doc["label"] = client_label
    client_doc["default_cwd"] = client_default_cwd

    scaffold_pack = ensure_client_overlay_scaffold_shape(client_doc)
    ensure_client_overlay_skillset_shape(client_doc, client_id)
    ensure_client_overlay_context_shape(client_doc, client_default_cwd, scaffold_pack)

    actions: list[str] = []
    rendered_overlay = render_yaml_document(overlay_doc)
    existing_overlay = overlay_path.read_text(encoding="utf-8")
    if existing_overlay != rendered_overlay:
        overlay_path.write_text(rendered_overlay, encoding="utf-8")
        actions.append(f"normalize-overlay: {repo_rel(root_dir, overlay_path)}")

    required_skills = client_scaffold_pack_required_skills(scaffold_pack)
    pick_line = ", ".join(required_skills)
    skill_repos_path = overlay_dir / "skill-repos.yaml"
    skill_repos_text = (
        f"# {client_label} client-specific skill repos.\n"
        "\n"
        "version: 2\n"
        "\n"
        "skill_repos:\n"
        "  - path: ./skills\n"
        f"    pick: [{pick_line}]\n"
    )
    if not skill_repos_path.is_file() or skill_repos_path.read_text(encoding="utf-8") != skill_repos_text:
        ensure_directory(skill_repos_path.parent, dry_run=False)
        skill_repos_path.write_text(skill_repos_text, encoding="utf-8")
        actions.append(f"write-file: {repo_rel(root_dir, skill_repos_path)}")

    skills_keep_path = overlay_dir / "skills" / ".gitkeep"
    ensure_directory(skills_keep_path.parent, dry_run=False)
    if not skills_keep_path.exists():
        skills_keep_path.write_text("", encoding="utf-8")
        actions.append(f"write-file: {repo_rel(root_dir, skills_keep_path)}")

    actions.extend(
        ensure_client_scaffold_skill_sources(
            root_dir,
            overlay_dir,
            scaffold_pack,
            dry_run=False,
        )
    )

    actions.extend(
        sync_client_scaffold_seed_files(
            root_dir,
            overlay_dir,
            client_label,
            scaffold_pack,
            dry_run=False,
        )
    )

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
