#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
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


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT_DIR = SCRIPT_DIR.parent.resolve()
SCRIPTS_DIR = DEFAULT_ROOT_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.runtime_model import (  # noqa: E402
    build_runtime_model,
    client_config_host_dir,
    client_config_runtime_dir,
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
VALID_HEALTHCHECK_TYPES = {"http", "path_exists"}
VALID_CHECK_TYPES = {"path_exists"}
VALID_TASK_SUCCESS_TYPES = {"path_exists"}
LOCKFILE_VERSION = 1
CONTEXT_CLAUDE_REL = Path("home") / ".claude" / "CLAUDE.md"
CONTEXT_CODEX_REL = Path("home") / ".codex" / "AGENTS.md"
CONTEXT_SYMLINK_TARGET = os.path.join("..", ".claude", "CLAUDE.md")
CLIENT_PROJECTS_REL = Path("builds") / "clients"
CLIENT_PROJECTION_VERSION = 1
CLIENT_PROJECT_RUNTIME_MODEL_REL = Path("runtime-model.json")
CLIENT_PROJECTION_METADATA_REL = Path("projection.json")
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
    if "required" in msg.lower() and "variable" in msg.lower():
        return structured_error(
            msg,
            error_type="missing_variable",
            recovery_hint="Add the missing --set KEY=VALUE assignments and retry.",
        )
    if "already exists" in msg.lower() or "without force" in msg.lower() or "already_exists" in msg.lower():
        return structured_error(
            msg,
            error_type="conflict",
            recovery_hint="Use --force to overwrite existing files, or choose a different client id.",
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


def next_actions_for_client_init(client_id: str) -> list[str]:
    return [
        f"sync --client {client_id} --format json",
        f"bootstrap --client {client_id} --format json",
        f"up --client {client_id} --format json",
    ]


def next_actions_for_focus(client_id: str, has_fail: bool) -> list[str]:
    if has_fail:
        return [
            f"doctor --client {client_id} --format json",
            f"logs --client {client_id} --format json",
        ]
    return [f"status --client {client_id} --format json"]


def next_actions_for_client_project(client_id: str) -> list[str]:
    return [
        f"render --client {client_id} --format json",
        f"sync --client {client_id} --format json",
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
                "bundle_dir": f"${{SKILLBOX_WORKSPACE_ROOT}}/default-skills/clients/{client_id}",
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


def default_client_scaffold_files(
    root_dir: Path,
    env_values: dict[str, str],
    client_id: str,
    client_label: str,
    client_root: str,
    client_default_cwd: str,
) -> dict[Path, str]:
    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)
    bundle_dir = root_dir / "default-skills" / "clients" / client_id
    skills_dir = root_dir / "skills" / "clients" / client_id

    overlay_path = overlay_dir / "overlay.yaml"
    manifest_path = overlay_dir / "skills.manifest"
    sources_path = overlay_dir / "skills.sources.yaml"
    bundle_readme_path = bundle_dir / "README.md"
    skills_keep_path = skills_dir / ".gitkeep"

    return {
        overlay_path: (
            "version: 1\n"
            "\n"
            "client:\n"
            f"  id: {json.dumps(client_id)}\n"
            f"  label: {json.dumps(client_label)}\n"
            f"  default_cwd: {json.dumps(client_default_cwd)}\n"
            "  repo_roots:\n"
            f"    - id: {json.dumps(f'{client_id}-root')}\n"
            "      kind: repo-root\n"
            f"      path: {json.dumps(client_root)}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: bind\n"
            "      sync:\n"
            "        mode: external\n"
            "      notes: Client root mounted from the shared monoserver tree.\n"
            "  skills:\n"
            f"    - id: {json.dumps(f'{client_id}-skills')}\n"
            "      kind: packaged-skill-set\n"
            "      required: false\n"
            "      profiles:\n"
            "        - core\n"
            f"      bundle_dir: {json.dumps(f'${{SKILLBOX_WORKSPACE_ROOT}}/default-skills/clients/{client_id}')}\n"
            f"      manifest: {json.dumps(f'${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skills.manifest')}\n"
            f"      sources_config: {json.dumps(f'${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skills.sources.yaml')}\n"
            f"      lock_path: {json.dumps(f'${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skills.lock.json')}\n"
            "      sync:\n"
            "        mode: unpack-bundles\n"
            "      install_targets:\n"
            "        - id: claude\n"
            f"          path: {json.dumps('${SKILLBOX_HOME_ROOT}/.claude/skills')}\n"
            "        - id: codex\n"
            f"          path: {json.dumps('${SKILLBOX_HOME_ROOT}/.codex/skills')}\n"
            "      notes: Client-scoped skills layered on top of the shared defaults.\n"
            "  logs:\n"
            f"    - id: {json.dumps(client_id)}\n"
            f"      path: {json.dumps(f'${{SKILLBOX_LOG_ROOT}}/clients/{client_id}')}\n"
            "      required: false\n"
            "      profiles:\n"
            "        - core\n"
            "      retention_days: 14\n"
            f"      notes: Client-scoped logs for the {client_id} overlay.\n"
            "  checks:\n"
            f"    - id: {json.dumps(f'{client_id}-root')}\n"
            "      type: path_exists\n"
            f"      path: {json.dumps(client_root)}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            f"      notes: The {client_id} overlay expects the client root to be mounted.\n"
        ),
        manifest_path: f"# {client_label} client-specific skills.\n",
        sources_path: (
            "version: 1\n"
            "\n"
            "sources:\n"
            "  - kind: local\n"
            f"    path: {json.dumps(f'./skills/clients/{client_id}')}\n"
        ),
        bundle_readme_path: (
            f"Generated `.skill` bundles for the `{client_id}` client overlay land here.\n"
        ),
        skills_keep_path: "",
    }


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

    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)
    bundle_dir = root_dir / "default-skills" / "clients" / client_id
    skills_dir = root_dir / "skills" / "clients" / client_id

    overlay_path = overlay_dir / "overlay.yaml"
    manifest_path = overlay_dir / "skills.manifest"
    sources_path = overlay_dir / "skills.sources.yaml"
    bundle_readme_path = bundle_dir / "README.md"
    skills_keep_path = skills_dir / ".gitkeep"

    return {
        overlay_path: render_yaml_document({"version": 1, "client": overlay_client}),
        manifest_path: f"# {overlay_client['label']} client-specific skills.\n",
        sources_path: render_yaml_document(
            {
                "version": 1,
                "sources": [
                    {
                        "kind": "local",
                        "path": f"./skills/clients/{client_id}",
                    }
                ],
            }
        ),
        bundle_readme_path: f"Generated `.skill` bundles for the `{client_id}` client overlay land here.\n",
        skills_keep_path: "",
    }


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

    return actions, blueprint_metadata


def normalize_active_profiles(raw_profiles: list[str] | None) -> set[str]:
    active_profiles = {value.strip() for value in raw_profiles or [] if value and value.strip()}
    active_profiles.add("core")
    return active_profiles


def normalize_active_clients(model: dict[str, Any], raw_clients: list[str] | None) -> set[str]:
    requested_clients = {value.strip() for value in raw_clients or [] if value and value.strip()}
    available_clients = {
        str(client.get("id", "")).strip()
        for client in model.get("clients") or []
        if str(client.get("id", "")).strip()
    }
    default_client = str((model.get("selection") or {}).get("default_client") or "").strip()
    if not requested_clients and default_client:
        requested_clients.add(default_client)

    unknown_clients = sorted(requested_clients - available_clients)
    if unknown_clients:
        raise RuntimeError(
            "Unknown runtime client(s): "
            + ", ".join(unknown_clients)
            + ". Available clients: "
            + (", ".join(sorted(available_clients)) or "(none)")
        )

    return requested_clients


def item_matches_profiles(item: dict[str, Any], active_profiles: set[str]) -> bool:
    item_profiles = {
        str(value).strip()
        for value in item.get("profiles") or []
        if str(value).strip()
    }
    if not item_profiles:
        return True
    return not item_profiles.isdisjoint(active_profiles)


def item_matches_clients(item: dict[str, Any], active_clients: set[str]) -> bool:
    item_client = str(item.get("client", "")).strip()
    if not item_client:
        return True
    return item_client in active_clients


def filter_model(model: dict[str, Any], active_profiles: set[str], active_clients: set[str]) -> dict[str, Any]:
    if not active_profiles and not active_clients:
        return model

    filtered_model = dict(model)
    filtered_model["active_profiles"] = sorted(active_profiles)
    filtered_model["active_clients"] = sorted(active_clients)
    filtered_model["clients"] = [
        copy.deepcopy(client)
        for client in model["clients"]
        if not active_clients or str(client.get("id", "")).strip() in active_clients
    ]
    filtered_model["repos"] = [
        copy.deepcopy(repo)
        for repo in model["repos"]
        if item_matches_profiles(repo, active_profiles) and item_matches_clients(repo, active_clients)
    ]
    filtered_model["artifacts"] = [
        copy.deepcopy(artifact)
        for artifact in model["artifacts"]
        if item_matches_profiles(artifact, active_profiles) and item_matches_clients(artifact, active_clients)
    ]
    filtered_model["env_files"] = [
        copy.deepcopy(env_file)
        for env_file in model["env_files"]
        if item_matches_profiles(env_file, active_profiles) and item_matches_clients(env_file, active_clients)
    ]
    filtered_model["skills"] = [
        copy.deepcopy(skillset)
        for skillset in model["skills"]
        if item_matches_profiles(skillset, active_profiles) and item_matches_clients(skillset, active_clients)
    ]
    filtered_model["tasks"] = [
        copy.deepcopy(task)
        for task in model["tasks"]
        if item_matches_profiles(task, active_profiles) and item_matches_clients(task, active_clients)
    ]
    filtered_model["services"] = [
        copy.deepcopy(service)
        for service in model["services"]
        if item_matches_profiles(service, active_profiles) and item_matches_clients(service, active_clients)
    ]
    filtered_model["logs"] = [
        copy.deepcopy(log_item)
        for log_item in model["logs"]
        if item_matches_profiles(log_item, active_profiles) and item_matches_clients(log_item, active_clients)
    ]
    filtered_model["checks"] = [
        copy.deepcopy(check)
        for check in model["checks"]
        if item_matches_profiles(check, active_profiles) and item_matches_clients(check, active_clients)
    ]

    included_repo_ids = {repo["id"] for repo in filtered_model["repos"]}
    included_artifact_ids = {artifact["id"] for artifact in filtered_model["artifacts"]}
    included_task_ids = {task["id"] for task in filtered_model["tasks"]}
    included_log_ids = {log_item["id"] for log_item in filtered_model["logs"]}

    tasks_by_id = {
        str(task["id"]): task
        for task in model["tasks"]
        if str(task.get("id", "")).strip()
    }

    def raw_task_dependency_ids(task: dict[str, Any]) -> list[str]:
        raw_dependencies = task.get("depends_on") or []
        if not isinstance(raw_dependencies, list):
            return []

        dependency_ids: list[str] = []
        seen_dependency_ids: set[str] = set()
        for raw_dependency in raw_dependencies:
            dependency_id = str(raw_dependency).strip()
            if not dependency_id or dependency_id in seen_dependency_ids:
                continue
            dependency_ids.append(dependency_id)
            seen_dependency_ids.add(dependency_id)
        return dependency_ids

    def raw_service_bootstrap_task_ids(service: dict[str, Any]) -> list[str]:
        raw_tasks = service.get("bootstrap_tasks") or []
        if not isinstance(raw_tasks, list):
            return []

        task_ids: list[str] = []
        seen_task_ids: set[str] = set()
        for raw_task in raw_tasks:
            task_id = str(raw_task).strip()
            if not task_id or task_id in seen_task_ids:
                continue
            task_ids.append(task_id)
            seen_task_ids.add(task_id)
        return task_ids

    def include_task(task_id: str) -> None:
        task = tasks_by_id.get(task_id)
        if task is None:
            return
        for dependency_id in raw_task_dependency_ids(task):
            include_task(dependency_id)
        if task_id in included_task_ids:
            return
        filtered_model["tasks"].append(copy.deepcopy(task))
        included_task_ids.add(task_id)

    for service in filtered_model["services"]:
        for task_id in raw_service_bootstrap_task_ids(service):
            include_task(task_id)

    for task in list(filtered_model["tasks"]):
        for dependency_id in raw_task_dependency_ids(task):
            include_task(dependency_id)

    required_repo_ids = {
        str(service["repo"])
        for service in filtered_model["services"]
        if service.get("repo")
    } | {
        str(task["repo"])
        for task in filtered_model["tasks"]
        if task.get("repo")
    }
    required_artifact_ids = {
        str(service["artifact"])
        for service in filtered_model["services"]
        if service.get("artifact")
    }
    required_log_ids = {
        str(service["log"])
        for service in filtered_model["services"]
        if service.get("log")
    } | {
        str(task["log"])
        for task in filtered_model["tasks"]
        if task.get("log")
    }

    for repo in model["repos"]:
        repo_id = str(repo.get("id", "")).strip()
        if repo_id and repo_id in required_repo_ids and repo_id not in included_repo_ids:
            filtered_model["repos"].append(copy.deepcopy(repo))
            included_repo_ids.add(repo_id)

    for artifact in model["artifacts"]:
        artifact_id = str(artifact.get("id", "")).strip()
        if artifact_id and artifact_id in required_artifact_ids and artifact_id not in included_artifact_ids:
            filtered_model["artifacts"].append(copy.deepcopy(artifact))
            included_artifact_ids.add(artifact_id)

    for log_item in model["logs"]:
        log_id = str(log_item.get("id", "")).strip()
        if log_id and log_id in required_log_ids and log_id not in included_log_ids:
            filtered_model["logs"].append(copy.deepcopy(log_item))
            included_log_ids.add(log_id)

    return filtered_model


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
                    "Refusing to remove a non-projection output directory outside the default "
                    f"build root: {output_dir}"
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
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    projection_payload: dict[str, Any] = {
        "version": CLIENT_PROJECTION_VERSION,
        "client_id": cid,
        "active_profiles": filtered_model.get("active_profiles", []),
        "active_clients": filtered_model.get("active_clients", []),
        "default_client": str((filtered_model.get("selection") or {}).get("default_client") or cid),
        "generated_at": generated_at,
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


def extract_bundle_to_target(bundle_path: Path, target_root: Path, skill_name: str) -> str:
    ensure_directory(target_root, dry_run=False)
    install_dir = target_root / skill_name

    bundle_members(bundle_path, expected_skill_name=skill_name)
    with tempfile.TemporaryDirectory(prefix=f".skillbox-{skill_name}-", dir=target_root) as tmpdir:
        temp_root = Path(tmpdir)
        with zipfile.ZipFile(bundle_path, "r") as archive:
            archive.extractall(temp_root)

        extracted_dir = temp_root / skill_name
        if not extracted_dir.is_dir():
            raise RuntimeError(f"Bundle {bundle_path} did not create {skill_name}/ after extraction")

        remove_path(install_dir)
        shutil.move(str(extracted_dir), str(install_dir))

    tree_sha = directory_tree_sha256(install_dir)
    if tree_sha is None:
        raise RuntimeError(f"Failed to hash installed skill directory {install_dir}")
    return tree_sha


def lock_skill_map(lock_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    skills = lock_payload.get("skills") or []
    if not isinstance(skills, list):
        raise RuntimeError("Lockfile field 'skills' must be a list")

    mapping: dict[str, dict[str, Any]] = {}
    for item in skills:
        if not isinstance(item, dict):
            raise RuntimeError("Lockfile skill entries must be objects")
        name = str(item.get("name", "")).strip()
        if not name:
            raise RuntimeError("Lockfile skill entries must include a non-empty name")
        if name in mapping:
            raise RuntimeError(f"Lockfile contains duplicate skill entry {name!r}")

        targets = item.get("targets") or []
        if not isinstance(targets, list):
            raise RuntimeError(f"Lockfile skill {name!r} has a non-list targets field")

        targets_by_id: dict[str, dict[str, Any]] = {}
        for target in targets:
            if not isinstance(target, dict):
                raise RuntimeError(f"Lockfile skill {name!r} contains a non-object target entry")
            target_id = str(target.get("id", "")).strip()
            if not target_id:
                raise RuntimeError(f"Lockfile skill {name!r} contains a target without an id")
            if target_id in targets_by_id:
                raise RuntimeError(f"Lockfile skill {name!r} contains duplicate target {target_id!r}")
            targets_by_id[target_id] = target

        mapping[name] = item | {"targets_by_id": targets_by_id}

    return mapping


def collect_skill_inventory(skillset: dict[str, Any]) -> dict[str, Any]:
    bundle_dir = Path(str(skillset["bundle_dir_host_path"]))
    manifest_path = Path(str(skillset["manifest_host_path"]))
    sources_config_path = Path(str(skillset["sources_config_host_path"]))
    lock_path = Path(str(skillset["lock_path_host_path"]))

    manifest_exists = manifest_path.is_file()
    sources_exists = sources_config_path.is_file()
    bundle_dir_exists = bundle_dir.is_dir()

    expected_skills = read_manifest_skills(manifest_path) if manifest_exists else []
    bundles: dict[str, dict[str, Any]] = {}
    if bundle_dir_exists:
        for bundle_path in sorted(bundle_dir.glob("*.skill")):
            bundles[bundle_path.stem] = bundle_metadata(bundle_path, expected_skill_name=bundle_path.stem)

    missing_bundles = sorted(name for name in expected_skills if name not in bundles)
    extra_bundles = sorted(name for name in bundles if name not in expected_skills)

    lock_payload: dict[str, Any] | None = None
    lock_error: str | None = None
    if lock_path.exists():
        try:
            lock_payload = load_json_file(lock_path)
            lock_skill_map(lock_payload)
        except RuntimeError as exc:
            lock_error = str(exc)

    lock_skills: dict[str, dict[str, Any]] = {}
    if lock_payload and not lock_error:
        lock_skills = lock_skill_map(lock_payload)

    skill_names = list(expected_skills)
    for extra_name in sorted(set(bundles) - set(skill_names)):
        skill_names.append(extra_name)
    for lock_name in sorted(set(lock_skills) - set(skill_names)):
        skill_names.append(lock_name)

    target_states: list[dict[str, Any]] = []
    for target in skillset.get("install_targets") or []:
        target_root = Path(str(target["host_path"]))
        target_states.append(
            {
                "id": target["id"],
                "path": str(target["path"]),
                "host_path": str(target_root),
                "present": target_root.exists(),
            }
        )

    skills: list[dict[str, Any]] = []
    for skill_name in skill_names:
        bundle_record = bundles.get(skill_name)
        lock_record = lock_skills.get(skill_name)
        skill_entry = {
            "name": skill_name,
            "bundle_present": bundle_record is not None,
            "bundle_state": "missing" if bundle_record is None else "present",
            "bundle_sha256": bundle_record.get("bundle_sha256") if bundle_record else None,
            "bundle_tree_sha256": bundle_record.get("bundle_tree_sha256") if bundle_record else None,
            "targets": [],
        }

        if bundle_record and lock_record:
            if (
                lock_record.get("bundle_sha256") == bundle_record["bundle_sha256"]
                and lock_record.get("bundle_tree_sha256") == bundle_record["bundle_tree_sha256"]
            ):
                skill_entry["bundle_state"] = "ok"
            else:
                skill_entry["bundle_state"] = "drift"
        elif bundle_record and lock_payload:
            skill_entry["bundle_state"] = "untracked"

        for target in target_states:
            install_dir = Path(target["host_path"]) / skill_name
            install_tree_sha = directory_tree_sha256(install_dir)
            target_lock = lock_record.get("targets_by_id", {}).get(target["id"]) if lock_record else None

            target_state = "missing"
            if install_dir.exists():
                target_state = "present"
            if target_lock:
                if install_tree_sha is None:
                    target_state = "missing"
                elif target_lock.get("tree_sha256") == install_tree_sha:
                    target_state = "ok"
                else:
                    target_state = "drift"
            elif install_tree_sha is not None and lock_payload:
                target_state = "untracked"

            skill_entry["targets"].append(
                {
                    "id": target["id"],
                    "path": str(target["path"]),
                    "host_path": str(install_dir),
                    "present": install_dir.exists(),
                    "tree_sha256": install_tree_sha,
                    "state": target_state,
                }
            )

        skills.append(skill_entry)

    return {
        "id": skillset["id"],
        "kind": skillset.get("kind", "packaged-skill-set"),
        "bundle_dir": str(skillset["bundle_dir"]),
        "bundle_dir_host_path": str(bundle_dir),
        "bundle_dir_exists": bundle_dir_exists,
        "manifest": str(skillset["manifest"]),
        "manifest_host_path": str(manifest_path),
        "manifest_exists": manifest_exists,
        "manifest_sha256": file_sha256(manifest_path) if manifest_exists else None,
        "sources_config": str(skillset["sources_config"]),
        "sources_config_host_path": str(sources_config_path),
        "sources_config_exists": sources_exists,
        "sources_config_sha256": file_sha256(sources_config_path) if sources_exists else None,
        "lock_path": str(skillset["lock_path"]),
        "lock_path_host_path": str(lock_path),
        "lock_present": lock_path.exists(),
        "lock_payload": lock_payload,
        "lock_error": lock_error,
        "expected_skills": expected_skills,
        "bundles": bundles,
        "missing_bundles": missing_bundles,
        "extra_bundles": extra_bundles,
        "install_targets": target_states,
        "skills": skills,
    }


def build_skill_lock(
    skillset: dict[str, Any],
    inventory: dict[str, Any],
    install_hashes: dict[str, dict[str, str]],
) -> dict[str, Any]:
    skills_payload: list[dict[str, Any]] = []
    for skill_name in inventory["expected_skills"]:
        bundle_record = inventory["bundles"][skill_name]
        target_payloads: list[dict[str, Any]] = []
        for target in skillset.get("install_targets") or []:
            install_dir = f"{str(target['path']).rstrip('/')}/{skill_name}"
            target_payloads.append(
                {
                    "id": target["id"],
                    "path": install_dir,
                    "tree_sha256": install_hashes[skill_name][target["id"]],
                }
            )

        skills_payload.append(
            {
                "name": skill_name,
                "bundle_file": bundle_record["filename"],
                "bundle_path": f"{str(skillset['bundle_dir']).rstrip('/')}/{bundle_record['filename']}",
                "bundle_sha256": bundle_record["bundle_sha256"],
                "bundle_tree_sha256": bundle_record["bundle_tree_sha256"],
                "targets": target_payloads,
            }
        )

    return {
        "version": LOCKFILE_VERSION,
        "id": skillset["id"],
        "kind": skillset.get("kind", "packaged-skill-set"),
        "bundle_dir": str(skillset["bundle_dir"]),
        "manifest": str(skillset["manifest"]),
        "manifest_sha256": inventory["manifest_sha256"],
        "sources_config": str(skillset["sources_config"]),
        "sources_config_sha256": inventory["sources_config_sha256"],
        "skills": skills_payload,
    }


def sync_skill_sets(model: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []

    for skillset in model["skills"]:
        inventory = collect_skill_inventory(skillset)
        missing_inputs: list[str] = []
        for field, present in (
            ("bundle_dir", inventory["bundle_dir_exists"]),
            ("manifest", inventory["manifest_exists"]),
            ("sources_config", inventory["sources_config_exists"]),
        ):
            if not present:
                missing_inputs.append(field)
        if missing_inputs:
            raise RuntimeError(
                f"Skill set {skillset['id']} is missing required files: {', '.join(missing_inputs)}"
            )
        if inventory["missing_bundles"]:
            raise RuntimeError(
                f"Skill set {skillset['id']} is missing bundles for: {', '.join(inventory['missing_bundles'])}"
            )

        if inventory["extra_bundles"]:
            actions.append(
                f"ignore-extra-bundles: {skillset['id']} -> {', '.join(inventory['extra_bundles'])}"
            )

        for target in skillset.get("install_targets") or []:
            target_root = Path(str(target["host_path"]))
            ensure_directory(target_root, dry_run)
            actions.append(f"ensure-directory: {target_root}")

        install_hashes: dict[str, dict[str, str]] = {}
        for skill_name in inventory["expected_skills"]:
            install_hashes[skill_name] = {}
            bundle_record = inventory["bundles"][skill_name]
            bundle_path = Path(str(bundle_record["host_path"]))

            for target in skillset.get("install_targets") or []:
                target_root = Path(str(target["host_path"]))
                install_dir = target_root / skill_name
                if dry_run:
                    actions.append(f"install-skill: {bundle_path} -> {install_dir}")
                    continue

                install_hashes[skill_name][target["id"]] = extract_bundle_to_target(
                    bundle_path=bundle_path,
                    target_root=target_root,
                    skill_name=skill_name,
                )
                actions.append(f"install-skill: {bundle_path} -> {install_dir}")

        lock_path = Path(str(skillset["lock_path_host_path"]))
        if dry_run:
            actions.append(f"write-lockfile: {lock_path}")
            continue

        lock_payload = build_skill_lock(skillset, inventory, install_hashes)
        changed = write_json_file(lock_path, lock_payload)
        actions.append(f"{'write-lockfile' if changed else 'lockfile-unchanged'}: {lock_path}")

    return actions


def validate_skill_locks_and_state(model: dict[str, Any]) -> list[CheckResult]:
    if not model["skills"]:
        return []

    bundle_failures: list[str] = []
    bundle_warnings: list[str] = []
    lock_failures: list[str] = []
    lock_warnings: list[str] = []
    install_failures: list[str] = []
    install_warnings: list[str] = []

    for skillset in model["skills"]:
        inventory = collect_skill_inventory(skillset)

        required_missing: list[str] = []
        for label, present, display_path in (
            ("bundle_dir", inventory["bundle_dir_exists"], inventory["bundle_dir_host_path"]),
            ("manifest", inventory["manifest_exists"], inventory["manifest_host_path"]),
            ("sources_config", inventory["sources_config_exists"], inventory["sources_config_host_path"]),
        ):
            if not present:
                required_missing.append(f"{skillset['id']}: missing {label} at {display_path}")

        if required_missing:
            bundle_failures.extend(required_missing)
            continue

        if inventory["missing_bundles"]:
            bundle_failures.append(
                f"{skillset['id']}: missing bundles for {', '.join(inventory['missing_bundles'])}"
            )
        if inventory["extra_bundles"]:
            bundle_warnings.append(
                f"{skillset['id']}: extra bundles present for {', '.join(inventory['extra_bundles'])}"
            )

        if inventory["lock_error"]:
            lock_failures.append(f"{skillset['id']}: {inventory['lock_error']}")
        elif not inventory["lock_present"]:
            lock_warnings.append(
                f"{skillset['id']}: lockfile missing at {inventory['lock_path_host_path']}"
            )
        else:
            lock_payload = inventory["lock_payload"] or {}
            if lock_payload.get("version") != LOCKFILE_VERSION:
                lock_failures.append(
                    f"{skillset['id']}: lockfile version {lock_payload.get('version')!r} does not match {LOCKFILE_VERSION}"
                )
            if lock_payload.get("id") != skillset["id"]:
                lock_failures.append(f"{skillset['id']}: lockfile id does not match the skill set id")
            if lock_payload.get("manifest_sha256") != inventory["manifest_sha256"]:
                lock_failures.append(f"{skillset['id']}: lockfile manifest digest is stale")
            if lock_payload.get("sources_config_sha256") != inventory["sources_config_sha256"]:
                lock_failures.append(f"{skillset['id']}: lockfile sources config digest is stale")

            indexed_lock = lock_skill_map(lock_payload)
            expected_skill_names = set(inventory["expected_skills"])
            if set(indexed_lock) - expected_skill_names:
                extras = ", ".join(sorted(set(indexed_lock) - expected_skill_names))
                lock_failures.append(f"{skillset['id']}: lockfile contains extra skills: {extras}")

            for skill_name in inventory["expected_skills"]:
                lock_record = indexed_lock.get(skill_name)
                if lock_record is None:
                    lock_failures.append(f"{skillset['id']}: lockfile is missing skill {skill_name}")
                    continue

                bundle_record = inventory["bundles"].get(skill_name)
                if bundle_record is None:
                    continue

                if lock_record.get("bundle_sha256") != bundle_record["bundle_sha256"]:
                    lock_failures.append(
                        f"{skillset['id']}: lockfile bundle digest is stale for {skill_name}"
                    )
                if lock_record.get("bundle_tree_sha256") != bundle_record["bundle_tree_sha256"]:
                    lock_failures.append(
                        f"{skillset['id']}: lockfile bundle tree digest is stale for {skill_name}"
                    )

                lock_targets = lock_record.get("targets_by_id", {})
                configured_targets = {target["id"] for target in skillset.get("install_targets") or []}
                if set(lock_targets) - configured_targets:
                    extras = ", ".join(sorted(set(lock_targets) - configured_targets))
                    lock_failures.append(
                        f"{skillset['id']}: lockfile contains unexpected targets for {skill_name}: {extras}"
                    )

                missing_targets = sorted(configured_targets - set(lock_targets))
                if missing_targets:
                    lock_failures.append(
                        f"{skillset['id']}: lockfile is missing targets for {skill_name}: {', '.join(missing_targets)}"
                    )

        for skill_entry in inventory["skills"]:
            bundle_state = skill_entry["bundle_state"]
            if bundle_state == "drift":
                install_failures.append(
                    f"{skillset['id']}: bundle digest drift detected for {skill_entry['name']}"
                )
            elif bundle_state == "untracked" and inventory["lock_present"]:
                install_failures.append(
                    f"{skillset['id']}: bundle {skill_entry['name']} is not represented in the lockfile"
                )

            for target in skill_entry["targets"]:
                if target["state"] == "drift":
                    install_failures.append(
                        f"{skillset['id']}: installed drift for {skill_entry['name']} in {target['id']}"
                    )
                elif target["state"] == "untracked":
                    install_failures.append(
                        f"{skillset['id']}: unmanaged install for {skill_entry['name']} in {target['id']}"
                    )
                elif target["state"] == "missing":
                    install_warnings.append(
                        f"{skillset['id']}: missing install for {skill_entry['name']} in {target['id']}"
                    )

    results: list[CheckResult] = []
    if bundle_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-bundle-state",
                message="managed skill bundles do not satisfy the declared manifest",
                details={"issues": bundle_failures},
            )
        )
    elif bundle_warnings:
        results.append(
            CheckResult(
                status="warn",
                code="skill-bundle-state",
                message="managed skill bundle directory contains undeclared bundles",
                details={"issues": bundle_warnings},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-bundle-state",
                message="managed skill bundle directories satisfy the declared manifests",
            )
        )

    if lock_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-lock-state",
                message="managed skill lockfiles are invalid or stale",
                details={"issues": lock_failures},
            )
        )
    elif lock_warnings:
        results.append(
            CheckResult(
                status="warn",
                code="skill-lock-state",
                message="managed skill lockfiles have not been generated yet",
                details={"issues": lock_warnings},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-lock-state",
                message="managed skill lockfiles match the current bundle and source manifests",
            )
        )

    if install_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-install-state",
                message="installed skill directories drifted from the managed bundles",
                details={"issues": install_failures},
            )
        )
    elif install_warnings:
        results.append(
            CheckResult(
                status="warn",
                code="skill-install-state",
                message="managed skill installs are missing and can be created by sync",
                details={"issues": install_warnings},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-install-state",
                message="managed skill installs match the lockfile and bundle contents",
            )
        )

    return results


def check_manifest(model: dict[str, Any]) -> list[CheckResult]:
    issues: list[str] = []

    client_ids = find_duplicates(model.get("clients") or [], "id")
    if client_ids:
        issues.append(f"clients contain duplicate ids: {', '.join(client_ids)}")
    for client in model.get("clients") or []:
        if not client.get("id"):
            issues.append("every client entry must have an id")

    declared_client_ids = {
        str(client.get("id", "")).strip()
        for client in model.get("clients") or []
        if str(client.get("id", "")).strip()
    }
    default_client = str((model.get("selection") or {}).get("default_client") or "").strip()
    if default_client and default_client not in declared_client_ids:
        issues.append(f"selection.default_client references unknown client {default_client!r}")

    for section in ("repos", "artifacts", "env_files", "skills", "tasks", "services", "logs", "checks"):
        duplicates = find_duplicates(model[section], "id")
        if duplicates:
            issues.append(f"{section} contain duplicate ids: {', '.join(duplicates)}")

    duplicate_repo_paths = find_duplicates(model["repos"], "path")
    if duplicate_repo_paths:
        issues.append(f"repos contain duplicate paths: {', '.join(duplicate_repo_paths)}")

    duplicate_log_paths = find_duplicates(model["logs"], "path")
    if duplicate_log_paths:
        issues.append(f"logs contain duplicate paths: {', '.join(duplicate_log_paths)}")

    duplicate_artifact_paths = find_duplicates(model["artifacts"], "path")
    if duplicate_artifact_paths:
        issues.append(f"artifacts contain duplicate paths: {', '.join(duplicate_artifact_paths)}")

    duplicate_env_file_paths = find_duplicates(model["env_files"], "path")
    if duplicate_env_file_paths:
        issues.append(f"env_files contain duplicate paths: {', '.join(duplicate_env_file_paths)}")

    repo_ids = {repo.get("id") for repo in model["repos"]}
    artifact_ids = {artifact.get("id") for artifact in model["artifacts"]}
    task_ids = {
        str(task.get("id", "")).strip()
        for task in model["tasks"]
        if str(task.get("id", "")).strip()
    }
    log_ids = {log_item.get("id") for log_item in model["logs"]}

    for repo in model["repos"]:
        if not repo.get("id"):
            issues.append("every repo entry must have an id")
        if not repo.get("path"):
            issues.append(f"repo {repo.get('id', '(missing id)')} is missing path")
        if repo.get("client") and repo["client"] not in declared_client_ids:
            issues.append(f"repo {repo.get('id')} references unknown client {repo['client']!r}")

        source = repo.get("source") or {}
        source_kind = source.get("kind", "manual")
        if source_kind not in VALID_REPO_SOURCE_KINDS:
            issues.append(f"repo {repo.get('id')} has unsupported source.kind {source_kind!r}")

        sync = repo.get("sync") or {}
        sync_mode = sync.get("mode") or (
            "ensure-directory" if source_kind == "directory" else "external"
        )
        if sync_mode not in VALID_SYNC_MODES:
            issues.append(f"repo {repo.get('id')} has unsupported sync.mode {sync_mode!r}")
        if source_kind == "git" and not source.get("url"):
            issues.append(f"repo {repo.get('id')} is git-backed but missing source.url")

    for artifact in model["artifacts"]:
        if not artifact.get("id"):
            issues.append("every artifact entry must have an id")
        if not artifact.get("path"):
            issues.append(f"artifact {artifact.get('id', '(missing id)')} is missing path")
        if artifact.get("client") and artifact["client"] not in declared_client_ids:
            issues.append(f"artifact {artifact.get('id')} references unknown client {artifact['client']!r}")

        source = artifact.get("source") or {}
        source_kind = source.get("kind", "manual")
        if source_kind not in VALID_ARTIFACT_SOURCE_KINDS:
            issues.append(f"artifact {artifact.get('id')} has unsupported source.kind {source_kind!r}")

        sync = artifact.get("sync") or {}
        sync_mode = sync.get("mode") or (
            "download-if-missing" if source_kind == "url" else "copy-if-missing" if source_kind == "file" else "manual"
        )
        if sync_mode not in VALID_ARTIFACT_SYNC_MODES:
            issues.append(f"artifact {artifact.get('id')} has unsupported sync.mode {sync_mode!r}")
        if source_kind == "url" and str(source.get("url") or "").strip():
            try:
                validate_url_download_source(source, artifact_id=str(artifact.get("id", "(missing id)")))
            except RuntimeError as exc:
                issues.append(str(exc))

    for env_file in model["env_files"]:
        if not env_file.get("id"):
            issues.append("every env_files entry must have an id")
        if not env_file.get("path"):
            issues.append(f"env file {env_file.get('id', '(missing id)')} is missing path")
        if env_file.get("client") and env_file["client"] not in declared_client_ids:
            issues.append(f"env file {env_file.get('id')} references unknown client {env_file['client']!r}")
        if env_file.get("repo") and env_file["repo"] not in repo_ids:
            issues.append(f"env file {env_file.get('id')} references unknown repo {env_file['repo']!r}")

        source = env_file.get("source") or {}
        source_kind = source.get("kind", "manual")
        if source_kind not in VALID_ENV_FILE_SOURCE_KINDS:
            issues.append(f"env file {env_file.get('id')} has unsupported source.kind {source_kind!r}")

        sync = env_file.get("sync") or {}
        sync_mode = sync.get("mode") or ("write" if source_kind == "file" else "manual")
        if sync_mode not in VALID_ENV_FILE_SYNC_MODES:
            issues.append(f"env file {env_file.get('id')} has unsupported sync.mode {sync_mode!r}")
        if source_kind == "file" and not source.get("path"):
            issues.append(f"env file {env_file.get('id')} is file-backed but missing source.path")

    for skillset in model["skills"]:
        if not skillset.get("id"):
            issues.append("every skills entry must have an id")
        if skillset.get("client") and skillset["client"] not in declared_client_ids:
            issues.append(f"skill set {skillset.get('id')} references unknown client {skillset['client']!r}")
        for field in ("bundle_dir", "manifest", "sources_config", "lock_path"):
            if not skillset.get(field):
                issues.append(f"skill set {skillset.get('id', '(missing id)')} is missing {field}")

        sync = skillset.get("sync") or {}
        sync_mode = sync.get("mode") or "unpack-bundles"
        if sync_mode not in VALID_SKILL_SYNC_MODES:
            issues.append(f"skill set {skillset.get('id')} has unsupported sync.mode {sync_mode!r}")

        targets = skillset.get("install_targets") or []
        if not targets:
            issues.append(f"skill set {skillset.get('id')} must declare at least one install target")
            continue

        target_ids = find_duplicates(targets, "id")
        if target_ids:
            issues.append(f"skill set {skillset.get('id')} contains duplicate target ids: {', '.join(target_ids)}")

        for target in targets:
            if not target.get("id"):
                issues.append(f"skill set {skillset.get('id')} contains a target without an id")
            if not target.get("path"):
                issues.append(f"skill set {skillset.get('id')} target {target.get('id', '(missing id)')} is missing path")

    task_dependency_map: dict[str, list[str]] = {}
    for task in model["tasks"]:
        task_id = str(task.get("id", "")).strip()
        if not task.get("id"):
            issues.append("every task entry must have an id")
        if task.get("client") and task["client"] not in declared_client_ids:
            issues.append(f"task {task.get('id')} references unknown client {task['client']!r}")
        if task.get("repo") and task["repo"] not in repo_ids:
            issues.append(f"task {task.get('id')} references unknown repo {task['repo']!r}")
        if task.get("log") and task["log"] not in log_ids:
            issues.append(f"task {task.get('id')} references unknown log {task['log']!r}")
        if not str(task.get("command") or "").strip():
            issues.append(f"task {task.get('id', '(missing id)')} is missing command")

        for field_name in ("inputs", "outputs"):
            raw_value = task.get(field_name) or []
            if not isinstance(raw_value, list):
                issues.append(f"task {task.get('id')} has non-list {field_name}")

        raw_dependencies = task.get("depends_on") or []
        if raw_dependencies and not isinstance(raw_dependencies, list):
            issues.append(f"task {task.get('id')} has non-list depends_on")
            raw_dependencies = []

        dependency_ids: list[str] = []
        seen_dependency_ids: set[str] = set()
        for raw_dependency in raw_dependencies:
            dependency_id = str(raw_dependency).strip()
            if not dependency_id:
                issues.append(f"task {task.get('id')} contains an empty depends_on entry")
                continue
            if dependency_id in seen_dependency_ids:
                issues.append(f"task {task.get('id')} contains duplicate depends_on entry {dependency_id!r}")
                continue
            if dependency_id == task_id:
                issues.append(f"task {task.get('id')} cannot depend on itself")
                continue
            if dependency_id not in task_ids:
                issues.append(f"task {task.get('id')} references unknown dependency {dependency_id!r}")
                continue
            dependency_ids.append(dependency_id)
            seen_dependency_ids.add(dependency_id)
        if task_id:
            task_dependency_map[task_id] = dependency_ids

        success = task.get("success") or {}
        success_type = success.get("type")
        if not success_type:
            issues.append(f"task {task.get('id', '(missing id)')} is missing success.type")
        elif success_type not in VALID_TASK_SUCCESS_TYPES:
            issues.append(f"task {task.get('id')} has unsupported success.type {success_type!r}")
        if success_type == "path_exists" and not success.get("path"):
            issues.append(f"task {task.get('id')} path_exists success is missing path")

    service_ids = {
        str(service.get("id", "")).strip()
        for service in model["services"]
        if str(service.get("id", "")).strip()
    }
    service_dependency_map: dict[str, list[str]] = {}

    for service in model["services"]:
        service_id = str(service.get("id", "")).strip()
        if not service.get("id"):
            issues.append("every service entry must have an id")
        if service.get("client") and service["client"] not in declared_client_ids:
            issues.append(f"service {service.get('id')} references unknown client {service['client']!r}")
        if service.get("repo") and service["repo"] not in repo_ids:
            issues.append(f"service {service.get('id')} references unknown repo {service['repo']!r}")
        if service.get("artifact") and service["artifact"] not in artifact_ids:
            issues.append(f"service {service.get('id')} references unknown artifact {service['artifact']!r}")
        if service.get("log") and service["log"] not in log_ids:
            issues.append(f"service {service.get('id')} references unknown log {service['log']!r}")
        if not str(service.get("command") or "").strip():
            issues.append(f"service {service.get('id', '(missing id)')} is missing command")

        raw_dependencies = service.get("depends_on") or []
        if raw_dependencies and not isinstance(raw_dependencies, list):
            issues.append(f"service {service.get('id')} has non-list depends_on")
            raw_dependencies = []

        dependency_ids: list[str] = []
        seen_dependency_ids: set[str] = set()
        for raw_dependency in raw_dependencies:
            dependency_id = str(raw_dependency).strip()
            if not dependency_id:
                issues.append(f"service {service.get('id')} contains an empty depends_on entry")
                continue
            if dependency_id in seen_dependency_ids:
                issues.append(f"service {service.get('id')} contains duplicate depends_on entry {dependency_id!r}")
                continue
            if dependency_id == service_id:
                issues.append(f"service {service.get('id')} cannot depend on itself")
                continue
            if dependency_id not in service_ids:
                issues.append(f"service {service.get('id')} references unknown dependency {dependency_id!r}")
                continue
            dependency_ids.append(dependency_id)
            seen_dependency_ids.add(dependency_id)
        if service_id:
            service_dependency_map[service_id] = dependency_ids

        raw_bootstrap_tasks = service.get("bootstrap_tasks") or []
        if raw_bootstrap_tasks and not isinstance(raw_bootstrap_tasks, list):
            issues.append(f"service {service.get('id')} has non-list bootstrap_tasks")
            raw_bootstrap_tasks = []

        seen_bootstrap_tasks: set[str] = set()
        for raw_task in raw_bootstrap_tasks:
            task_id = str(raw_task).strip()
            if not task_id:
                issues.append(f"service {service.get('id')} contains an empty bootstrap_tasks entry")
                continue
            if task_id in seen_bootstrap_tasks:
                issues.append(f"service {service.get('id')} contains duplicate bootstrap_tasks entry {task_id!r}")
                continue
            if task_id not in task_ids:
                issues.append(f"service {service.get('id')} references unknown bootstrap task {task_id!r}")
                continue
            seen_bootstrap_tasks.add(task_id)

        healthcheck = service.get("healthcheck") or {}
        healthcheck_type = healthcheck.get("type")
        if healthcheck_type:
            if healthcheck_type not in VALID_HEALTHCHECK_TYPES:
                issues.append(
                    f"service {service.get('id')} has unsupported healthcheck.type {healthcheck_type!r}"
                )
            if healthcheck_type == "http" and not healthcheck.get("url"):
                issues.append(f"service {service.get('id')} http healthcheck is missing url")
            if healthcheck_type == "path_exists" and not healthcheck.get("path"):
                issues.append(f"service {service.get('id')} path_exists healthcheck is missing path")

    visiting: list[str] = []
    visited: set[str] = set()

    def visit_service_dependency(service_id: str) -> None:
        if service_id in visited:
            return
        if service_id in visiting:
            cycle_start = visiting.index(service_id)
            cycle = visiting[cycle_start:] + [service_id]
            issues.append("service dependency cycle detected: " + " -> ".join(cycle))
            return

        visiting.append(service_id)
        for dependency_id in service_dependency_map.get(service_id, []):
            visit_service_dependency(dependency_id)
        visiting.pop()
        visited.add(service_id)

    for service_id in sorted(service_dependency_map):
        visit_service_dependency(service_id)

    task_visiting: list[str] = []
    task_visited: set[str] = set()

    def visit_task_dependency(task_id: str) -> None:
        if task_id in task_visited:
            return
        if task_id in task_visiting:
            cycle_start = task_visiting.index(task_id)
            cycle = task_visiting[cycle_start:] + [task_id]
            issues.append("task dependency cycle detected: " + " -> ".join(cycle))
            return

        task_visiting.append(task_id)
        for dependency_id in task_dependency_map.get(task_id, []):
            visit_task_dependency(dependency_id)
        task_visiting.pop()
        task_visited.add(task_id)

    for task_id in sorted(task_dependency_map):
        visit_task_dependency(task_id)

    for log_item in model["logs"]:
        if not log_item.get("id"):
            issues.append("every log entry must have an id")
        if not log_item.get("path"):
            issues.append(f"log {log_item.get('id', '(missing id)')} is missing path")
        if log_item.get("client") and log_item["client"] not in declared_client_ids:
            issues.append(f"log {log_item.get('id')} references unknown client {log_item['client']!r}")

    for check in model["checks"]:
        check_type = check.get("type")
        if check_type not in VALID_CHECK_TYPES:
            issues.append(f"check {check.get('id')} has unsupported type {check_type!r}")
        if check_type == "path_exists" and not check.get("path"):
            issues.append(f"check {check.get('id')} is missing path")
        if check.get("client") and check["client"] not in declared_client_ids:
            issues.append(f"check {check.get('id')} references unknown client {check['client']!r}")

    if issues:
        return [
            CheckResult(
                status="fail",
                code="runtime-manifest",
                message="runtime manifest contains invalid definitions",
                details={"issues": issues},
            )
        ]

    return [
        CheckResult(
            status="pass",
            code="runtime-manifest",
            message="runtime manifest definitions are internally consistent",
            details={
                "repos": len(model["repos"]),
                "artifacts": len(model["artifacts"]),
                "env_files": len(model["env_files"]),
                "skills": len(model["skills"]),
                "tasks": len(model["tasks"]),
                "services": len(model["services"]),
                "logs": len(model["logs"]),
                "checks": len(model["checks"]),
            },
        )
    ]


def normalize_file_mode(raw_mode: Any, default: int = 0o600) -> int:
    if raw_mode is None:
        return default
    if isinstance(raw_mode, int):
        return raw_mode & 0o777

    text = str(raw_mode).strip()
    if not text:
        return default
    try:
        return int(text, 8) & 0o777
    except ValueError as exc:
        raise RuntimeError(f"Invalid file mode {raw_mode!r}. Use an octal string such as '0600'.") from exc


def env_file_state(env_file: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(env_file["host_path"]))
    source = env_file.get("source") or {}
    source_kind = str(source.get("kind", "manual")).strip() or "manual"
    sync = env_file.get("sync") or {}
    sync_mode = str(sync.get("mode") or ("write" if source_kind == "file" else "manual")).strip()
    desired_mode = normalize_file_mode(env_file.get("mode"), default=0o600)

    source_path: Path | None = None
    raw_source_path = str(source.get("host_path") or source.get("path") or "").strip()
    if raw_source_path:
        source_path = Path(raw_source_path)

    present = path.is_file()
    source_present = bool(source_path and source_path.is_file())
    state = "ok" if present else "missing"
    syncable = False

    if source_kind == "file" and sync_mode == "write":
        if not source_present:
            state = "source-missing"
        elif not present:
            state = "missing"
            syncable = True
        else:
            target_mode = path.stat().st_mode & 0o777
            if path.read_bytes() != source_path.read_bytes() or target_mode != desired_mode:
                state = "stale"
                syncable = True
            else:
                state = "ok"
    elif not present:
        state = "missing"

    return {
        "id": env_file["id"],
        "kind": env_file.get("kind", "env-file"),
        "repo": str(env_file.get("repo") or ""),
        "path": str(env_file["path"]),
        "host_path": str(path),
        "present": present,
        "required": bool(env_file.get("required")),
        "profiles": env_file.get("profiles") or [],
        "source_kind": source_kind,
        "source_path": str(source.get("path") or ""),
        "source_host_path": str(source_path) if source_path else "",
        "source_present": source_present,
        "sync_mode": sync_mode,
        "mode": f"{desired_mode:04o}",
        "state": state,
        "syncable": syncable,
    }


def check_filesystem(model: dict[str, Any], root_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    missing_syncable_repo_paths: list[str] = []
    missing_required_repo_paths: list[str] = []
    missing_syncable_artifact_paths: list[str] = []
    missing_required_artifact_paths: list[str] = []
    syncable_env_files: list[str] = []
    missing_required_env_sources: list[str] = []
    missing_required_env_targets: list[str] = []
    missing_log_paths: list[str] = []
    missing_required_checks: list[str] = []

    for repo in model["repos"]:
        path = Path(str(repo["host_path"]))
        if path.exists():
            continue

        source = repo.get("source") or {}
        source_kind = source.get("kind", "manual")
        sync = repo.get("sync") or {}
        sync_mode = sync.get("mode") or (
            "ensure-directory" if source_kind == "directory" else "external"
        )

        if sync_mode in {"ensure-directory", "clone-if-missing"} or source_kind in {"directory", "git"}:
            missing_syncable_repo_paths.append(repo_rel(root_dir, path))
        elif repo.get("required"):
            missing_required_repo_paths.append(repo_rel(root_dir, path))

    for artifact in model["artifacts"]:
        path = Path(str(artifact["host_path"]))
        if path.exists():
            continue

        source = artifact.get("source") or {}
        source_kind = source.get("kind", "manual")
        sync = artifact.get("sync") or {}
        sync_mode = sync.get("mode") or (
            "download-if-missing" if source_kind == "url" else "copy-if-missing" if source_kind == "file" else "manual"
        )

        if (
            sync_mode in {"copy-if-missing", "download-if-missing"}
            and source_kind in {"file", "url"}
            and artifact_source_configured(artifact)
        ):
            missing_syncable_artifact_paths.append(repo_rel(root_dir, path))
        elif artifact.get("required"):
            missing_required_artifact_paths.append(repo_rel(root_dir, path))

    for env_file in model["env_files"]:
        state = env_file_state(env_file)
        display_path = repo_rel(root_dir, Path(state["host_path"]))
        if state["state"] == "source-missing":
            if env_file.get("required"):
                if state["source_host_path"]:
                    missing_required_env_sources.append(repo_rel(root_dir, Path(state["source_host_path"])))
                else:
                    missing_required_env_sources.append(state["source_path"] or display_path)
        elif state["state"] in {"missing", "stale"}:
            if state["syncable"]:
                syncable_env_files.append(display_path)
            elif env_file.get("required"):
                missing_required_env_targets.append(display_path)

    for log_item in model["logs"]:
        path = Path(str(log_item["host_path"]))
        if not path.exists():
            missing_log_paths.append(repo_rel(root_dir, path))

    for check in model["checks"]:
        if check.get("type") != "path_exists":
            continue
        path = Path(str(check["host_path"]))
        if not path.exists() and check.get("required"):
            missing_required_checks.append(repo_rel(root_dir, path))

    if missing_required_repo_paths:
        results.append(
            CheckResult(
                status="fail",
                code="required-runtime-paths",
                message="required runtime repo paths are missing",
                details={"missing": missing_required_repo_paths},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="required-runtime-paths",
                message="required runtime repo paths are present",
            )
        )

    if missing_syncable_repo_paths:
        results.append(
            CheckResult(
                status="warn",
                code="syncable-repo-paths",
                message="managed repo paths are missing but can be created by sync",
                details={"missing": missing_syncable_repo_paths},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="syncable-repo-paths",
                message="managed repo paths do not need sync",
            )
        )

    if missing_required_artifact_paths:
        results.append(
            CheckResult(
                status="fail",
                code="required-runtime-artifacts",
                message="required runtime artifact paths are missing",
                details={"missing": missing_required_artifact_paths},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="required-runtime-artifacts",
                message="required runtime artifact paths are present",
            )
        )

    if missing_syncable_artifact_paths:
        results.append(
            CheckResult(
                status="warn",
                code="syncable-artifact-paths",
                message="managed artifact paths are missing but can be created by sync",
                details={"missing": missing_syncable_artifact_paths},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="syncable-artifact-paths",
                message="managed artifact paths do not need sync",
            )
        )

    if missing_required_env_sources or missing_required_env_targets:
        details: dict[str, Any] = {}
        if missing_required_env_sources:
            details["missing_sources"] = missing_required_env_sources
        if missing_required_env_targets:
            details["missing_targets"] = missing_required_env_targets
        results.append(
            CheckResult(
                status="fail",
                code="required-runtime-env-files",
                message="required runtime env files cannot be materialized",
                details=details,
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="required-runtime-env-files",
                message="required runtime env files are materialized or source-backed",
            )
        )

    if syncable_env_files:
        results.append(
            CheckResult(
                status="warn",
                code="syncable-env-files",
                message="managed env files are missing or stale but can be materialized by sync",
                details={"targets": syncable_env_files},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="syncable-env-files",
                message="managed env files do not need sync",
            )
        )

    if missing_log_paths:
        results.append(
            CheckResult(
                status="warn",
                code="runtime-log-paths",
                message="managed log directories are missing but can be created by sync",
                details={"missing": missing_log_paths},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="runtime-log-paths",
                message="managed log directories are present",
            )
        )

    if missing_required_checks:
        results.append(
            CheckResult(
                status="fail",
                code="required-runtime-checks",
                message="required runtime checks failed",
                details={"missing": missing_required_checks},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="required-runtime-checks",
                message="required runtime checks passed",
            )
        )

    return results


def validate_task_state(model: dict[str, Any]) -> list[CheckResult]:
    if not model["tasks"]:
        return []

    pending_tasks: list[str] = []
    blocked_tasks: list[str] = []

    for task in model["tasks"]:
        task_state = probe_task(model, task)
        if task_state["state"] == "ready":
            continue

        summary = task["id"]
        if task_state.get("target"):
            summary += f" -> {task_state['target']}"
        if task_state["state"] == "blocked":
            blocked_on = [
                dependency_id
                for dependency_id, dependency_state in task_state.get("dependency_states", {}).items()
                if dependency_state != "ok"
            ]
            if blocked_on:
                summary += f" (blocked by {', '.join(blocked_on)})"
            blocked_tasks.append(summary)
        else:
            pending_tasks.append(summary)

    if pending_tasks or blocked_tasks:
        details: dict[str, Any] = {}
        if pending_tasks:
            details["pending"] = pending_tasks
        if blocked_tasks:
            details["blocked"] = blocked_tasks
        return [
            CheckResult(
                status="warn",
                code="bootstrap-task-state",
                message="bootstrap tasks are pending and can be materialized by bootstrap",
                details=details,
            )
        ]

    return [
        CheckResult(
            status="pass",
            code="bootstrap-task-state",
            message="bootstrap task success checks are satisfied",
        )
    ]


def doctor_results(model: dict[str, Any], root_dir: Path) -> list[CheckResult]:
    results = check_manifest(model)
    if any(result.status == "fail" for result in results):
        return results
    return results + check_filesystem(model, root_dir) + validate_skill_locks_and_state(model) + validate_task_state(model)


def sync_artifact(artifact: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []
    path = Path(str(artifact["host_path"]))
    source = artifact.get("source") or {}
    source_kind = source.get("kind", "manual")
    sync = artifact.get("sync") or {}
    sync_mode = sync.get("mode") or (
        "download-if-missing" if source_kind == "url" else "copy-if-missing" if source_kind == "file" else "manual"
    )

    if path.exists():
        return [f"exists: {path}"]

    if sync_mode == "download-if-missing" and source_kind == "url":
        if not str(source.get("url") or "").strip():
            return [f"skip: {path} (artifact source url missing)"]
        url, expected_sha256 = validate_url_download_source(source, artifact_id=str(artifact["id"]))
        ensure_directory(path.parent, dry_run)
        if dry_run:
            return [f"download-if-missing: {url} -> {path}"]

        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read()
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"artifact {artifact['id']} digest mismatch for {url}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        tmp_path = path.parent / f".{path.name}.tmp"
        tmp_path.write_bytes(payload)
        if source.get("executable", False):
            tmp_path.chmod(0o755)
        tmp_path.replace(path)
        return [f"download-if-missing: {url} -> {path}"]

    if sync_mode == "copy-if-missing" and source_kind == "file":
        raw_source_path = str(source.get("host_path") or source.get("path") or "").strip()
        if not raw_source_path:
            return [f"skip: {path} (artifact source path missing)"]
        source_path = Path(raw_source_path)
        ensure_directory(path.parent, dry_run)
        if dry_run:
            return [f"copy-if-missing: {source_path} -> {path}"]
        if not source_path.is_file():
            raise RuntimeError(f"artifact source file is missing: {source_path}")
        shutil.copyfile(source_path, path)
        if source.get("executable", False):
            path.chmod(0o755)
        return [f"copy-if-missing: {source_path} -> {path}"]

    return [f"skip: {path} (sync mode {sync_mode})"]


def sync_env_file(env_file: dict[str, Any], dry_run: bool) -> list[str]:
    state = env_file_state(env_file)
    path = Path(state["host_path"])
    source_path = Path(state["source_host_path"]) if state["source_host_path"] else None

    if state["source_kind"] == "file" and state["sync_mode"] == "write":
        if source_path is None or not source_path.is_file():
            if env_file.get("required"):
                raise RuntimeError(
                    f"Required env file {env_file['id']} is missing source {state['source_path'] or state['source_host_path'] or path}."
                )
            return [f"skip: {path} (env source path missing)"]

        ensure_directory(path.parent, dry_run)
        if dry_run:
            return [f"hydrate-env: {source_path} -> {path}"]

        payload = source_path.read_bytes()
        current_payload = path.read_bytes() if path.is_file() else None
        desired_mode = normalize_file_mode(env_file.get("mode"), default=0o600)
        current_mode = path.stat().st_mode & 0o777 if path.is_file() else None
        if current_payload == payload and current_mode == desired_mode:
            return [f"env-unchanged: {path}"]

        path.write_bytes(payload)
        path.chmod(desired_mode)
        return [f"hydrate-env: {source_path} -> {path}"]

    if path.exists():
        return [f"exists: {path}"]

    if env_file.get("required"):
        raise RuntimeError(f"Required env file {env_file['id']} is missing at {path}.")
    return [f"skip: {path} (sync mode {state['sync_mode']})"]


def sync_dcg_config(model: dict[str, Any], root_dir: Path, dry_run: bool) -> list[str]:
    """Render .dcg.toml from env and client overlay dcg settings."""
    actions: list[str] = []
    env = model.get("env") or {}
    dcg_bin = env.get("SKILLBOX_DCG_BIN", "").strip()
    if not dcg_bin:
        return [f"skip: .dcg.toml (dcg not configured)"]

    packs_raw = env.get("SKILLBOX_DCG_PACKS", "core.git,core.filesystem").strip()
    packs = [p.strip() for p in packs_raw.split(",") if p.strip()]

    # Client overlays can declare extra dcg packs and allowlist rules
    client_dcg = {}
    for client in model.get("clients") or []:
        if "dcg" in client:
            client_dcg = client["dcg"]
            extra_packs = client_dcg.get("packs") or []
            for p in extra_packs:
                if p not in packs:
                    packs.append(p)

    allowlist = client_dcg.get("allowlist") or []

    lines = ["# Auto-generated by skillbox runtime manager. Do not edit manually.", ""]
    lines.append("[packs]")
    lines.append(f"enabled = [{', '.join(repr(p) for p in packs)}]")
    lines.append("")

    if allowlist:
        lines.append("[allowlist]")
        lines.append("rules = [")
        for rule in allowlist:
            lines.append(f"  {rule!r},")
        lines.append("]")
        lines.append("")

    content = "\n".join(lines) + "\n"

    dcg_config_path = root_dir / ".dcg.toml"
    if dcg_config_path.exists():
        existing = dcg_config_path.read_text()
        if existing == content:
            return [f"exists: {dcg_config_path}"]

    if dry_run:
        return [f"render-dcg-config: {dcg_config_path} (packs: {', '.join(packs)})"]

    dcg_config_path.write_text(content)
    actions.append(f"render-dcg-config: {dcg_config_path} (packs: {', '.join(packs)})")
    return actions


def sync_runtime(model: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []

    for repo in model["repos"]:
        path = Path(str(repo["host_path"]))
        source = repo.get("source") or {}
        source_kind = source.get("kind", "manual")
        sync = repo.get("sync") or {}
        sync_mode = sync.get("mode") or (
            "ensure-directory" if source_kind == "directory" else "external"
        )

        if path.exists():
            actions.append(f"exists: {path}")
            continue

        if sync_mode == "ensure-directory" or source_kind == "directory":
            ensure_directory(path, dry_run)
            actions.append(f"ensure-directory: {path}")
            continue

        if source_kind == "git" and sync_mode == "clone-if-missing":
            parent = path.parent
            ensure_directory(parent, dry_run)
            url = str(source["url"])
            branch = str(source.get("branch", "")).strip()
            if dry_run:
                actions.append(f"clone-if-missing: {url} -> {path}")
                continue

            args = ["git", "clone"]
            if branch:
                args.extend(["--branch", branch])
            args.extend([url, str(path)])
            result = run_command(args)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"git clone failed for {url}")
            actions.append(f"clone-if-missing: {url} -> {path}")
            continue

        actions.append(f"skip: {path} (sync mode {sync_mode})")

    for artifact in model["artifacts"]:
        actions.extend(sync_artifact(artifact, dry_run=dry_run))

    for env_file in model["env_files"]:
        actions.extend(sync_env_file(env_file, dry_run=dry_run))

    for log_item in model["logs"]:
        path = Path(str(log_item["host_path"]))
        if path.exists():
            actions.append(f"exists: {path}")
            continue
        ensure_directory(path, dry_run)
        actions.append(f"ensure-directory: {path}")

    actions.extend(sync_skill_sets(model, dry_run=dry_run))
    actions.extend(sync_dcg_config(model, DEFAULT_ROOT_DIR, dry_run=dry_run))
    if not dry_run:
        emit_event("sync.completed", "runtime", {"action_count": len(actions)})
    return actions


def runtime_log_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(log_item["id"]): log_item
        for log_item in model.get("logs") or []
        if str(log_item.get("id", "")).strip()
    }


def runtime_repo_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(repo["id"]): repo
        for repo in model.get("repos") or []
        if str(repo.get("id", "")).strip()
    }


def task_id_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(task["id"]): task
        for task in model.get("tasks") or []
        if str(task.get("id", "")).strip()
    }


def task_dependency_ids(task: dict[str, Any]) -> list[str]:
    raw_dependencies = task.get("depends_on") or []
    if not isinstance(raw_dependencies, list):
        return []

    dependencies: list[str] = []
    seen: set[str] = set()
    for raw_dependency in raw_dependencies:
        dependency_id = str(raw_dependency).strip()
        if not dependency_id or dependency_id in seen:
            continue
        dependencies.append(dependency_id)
        seen.add(dependency_id)
    return dependencies


def service_bootstrap_task_ids(service: dict[str, Any]) -> list[str]:
    raw_tasks = service.get("bootstrap_tasks") or []
    if not isinstance(raw_tasks, list):
        return []

    task_ids: list[str] = []
    seen: set[str] = set()
    for raw_task in raw_tasks:
        task_id = str(raw_task).strip()
        if not task_id or task_id in seen:
            continue
        task_ids.append(task_id)
        seen.add(task_id)
    return task_ids


def task_dependency_graph(model: dict[str, Any]) -> dict[str, list[str]]:
    return {
        task_id: task_dependency_ids(task)
        for task_id, task in task_id_map(model).items()
    }


def expand_graph_ids(graph: dict[str, list[str]], root_ids: list[str]) -> set[str]:
    expanded = set(root_ids)
    queue = list(root_ids)

    while queue:
        item_id = queue.pop()
        for linked_item_id in graph.get(item_id, []):
            if linked_item_id in expanded:
                continue
            expanded.add(linked_item_id)
            queue.append(linked_item_id)

    return expanded


def order_task_ids(model: dict[str, Any], selected_ids: set[str]) -> list[str]:
    tasks_by_id = task_id_map(model)
    dependency_graph = task_dependency_graph(model)
    ordered_ids: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise RuntimeError(f"Task dependency cycle detected at {task_id}.")
        if task_id not in tasks_by_id:
            raise RuntimeError(f"Task dependency references unknown task {task_id!r}.")

        visiting.add(task_id)
        for dependency_id in dependency_graph.get(task_id, []):
            if dependency_id in selected_ids:
                visit(dependency_id)
        visiting.remove(task_id)
        visited.add(task_id)
        ordered_ids.append(task_id)

    for task in model["tasks"]:
        task_id = str(task.get("id", "")).strip()
        if task_id and task_id in selected_ids:
            visit(task_id)

    return ordered_ids


def service_supports_lifecycle(service: dict[str, Any]) -> tuple[bool, str | None]:
    if not str(service.get("command") or "").strip():
        return False, "command missing"
    if str(service.get("kind") or "").strip() == "orchestration":
        return False, "orchestration services are status-only"
    return True, None


def service_dependency_ids(service: dict[str, Any]) -> list[str]:
    raw_dependencies = service.get("depends_on") or []
    if not isinstance(raw_dependencies, list):
        return []

    dependencies: list[str] = []
    seen: set[str] = set()
    for raw_dependency in raw_dependencies:
        dependency_id = str(raw_dependency).strip()
        if not dependency_id or dependency_id in seen:
            continue
        dependencies.append(dependency_id)
        seen.add(dependency_id)
    return dependencies


def service_id_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(service["id"]): service
        for service in model["services"]
        if str(service.get("id", "")).strip()
    }


def service_dependency_graph(model: dict[str, Any]) -> dict[str, list[str]]:
    return {
        service_id: service_dependency_ids(service)
        for service_id, service in service_id_map(model).items()
    }


def reverse_service_dependency_graph(model: dict[str, Any]) -> dict[str, list[str]]:
    reverse_graph: dict[str, list[str]] = {
        service_id: []
        for service_id in service_id_map(model)
    }
    for service_id, dependency_ids in service_dependency_graph(model).items():
        for dependency_id in dependency_ids:
            reverse_graph.setdefault(dependency_id, []).append(service_id)
    return reverse_graph


def order_service_ids(model: dict[str, Any], selected_ids: set[str]) -> list[str]:
    services_by_id = service_id_map(model)
    dependency_graph = service_dependency_graph(model)
    ordered_ids: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(service_id: str) -> None:
        if service_id in visited:
            return
        if service_id in visiting:
            raise RuntimeError(f"Service dependency cycle detected at {service_id}.")
        if service_id not in services_by_id:
            raise RuntimeError(f"Service dependency references unknown service {service_id!r}.")

        visiting.add(service_id)
        for dependency_id in dependency_graph.get(service_id, []):
            if dependency_id in selected_ids:
                visit(dependency_id)
        visiting.remove(service_id)
        visited.add(service_id)
        ordered_ids.append(service_id)

    for service in model["services"]:
        service_id = str(service.get("id", "")).strip()
        if service_id and service_id in selected_ids:
            visit(service_id)

    return ordered_ids


def resolve_services_for_start(
    model: dict[str, Any],
    requested_services: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requested_ids = [str(service["id"]) for service in requested_services]
    expanded_ids = expand_graph_ids(service_dependency_graph(model), requested_ids)
    ordered_ids = order_service_ids(model, expanded_ids)
    services_by_id = service_id_map(model)
    return [services_by_id[service_id] for service_id in ordered_ids]


def resolve_services_for_stop(
    model: dict[str, Any],
    requested_services: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requested_ids = [str(service["id"]) for service in requested_services]
    expanded_ids = expand_graph_ids(reverse_service_dependency_graph(model), requested_ids)
    ordered_ids = list(reversed(order_service_ids(model, expanded_ids)))
    services_by_id = service_id_map(model)
    return [services_by_id[service_id] for service_id in ordered_ids]


def translated_runtime_env(root_dir: Path, runtime_env: dict[str, str]) -> dict[str, str]:
    translated: dict[str, str] = {}
    for key, value in runtime_env.items():
        if key in {"SKILLBOX_MONOSERVER_HOST_ROOT", "SKILLBOX_CLIENTS_HOST_ROOT"}:
            translated[key] = str(host_path_to_absolute_path(root_dir, value))
            continue
        if key in PATH_LIKE_ENV_KEYS and value:
            translated[key] = str(runtime_path_to_host_path(root_dir, runtime_env, value))
            continue
        translated[key] = value
    translated["ROOT_DIR"] = str(root_dir)
    return translated


def translate_runtime_paths(value: str, runtime_env: dict[str, str], translated_env: dict[str, str]) -> str:
    translated = value
    replacements: list[tuple[str, str]] = []
    for key in PATH_LIKE_ENV_KEYS:
        runtime_path = str(runtime_env.get(key, "")).strip()
        host_path = str(translated_env.get(key, "")).strip()
        if runtime_path and host_path and runtime_path != host_path:
            replacements.append((runtime_path, host_path))

    for runtime_path, host_path in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        translated = translated.replace(runtime_path, host_path)
    return translated


def runtime_item_log_dir(model: dict[str, Any], item: dict[str, Any]) -> Path:
    log_map = runtime_log_map(model)
    log_id = str(item.get("log") or "").strip()
    log_dir: Path
    if log_id and log_id in log_map:
        log_dir = Path(str(log_map[log_id]["host_path"]))
    elif "runtime" in log_map:
        log_dir = Path(str(log_map["runtime"]["host_path"]))
    else:
        log_dir = Path(str(model["root_dir"])) / "logs" / "runtime"
    return log_dir


def service_paths(model: dict[str, Any], service: dict[str, Any]) -> dict[str, Path]:
    log_dir = runtime_item_log_dir(model, service)
    service_slug = str(service["id"])
    return {
        "log_dir": log_dir,
        "log_file": log_dir / f"{service_slug}.log",
        "pid_file": log_dir / f"{service_slug}.pid",
    }


def task_paths(model: dict[str, Any], task: dict[str, Any]) -> dict[str, Path]:
    log_dir = runtime_item_log_dir(model, task)
    task_slug = str(task["id"])
    return {
        "log_dir": log_dir,
        "log_file": log_dir / f"{task_slug}.log",
    }


def read_service_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    raw_value = pid_path.read_text(encoding="utf-8").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        return None


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def remove_pid_file(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except FileNotFoundError:
        return


def live_service_pid(pid_path: Path) -> int | None:
    pid = read_service_pid(pid_path)
    if pid is None:
        return None
    if process_is_running(pid):
        return pid
    remove_pid_file(pid_path)
    return None


def service_manager_state(model: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    manageable, reason = service_supports_lifecycle(service)
    paths = service_paths(model, service)
    pid = live_service_pid(paths["pid_file"])
    return {
        "managed": manageable,
        "manager_reason": reason,
        "pid": pid,
        "pid_file": str(paths["pid_file"]),
        "log_file": str(paths["log_file"]),
        "log_present": paths["log_file"].is_file(),
    }


def resolve_runtime_command_cwd(model: dict[str, Any], item: dict[str, Any]) -> Path:
    repo_id = str(item.get("repo") or "").strip()
    repo = runtime_repo_map(model).get(repo_id)
    if repo is not None:
        return Path(str(repo["host_path"]))

    host_path = str(item.get("host_path") or "").strip()
    if host_path:
        candidate = Path(host_path)
        return candidate if candidate.is_dir() else candidate.parent

    return Path(str(model["root_dir"]))


def translated_runtime_command(model: dict[str, Any], item: dict[str, Any]) -> tuple[str, dict[str, str]]:
    root_dir = Path(str(model["root_dir"]))
    runtime_env = dict(model.get("env") or {})
    translated_env = translated_runtime_env(root_dir, runtime_env)
    command = translate_runtime_paths(str(item["command"]), runtime_env, translated_env)
    env = os.environ.copy()
    env.update(translated_env)
    return command, env


def task_success_state(task: dict[str, Any]) -> dict[str, Any]:
    success = task.get("success") or {}
    success_type = success.get("type")
    if success_type == "path_exists":
        path = Path(str(success["host_path"]))
        return {"state": "ok" if path.exists() else "down", "target": str(path)}
    return {"state": "unknown"}


def probe_task(model: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    success_state = task_success_state(task)
    tasks_by_id = task_id_map(model)
    dependency_states = {
        dependency_id: task_success_state(tasks_by_id[dependency_id]).get("state", "unknown")
        for dependency_id in task_dependency_ids(task)
        if dependency_id in tasks_by_id
    }

    if success_state.get("state") == "ok":
        state = "ready"
    elif any(dependency_state != "ok" for dependency_state in dependency_states.values()):
        state = "blocked"
    else:
        state = "pending"

    result = {
        "state": state,
        "depends_on": task_dependency_ids(task),
        "dependency_states": dependency_states,
    }
    if success_state.get("target"):
        result["target"] = success_state["target"]
    return result


def service_healthcheck_state(service: dict[str, Any]) -> dict[str, Any]:
    healthcheck = service.get("healthcheck") or {}
    healthcheck_type = healthcheck.get("type")
    if not healthcheck_type:
        return {"state": "declared"}

    if healthcheck_type == "path_exists":
        path = Path(str(healthcheck["host_path"]))
        return {"state": "ok" if path.exists() else "down", "target": str(path)}

    if healthcheck_type == "http":
        url = str(healthcheck["url"])
        timeout = float(healthcheck.get("timeout_seconds", 0.5))
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return {"state": "ok", "status_code": response.getcode(), "url": url}
        except (urllib.error.URLError, TimeoutError, ValueError):
            return {"state": "down", "url": url}

    return {"state": "unknown"}


def wait_for_service_health(
    service: dict[str, Any],
    process: subprocess.Popen[str],
    wait_seconds: float,
) -> dict[str, Any]:
    healthcheck = service.get("healthcheck") or {}
    if not healthcheck.get("type"):
        if process.poll() is not None:
            return {"state": "failed", "exit_code": process.returncode}
        return {"state": "started"}

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() <= deadline:
        if process.poll() is not None:
            return {"state": "failed", "exit_code": process.returncode}

        probe = service_healthcheck_state(service)
        if probe.get("state") == "ok":
            return {"state": "ok"} | probe
        time.sleep(0.25)

    return {"state": "timeout"} | service_healthcheck_state(service)


def tail_lines(path: Path, line_count: int) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if line_count <= 0:
        return lines
    return lines[-line_count:]


def stop_process(pid: int, wait_seconds: float) -> tuple[str, int | None]:
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return "not-running", None

    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return "not-running", None

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() <= deadline:
        if not process_is_running(pid):
            return "stopped", signal.SIGTERM
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return "stopped", signal.SIGTERM

    deadline = time.monotonic() + 1.0
    while time.monotonic() <= deadline:
        if not process_is_running(pid):
            return "killed", signal.SIGKILL
        time.sleep(0.1)

    return "stuck", None


def select_tasks(model: dict[str, Any], task_ids: list[str] | None) -> list[dict[str, Any]]:
    requested_ids = [task_id.strip() for task_id in task_ids or [] if task_id.strip()]
    available = {
        str(task["id"]): task
        for task in model["tasks"]
        if str(task.get("id", "")).strip()
    }
    unknown = sorted(task_id for task_id in requested_ids if task_id not in available)
    if unknown:
        raise RuntimeError(
            "Unknown task id(s): "
            + ", ".join(unknown)
            + ". Available tasks: "
            + (", ".join(sorted(available)) or "(none)")
        )
    if not requested_ids:
        return list(model["tasks"])

    requested = set(requested_ids)
    return [task for task in model["tasks"] if task["id"] in requested]


def resolve_tasks_for_run(
    model: dict[str, Any],
    requested_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requested_ids = [str(task["id"]) for task in requested_tasks]
    expanded_ids = expand_graph_ids(task_dependency_graph(model), requested_ids)
    ordered_ids = order_task_ids(model, expanded_ids)
    tasks_by_id = task_id_map(model)
    return [tasks_by_id[task_id] for task_id in ordered_ids]


def resolve_tasks_for_services(
    model: dict[str, Any],
    services: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    root_task_ids: list[str] = []
    for service in services:
        root_task_ids.extend(service_bootstrap_task_ids(service))
    if not root_task_ids:
        return []
    expanded_ids = expand_graph_ids(task_dependency_graph(model), root_task_ids)
    ordered_ids = order_task_ids(model, expanded_ids)
    tasks_by_id = task_id_map(model)
    return [tasks_by_id[task_id] for task_id in ordered_ids]


def select_services(model: dict[str, Any], service_ids: list[str] | None) -> list[dict[str, Any]]:
    requested_ids = [service_id.strip() for service_id in service_ids or [] if service_id.strip()]
    available = {
        str(service["id"]): service
        for service in model["services"]
        if str(service.get("id", "")).strip()
    }
    unknown = sorted(service_id for service_id in requested_ids if service_id not in available)
    if unknown:
        raise RuntimeError(
            "Unknown service id(s): "
            + ", ".join(unknown)
            + ". Available services: "
            + (", ".join(sorted(available)) or "(none)")
        )
    if not requested_ids:
        return list(model["services"])

    requested = set(requested_ids)
    return [service for service in model["services"] if service["id"] in requested]


def select_env_files_for_services(model: dict[str, Any], services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not services:
        return list(model["env_files"])

    repo_ids = {
        str(service.get("repo") or "").strip()
        for service in services
        if str(service.get("repo") or "").strip()
    }
    return [
        env_file
        for env_file in model["env_files"]
        if not str(env_file.get("repo") or "").strip() or str(env_file.get("repo") or "").strip() in repo_ids
    ]


def select_env_files_for_tasks(model: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not tasks:
        return []

    repo_ids = {
        str(task.get("repo") or "").strip()
        for task in tasks
        if str(task.get("repo") or "").strip()
    }
    return [
        env_file
        for env_file in model["env_files"]
        if not str(env_file.get("repo") or "").strip() or str(env_file.get("repo") or "").strip() in repo_ids
    ]


def ensure_required_env_files_ready(env_files: list[dict[str, Any]]) -> None:
    unresolved: list[str] = []
    for env_file in env_files:
        state = env_file_state(env_file)
        if not env_file.get("required") or state["state"] == "ok":
            continue
        detail = state["state"]
        if state["state"] == "source-missing" and state["source_path"]:
            detail = f"{detail}: {state['source_path']}"
        unresolved.append(f"{env_file['id']} ({detail})")

    if unresolved:
        raise RuntimeError(f"Required env files are not ready: {', '.join(unresolved)}")


def run_tasks(
    model: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for task in tasks:
        paths = task_paths(model, task)
        result = {
            "id": task["id"],
            "kind": task.get("kind", "task"),
            "log_file": str(paths["log_file"]),
            "depends_on": task_dependency_ids(task),
        }
        task_state = probe_task(model, task)
        if task_state["state"] == "ready":
            results.append(result | {"result": "ready", "target": task_state.get("target")})
            continue
        if task_state["state"] == "blocked":
            blocked_on = [
                dependency_id
                for dependency_id, dependency_state in task_state.get("dependency_states", {}).items()
                if dependency_state != "ok"
            ]
            raise RuntimeError(
                f"Task {task['id']} is blocked by incomplete dependencies: {', '.join(blocked_on)}"
            )

        command, env = translated_runtime_command(model, task)
        cwd = resolve_runtime_command_cwd(model, task)
        result["command"] = command
        result["cwd"] = str(cwd)

        ensure_directory(paths["log_dir"], dry_run)
        if dry_run:
            results.append(result | {"result": "dry-run"})
            continue

        emit_event("task.started", task["id"])
        with paths["log_file"].open("a", encoding="utf-8") as log_handle:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=True,
                text=True,
                check=False,
            )

        if completed.returncode != 0:
            tail = tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)
            emit_event("task.failed", task["id"], {"exit_code": completed.returncode})
            raise RuntimeError(
                f"Task {task['id']} failed with exit code {completed.returncode}."
                + (f" Recent logs: {' | '.join(tail)}" if tail else "")
            )

        post_state = probe_task(model, task)
        if post_state["state"] != "ready":
            tail = tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)
            emit_event("task.failed", task["id"], {"reason": "success_check_unsatisfied"})
            raise RuntimeError(
                f"Task {task['id']} completed but did not satisfy its success check."
                + (f" Success target: {post_state['target']}." if post_state.get("target") else "")
                + (f" Recent logs: {' | '.join(tail)}" if tail else "")
            )

        results.append(result | {"result": "completed", "target": post_state.get("target")})
        emit_event("task.completed", task["id"])
    return results


def start_services(
    model: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    dry_run: bool,
    wait_seconds: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for service in services:
        manageable, reason = service_supports_lifecycle(service)
        paths = service_paths(model, service)
        result = {
            "id": service["id"],
            "kind": service.get("kind", "service"),
            "log_file": str(paths["log_file"]),
            "pid_file": str(paths["pid_file"]),
        }

        if not manageable:
            results.append(result | {"result": "skipped", "reason": reason})
            continue

        pid = live_service_pid(paths["pid_file"])
        if pid is not None:
            results.append(result | {"result": "already-running", "pid": pid})
            continue

        command, env = translated_runtime_command(model, service)
        cwd = resolve_runtime_command_cwd(model, service)
        result["command"] = command
        result["cwd"] = str(cwd)

        ensure_directory(paths["log_dir"], dry_run)
        if dry_run:
            results.append(result | {"result": "dry-run"})
            continue

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

        paths["pid_file"].write_text(f"{process.pid}\n", encoding="utf-8")
        health_state = wait_for_service_health(service, process, wait_seconds)
        if health_state.get("state") in {"failed", "timeout"}:
            stop_process(process.pid, DEFAULT_SERVICE_STOP_WAIT_SECONDS)
            remove_pid_file(paths["pid_file"])
            tail = tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)
            detail = result | {"result": "failed", "tail": tail}
            if "exit_code" in health_state:
                detail["exit_code"] = health_state["exit_code"]
            if "url" in health_state:
                detail["url"] = health_state["url"]
            if "target" in health_state:
                detail["target"] = health_state["target"]
            emit_event("service.start_failed", service["id"], {"state": health_state.get("state")})
            raise RuntimeError(
                f"Service {service['id']} failed to become healthy."
                + (f" Exit code: {health_state['exit_code']}." if "exit_code" in health_state else "")
                + (f" Health target: {health_state['url']}." if "url" in health_state else "")
                + (f" Health target: {health_state['target']}." if "target" in health_state else "")
                + (f" Recent logs: {' | '.join(tail)}" if tail else "")
            )

        results.append(result | {"result": "started", "pid": process.pid})
        emit_event("service.started", service["id"], {"pid": process.pid})
    return results


def stop_services(
    model: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    dry_run: bool,
    wait_seconds: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for service in services:
        manageable, reason = service_supports_lifecycle(service)
        paths = service_paths(model, service)
        result = {
            "id": service["id"],
            "kind": service.get("kind", "service"),
            "log_file": str(paths["log_file"]),
            "pid_file": str(paths["pid_file"]),
        }

        if not manageable:
            results.append(result | {"result": "skipped", "reason": reason})
            continue

        pid = live_service_pid(paths["pid_file"])
        if pid is None:
            external_state = service_healthcheck_state(service)
            if external_state.get("state") == "ok":
                results.append(result | {"result": "external"})
            else:
                results.append(result | {"result": "not-running"})
            continue

        if dry_run:
            results.append(result | {"result": "dry-run", "pid": pid})
            continue

        stop_result, signal_used = stop_process(pid, wait_seconds)
        remove_pid_file(paths["pid_file"])
        results.append(
            result
            | {
                "result": stop_result,
                "pid": pid,
                "signal": signal_used,
            }
        )
        emit_event("service.stopped", service["id"], {"signal": signal_used})
    return results


def collect_service_logs(
    model: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    line_count: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for service in services:
        paths = service_paths(model, service)
        log_file = paths["log_file"]
        results.append(
            {
                "id": service["id"],
                "kind": service.get("kind", "service"),
                "log_file": str(log_file),
                "present": log_file.is_file(),
                "lines": tail_lines(log_file, line_count),
            }
        )
    return results


def git_repo_state(path: Path) -> dict[str, Any]:
    top_level = run_command(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if top_level.returncode != 0:
        return {"git": False}

    if Path(top_level.stdout.strip()).resolve() != path.resolve():
        return {"git": False}

    result = run_command(["git", "status", "--short", "--branch"], cwd=path)
    if result.returncode != 0:
        return {"git": False}

    branch = ""
    dirty = 0
    untracked = 0
    for index, line in enumerate(result.stdout.splitlines()):
        if index == 0 and line.startswith("## "):
            branch = line[3:].strip()
            continue
        if not line.strip():
            continue
        if line.startswith("?? "):
            untracked += 1
        else:
            dirty += 1

    return {"git": True, "branch": branch, "dirty": dirty, "untracked": untracked}


def log_directory_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"present": False, "files": 0, "bytes": 0}

    file_count = 0
    total_bytes = 0
    for child in path.rglob("*"):
        if child.is_file():
            file_count += 1
            total_bytes += child.stat().st_size
    return {"present": True, "files": file_count, "bytes": total_bytes}


def probe_service(model: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    manager_state = service_manager_state(model, service)
    health_state = service_healthcheck_state(service)
    pid = manager_state.get("pid")

    if pid is not None:
        if health_state.get("state") == "ok":
            state = "running"
        elif health_state.get("state") == "declared":
            state = "running"
        else:
            state = "starting"
    else:
        state = health_state.get("state", "declared")

    return {
        "state": state,
        "managed": manager_state["managed"],
        "manager_reason": manager_state["manager_reason"],
        "pid": pid,
        "pid_file": manager_state["pid_file"],
        "log_file": manager_state["log_file"],
        "log_present": manager_state["log_present"],
    } | {
        key: value
        for key, value in health_state.items()
        if key != "state"
    }


FOCUS_STATE_REL = Path("workspace") / ".focus.json"
FOCUS_ERROR_PATTERNS = re.compile(
    r"(?:error|exception|traceback|fatal|panic|fail(?:ed|ure)?)",
    re.IGNORECASE,
)


def collect_live_state(model: dict[str, Any]) -> dict[str, Any]:
    """Snapshot volatile runtime state: git branches, service health, check results, recent errors."""
    repo_states: list[dict[str, Any]] = []
    for repo in model.get("repos") or []:
        path = Path(str(repo["host_path"]))
        item: dict[str, Any] = {
            "id": repo["id"],
            "path": str(repo["path"]),
            "present": path.exists(),
        }
        if path.exists() and path.is_dir():
            git_state = git_repo_state(path)
            item.update(git_state)
            if git_state.get("git"):
                log_result = run_command(
                    ["git", "log", "--oneline", "-1"], cwd=path,
                )
                if log_result.returncode == 0:
                    item["last_commit"] = log_result.stdout.strip()
        repo_states.append(item)

    service_states: list[dict[str, Any]] = []
    for service in model.get("services") or []:
        probe = probe_service(model, service)
        service_states.append({
            "id": service["id"],
            "kind": service.get("kind", "service"),
            "state": probe.get("state", "declared"),
            "pid": probe.get("pid"),
            "healthy": probe.get("state") == "running",
        })

    check_states: list[dict[str, Any]] = []
    for check in model.get("checks") or []:
        item = {"id": check["id"], "type": check["type"], "ok": False}
        if check["type"] == "path_exists":
            item["ok"] = Path(str(check["host_path"])).exists()
        check_states.append(item)

    log_states: list[dict[str, Any]] = []
    for log_item in model.get("logs") or []:
        path = Path(str(log_item["host_path"]))
        item = {
            "id": log_item["id"],
            "path": str(log_item["path"]),
            "present": path.exists(),
            "recent_errors": [],
        }
        if path.exists():
            # Scan the most recently modified log file for error-like lines.
            log_files = sorted(
                (f for f in path.rglob("*") if f.is_file()),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            if log_files:
                lines = tail_lines(log_files[0], 100)
                errors = [
                    line for line in lines if FOCUS_ERROR_PATTERNS.search(line)
                ]
                item["recent_errors"] = errors[-5:]  # Keep at most 5
                item["scanned_file"] = str(log_files[0].name)
        log_states.append(item)

    return {
        "collected_at": time.time(),
        "repos": repo_states,
        "services": service_states,
        "checks": check_states,
        "logs": log_states,
    }


def runtime_status(model: dict[str, Any]) -> dict[str, Any]:
    repo_statuses: list[dict[str, Any]] = []
    for repo in model["repos"]:
        path = Path(str(repo["host_path"]))
        item = {
            "id": repo["id"],
            "kind": repo.get("kind", "repo"),
            "path": str(repo["path"]),
            "host_path": str(path),
            "present": path.exists(),
            "profiles": repo.get("profiles") or [],
        }
        if path.exists() and path.is_dir():
            item.update(git_repo_state(path))
        repo_statuses.append(item)

    artifact_statuses: list[dict[str, Any]] = []
    for artifact in model["artifacts"]:
        path = Path(str(artifact["host_path"]))
        source = artifact.get("source") or {}
        item = {
            "id": artifact["id"],
            "kind": artifact.get("kind", "artifact"),
            "path": str(artifact["path"]),
            "host_path": str(path),
            "present": path.exists(),
            "profiles": artifact.get("profiles") or [],
            "source_kind": source.get("kind", "manual"),
        }
        artifact_statuses.append(item)

    env_file_statuses = [env_file_state(env_file) for env_file in model["env_files"]]

    skill_statuses: list[dict[str, Any]] = []
    for skillset in model["skills"]:
        inventory = collect_skill_inventory(skillset)
        skill_statuses.append(
            {
                "id": inventory["id"],
                "kind": inventory["kind"],
                "bundle_dir": inventory["bundle_dir"],
                "bundle_dir_host_path": inventory["bundle_dir_host_path"],
                "manifest": inventory["manifest"],
                "lock_path": inventory["lock_path"],
                "lock_present": inventory["lock_present"],
                "lock_error": inventory["lock_error"],
                "missing_bundles": inventory["missing_bundles"],
                "extra_bundles": inventory["extra_bundles"],
                "skills": inventory["skills"],
            }
        )

    task_statuses: list[dict[str, Any]] = []
    for task in model["tasks"]:
        item = {
            "id": task["id"],
            "kind": task.get("kind", "task"),
            "profiles": task.get("profiles") or [],
            "depends_on": task_dependency_ids(task),
            "inputs": list(task.get("inputs") or []),
            "outputs": list(task.get("outputs") or []),
        }
        item.update(probe_task(model, task))
        task_statuses.append(item)

    service_statuses: list[dict[str, Any]] = []
    for service in model["services"]:
        item = {
            "id": service["id"],
            "kind": service.get("kind", "service"),
            "profiles": service.get("profiles") or [],
            "depends_on": service_dependency_ids(service),
            "bootstrap_tasks": service_bootstrap_task_ids(service),
        }
        item.update(probe_service(model, service))
        service_statuses.append(item)

    log_statuses: list[dict[str, Any]] = []
    for log_item in model["logs"]:
        path = Path(str(log_item["host_path"]))
        item = {
            "id": log_item["id"],
            "path": str(log_item["path"]),
            "host_path": str(path),
        }
        item.update(log_directory_state(path))
        log_statuses.append(item)

    check_statuses: list[dict[str, Any]] = []
    for check in model["checks"]:
        item = {
            "id": check["id"],
            "type": check["type"],
        }
        if check["type"] == "path_exists":
            path = Path(str(check["host_path"]))
            item["path"] = str(check["path"])
            item["host_path"] = str(path)
            item["ok"] = path.exists()
        check_statuses.append(item)

    return {
        "clients": copy.deepcopy(model.get("clients") or []),
        "active_clients": model.get("active_clients") or [],
        "default_client": (model.get("selection") or {}).get("default_client"),
        "active_profiles": model.get("active_profiles") or [],
        "repos": repo_statuses,
        "artifacts": artifact_statuses,
        "env_files": env_file_statuses,
        "skills": skill_statuses,
        "tasks": task_statuses,
        "services": service_statuses,
        "logs": log_statuses,
        "checks": check_statuses,
    }


def generate_context_markdown(model: dict[str, Any]) -> str:
    """Generate a CLAUDE.md / AGENTS.md from the resolved runtime model."""
    lines: list[str] = []

    active_clients = model.get("active_clients") or []
    active_profiles = [p for p in (model.get("active_profiles") or []) if p != "core"]
    clients_data = {
        str(c.get("id", "")): c
        for c in model.get("clients") or []
    }

    # Build make suffix for commands
    make_parts: list[str] = []
    if active_clients:
        make_parts.append(f"CLIENT={active_clients[0]}")

    make_suffix = " " + " ".join(make_parts) if make_parts else ""

    # Determine default CWD from active client
    default_cwd = ""
    for client_id in active_clients:
        client_data = clients_data.get(client_id, {})
        cwd = str(client_data.get("default_cwd", "")).strip()
        if cwd:
            default_cwd = cwd
            break

    # Regenerate hint
    regen_cmd = f"make context{make_suffix}"
    sync_cmd = f"make runtime-sync{make_suffix}"

    # Header
    lines.append("# skillbox")
    lines.append("")
    lines.append(f"> Auto-generated from the runtime graph. Do not edit manually.")
    lines.append(f"> Regenerate: `{regen_cmd}` or `{sync_cmd}`.")
    lines.append("")
    lines.append("You are inside a skillbox workspace container.")
    lines.append("")

    # Environment
    lines.append("## Environment")
    lines.append("")
    if active_clients:
        lines.append(f"- Client: **{', '.join(active_clients)}**")
    if default_cwd:
        lines.append(f"- Default CWD: `{default_cwd}`")
    if active_profiles:
        lines.append(f"- Profiles: {', '.join(active_profiles)}")

    # Skill context pointer
    runtime_env = model.get("env") or {}
    for cid_env in active_clients:
        client_data_env = clients_data.get(cid_env, {})
        if client_data_env.get("context"):
            ctx_path = client_config_runtime_dir(runtime_env, cid_env) / "context.yaml"
            lines.append(f"- Skill context: `$SKILLBOX_CLIENT_CONTEXT` → `{ctx_path}`")
            break
    lines.append("")

    # Repos
    repos = model.get("repos") or []
    if repos:
        lines.append("## Repos")
        lines.append("")
        lines.append("| ID | Path | Kind |")
        lines.append("|----|------|------|")
        for repo in repos:
            lines.append(
                f"| {repo['id']} | `{repo['path']}` | {repo.get('kind', 'repo')} |"
            )
        lines.append("")

    # Services
    services = model.get("services") or []
    if services:
        lines.append("## Services")
        lines.append("")
        for service in services:
            sid = service["id"]
            kind = service.get("kind", "service")
            profiles = service.get("profiles") or []
            profile_label = ", ".join(profiles) or "core"
            manageable, reason = service_supports_lifecycle(service)

            if manageable:
                svc_parts = list(make_parts)
                non_core = [p for p in profiles if p != "core"]
                if non_core:
                    svc_parts.append(f"PROFILE={non_core[0]}")
                svc_parts.append(f"SERVICE={sid}")
                svc_suffix = " " + " ".join(svc_parts)

                deps = service_dependency_ids(service)
                dep_note = f" (depends on: {', '.join(deps)})" if deps else ""

                lines.append(f"- **{sid}** ({kind}, {profile_label}){dep_note}")
                lines.append(f"  - Start: `make runtime-up{svc_suffix}`")
                lines.append(f"  - Stop: `make runtime-down{svc_suffix}`")
                lines.append(f"  - Logs: `make runtime-logs{svc_suffix}`")
            else:
                lines.append(
                    f"- **{sid}** ({kind}, {profile_label})"
                    f" — {reason or 'not manageable'}"
                )
        lines.append("")

    # Tasks
    tasks = model.get("tasks") or []
    if tasks:
        lines.append("## Tasks")
        lines.append("")
        for task in tasks:
            tid = task["id"]
            deps = task_dependency_ids(task)
            dep_note = f" (depends on: {', '.join(deps)})" if deps else ""

            task_parts = list(make_parts)
            task_parts.append(f"TASK={tid}")
            task_suffix = " " + " ".join(task_parts)

            lines.append(
                f"- **{tid}**{dep_note}: `make runtime-bootstrap{task_suffix}`"
            )
        lines.append("")

    # Installed skills
    skills = model.get("skills") or []
    if skills:
        lines.append("## Installed Skills")
        lines.append("")
        for skillset in skills:
            sid = skillset["id"]
            manifest_host_path = Path(
                str(skillset.get("manifest_host_path", ""))
            )
            skill_names: list[str] = []
            if manifest_host_path.is_file():
                try:
                    skill_names = read_manifest_skills(manifest_host_path)
                except Exception:
                    pass

            if skill_names:
                lines.append(f"- **{sid}**: {', '.join(skill_names)}")
            else:
                lines.append(f"- **{sid}**: (empty)")
        lines.append("")

    # Logs
    logs = model.get("logs") or []
    if logs:
        lines.append("## Logs")
        lines.append("")
        lines.append("| ID | Path |")
        lines.append("|----|------|")
        for log_item in logs:
            lines.append(f"| {log_item['id']} | `{log_item['path']}` |")
        lines.append("")

    # Quick reference
    lines.append("## Quick Reference")
    lines.append("")
    lines.append("```bash")
    lines.append(f"make dev-sanity{make_suffix}")
    lines.append(f"make runtime-status{make_suffix}")
    lines.append(f"make runtime-sync{make_suffix}")
    lines.append(f"make runtime-up{make_suffix} SERVICE=<id>")
    lines.append(f"make runtime-down{make_suffix} SERVICE=<id>")
    lines.append(f"make runtime-logs{make_suffix} SERVICE=<id>")
    if tasks:
        lines.append(f"make runtime-bootstrap{make_suffix} TASK=<id>")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def generate_live_context_markdown(
    model: dict[str, Any], live_state: dict[str, Any],
    root_dir: Path = DEFAULT_ROOT_DIR,
) -> str:
    """Generate enriched CLAUDE.md / AGENTS.md with live runtime state."""
    base = generate_context_markdown(model)
    lines: list[str] = [base.rstrip()]

    # --- Live Service Status ---
    svc_states = live_state.get("services") or []
    if svc_states:
        lines.append("")
        lines.append("## Live Status")
        lines.append("")
        lines.append("| Service | State | PID | Healthy |")
        lines.append("|---------|-------|-----|---------|")
        for svc in svc_states:
            pid = str(svc.get("pid") or "-")
            healthy = "yes" if svc.get("healthy") else "no"
            state = svc.get("state", "unknown")
            lines.append(f"| {svc['id']} | {state} | {pid} | {healthy} |")
        lines.append("")

    # --- Repo State ---
    repo_states = live_state.get("repos") or []
    git_repos = [r for r in repo_states if r.get("git")]
    if git_repos:
        lines.append("## Repo State")
        lines.append("")
        lines.append("| Repo | Branch | Dirty | Untracked | Last Commit |")
        lines.append("|------|--------|-------|-----------|-------------|")
        for repo in git_repos:
            branch = repo.get("branch", "-")
            dirty = str(repo.get("dirty", 0))
            untracked = str(repo.get("untracked", 0))
            last_commit = repo.get("last_commit", "-")
            lines.append(
                f"| {repo['id']} | `{branch}` | {dirty} | {untracked} | {last_commit} |"
            )
        lines.append("")

    # --- Attention ---
    attention: list[str] = []

    # Failing checks
    for check in live_state.get("checks") or []:
        if not check.get("ok"):
            attention.append(f"CHECK FAIL: **{check['id']}** ({check['type']})")

    # Non-running services
    for svc in svc_states:
        if svc.get("state") in ("stopped", "not-running", "declared"):
            attention.append(
                f"SERVICE DOWN: **{svc['id']}** (state: {svc['state']})"
            )
        elif svc.get("state") == "starting":
            attention.append(
                f"SERVICE STARTING: **{svc['id']}** — may not be healthy yet"
            )

    # Recent errors from logs
    for log_item in live_state.get("logs") or []:
        errors = log_item.get("recent_errors") or []
        if errors:
            scanned = log_item.get("scanned_file", "")
            file_note = f" ({scanned})" if scanned else ""
            attention.append(
                f"RECENT ERRORS in **{log_item['id']}**{file_note}:"
            )
            for err_line in errors[-3:]:
                attention.append(f"  `{err_line.strip()[:120]}`")

    # --- Recent Activity (from journal, excluding acked events) ---
    acks = read_acks(root_dir)
    recent = query_journal(root_dir, limit=20)
    unacked = [ev for ev in recent if not is_acked(acks, ev["ts"])]
    acked_count = len(recent) - len(unacked)
    if unacked:
        lines.append("## Recent Activity")
        lines.append("")
        for ev in unacked:
            ts_str = time.strftime("%H:%M", time.localtime(ev["ts"]))
            lines.append(f"- `{ts_str}` **{ev['type']}** {ev['subject']}")
        if acked_count > 0:
            lines.append(f"- _{acked_count} acknowledged events hidden_")
        lines.append("")

    if attention:
        lines.append("## Attention")
        lines.append("")
        for item in attention:
            if item.startswith("  "):
                lines.append(item)
            else:
                lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines)


def write_agent_context_files(
    content: str,
    *,
    root_dir: Path,
    dry_run: bool,
    context_dir: Path | None,
    action_prefix: str,
    event_subject: str | None = None,
) -> list[str]:
    actions: list[str] = []
    claude_path, codex_path, symlink_target = context_output_paths(root_dir, context_dir)

    ensure_directory(claude_path.parent, dry_run)
    if not dry_run:
        claude_path.write_text(content, encoding="utf-8")
    actions.append(f"{action_prefix}: {repo_rel(root_dir, claude_path)}")

    ensure_directory(codex_path.parent, dry_run)
    if codex_path.is_symlink():
        current_target = os.readlink(str(codex_path))
        if current_target == symlink_target:
            actions.append(
                f"exists: {repo_rel(root_dir, codex_path)}"
                f" -> {symlink_target}"
            )
            return actions
        if not dry_run:
            codex_path.unlink()
    elif codex_path.exists():
        if not dry_run:
            codex_path.unlink()

    if not dry_run:
        codex_path.symlink_to(symlink_target)
    actions.append(
        f"symlink-context: {repo_rel(root_dir, codex_path)}"
        f" -> {symlink_target}"
    )

    if not dry_run and event_subject:
        detail = {"output_dir": repo_rel(root_dir, claude_path.parent)}
        emit_event("context.generated", event_subject, detail, root_dir=root_dir)

    return actions


def sync_live_context(
    model: dict[str, Any],
    live_state: dict[str, Any],
    root_dir: Path,
    context_dir: Path | None = None,
) -> list[str]:
    """Write live-enriched CLAUDE.md and create the AGENTS.md symlink."""
    content = generate_live_context_markdown(model, live_state, root_dir)
    return write_agent_context_files(
        content,
        root_dir=root_dir,
        dry_run=False,
        context_dir=context_dir,
        action_prefix="write-live-context",
        event_subject="live-context",
    )


def _resolve_context_paths(
    context: dict[str, Any], client_dir: Path,
) -> dict[str, Any]:
    """Resolve relative paths in a context dict to absolute paths under client_dir.

    A value is treated as a relative path if it doesn't start with ``/`` and
    contains no spaces (heuristic: avoids mangling descriptions or list items).
    """
    resolved: dict[str, Any] = {}
    for key, value in context.items():
        if isinstance(value, dict):
            resolved[key] = _resolve_context_paths(value, client_dir)
        elif isinstance(value, str) and not value.startswith("/") and " " not in value and "/" in value:
            resolved[key] = str(client_dir / value)
        elif isinstance(value, list):
            resolved[key] = [
                str(client_dir / v)
                if isinstance(v, str) and not v.startswith("/") and " " not in v and "/" in v
                else v
                for v in value
            ]
        else:
            resolved[key] = value
    return resolved


def generate_skill_context(
    model: dict[str, Any], root_dir: Path, dry_run: bool,
) -> list[str]:
    """Write a resolved context.yaml for each active client that declares context."""
    yaml_mod = require_yaml("generate skill context")
    actions: list[str] = []
    active_ids = set(model.get("active_clients") or [])
    runtime_env = model.get("env") or load_runtime_env(root_dir)

    for client in model.get("clients") or []:
        cid = client.get("id", "")
        if cid not in active_ids:
            continue
        raw_context = client.get("context")
        if not raw_context or not isinstance(raw_context, dict):
            continue

        client_dir = client_config_host_dir(root_dir, runtime_env, cid)
        client_runtime_dir = client_config_runtime_dir(runtime_env, cid)
        resolved = _resolve_context_paths(raw_context, client_dir)
        resolved["client_id"] = cid
        resolved["client_dir"] = str(client_dir)

        header = (
            f"# AUTO-GENERATED by focus. Do not edit.\n"
            f"# Source: {client_runtime_dir / 'overlay.yaml'}\n"
            f"# Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n"
        )
        body = yaml_mod.safe_dump(resolved, sort_keys=False, default_flow_style=False)
        out_path = client_dir / "context.yaml"

        if not dry_run:
            ensure_directory(client_dir, dry_run=False)
            out_path.write_text(header + body, encoding="utf-8")

        actions.append(f"write-skill-context: {repo_rel(root_dir, out_path)}")

    if actions:
        emit_event("skill-context.generated", "focus", root_dir=root_dir)
    return actions


def sync_context(
    model: dict[str, Any],
    root_dir: Path,
    dry_run: bool,
    context_dir: Path | None = None,
) -> list[str]:
    """Write the generated CLAUDE.md and create the AGENTS.md symlink."""
    content = generate_context_markdown(model)
    return write_agent_context_files(
        content,
        root_dir=root_dir,
        dry_run=dry_run,
        context_dir=context_dir,
        action_prefix="write-context",
        event_subject="context",
    )


def print_render_text(model: dict[str, Any]) -> None:
    available_clients = ", ".join(client["id"] for client in model.get("clients") or []) or "(none)"
    default_client = (model.get("selection") or {}).get("default_client") or "(none)"
    active_clients = model.get("active_clients") or []
    print(f"clients: {available_clients}")
    print(f"default client: {default_client}")
    if active_clients:
        print(f"active clients: {', '.join(active_clients)}")
    active_profiles = model.get("active_profiles") or []
    if active_profiles:
        print(f"active profiles: {', '.join(active_profiles)}")
    print(f"runtime manifest: {model['manifest_file']}")
    print(f"repos: {len(model['repos'])}")
    for repo in model["repos"]:
        print(f"  - {repo['id']}: {repo.get('kind', 'repo')} @ {repo['path']}")
    print(f"artifacts: {len(model['artifacts'])}")
    for artifact in model["artifacts"]:
        print(f"  - {artifact['id']}: {artifact.get('kind', 'artifact')} @ {artifact['path']}")
    print(f"env files: {len(model['env_files'])}")
    for env_file in model["env_files"]:
        print(f"  - {env_file['id']}: {env_file.get('kind', 'env-file')} @ {env_file['path']}")
    print(f"skills: {len(model['skills'])}")
    for skillset in model["skills"]:
        print(f"  - {skillset['id']}: {skillset.get('kind', 'packaged-skill-set')} @ {skillset['bundle_dir']}")
    print(f"tasks: {len(model['tasks'])}")
    for task in model["tasks"]:
        dependency_summary = ""
        dependency_ids = task_dependency_ids(task)
        if dependency_ids:
            dependency_summary = f" depends on {', '.join(dependency_ids)}"
        print(f"  - {task['id']}: {task.get('kind', 'task')}{dependency_summary}")
    print(f"services: {len(model['services'])}")
    for service in model["services"]:
        profiles = ", ".join(service.get("profiles") or []) or "core"
        dependency_summary = ""
        dependency_ids = service_dependency_ids(service)
        if dependency_ids:
            dependency_summary = f" depends on {', '.join(dependency_ids)}"
        bootstrap_summary = ""
        bootstrap_task_ids = service_bootstrap_task_ids(service)
        if bootstrap_task_ids:
            bootstrap_summary = f" bootstrap {', '.join(bootstrap_task_ids)}"
        print(f"  - {service['id']}: {service.get('kind', 'service')} [{profiles}]{dependency_summary}{bootstrap_summary}")
    print(f"logs: {len(model['logs'])}")
    for log_item in model["logs"]:
        print(f"  - {log_item['id']}: {log_item['path']}")
    print(f"checks: {len(model['checks'])}")
    for check in model["checks"]:
        print(f"  - {check['id']}: {check['type']}")


def detail_lines(details: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in details.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            lines.append(f"{key}: {', '.join(str(item) for item in value)}")
        else:
            lines.append(f"{key}: {value}")
    return lines


def print_doctor_text(results: list[CheckResult]) -> None:
    for result in results:
        print(f"{result.status.upper():4} {result.code}: {result.message}")
        if result.details:
            for line in detail_lines(result.details):
                print(f"     {line}")

    counts = {
        "pass": sum(1 for item in results if item.status == "pass"),
        "warn": sum(1 for item in results if item.status == "warn"),
        "fail": sum(1 for item in results if item.status == "fail"),
    }
    print()
    print(
        "summary: "
        f"{counts['pass']} passed, "
        f"{counts['warn']} warnings, "
        f"{counts['fail']} failed"
    )


def print_status_text(status_payload: dict[str, Any]) -> None:
    available_clients = ", ".join(client["id"] for client in status_payload.get("clients") or []) or "(none)"
    print(f"clients: {available_clients}")
    default_client = status_payload.get("default_client") or "(none)"
    print(f"default client: {default_client}")
    active_clients = status_payload.get("active_clients") or []
    if active_clients:
        print(f"active clients: {', '.join(active_clients)}")
    active_profiles = status_payload.get("active_profiles") or []
    if active_profiles:
        print(f"active profiles: {', '.join(active_profiles)}")
    print("repos:")
    for repo in status_payload["repos"]:
        summary = "present" if repo["present"] else "missing"
        if repo.get("git"):
            summary = (
                f"{summary}, git {repo.get('branch', '(detached)')}, "
                f"{repo.get('dirty', 0)} dirty, {repo.get('untracked', 0)} untracked"
            )
        print(f"  - {repo['id']}: {summary}")

    print("artifacts:")
    for artifact in status_payload["artifacts"]:
        state = "present" if artifact["present"] else "missing"
        print(f"  - {artifact['id']}: {state} ({artifact.get('source_kind', 'manual')})")

    print("env files:")
    for env_file in status_payload["env_files"]:
        print(f"  - {env_file['id']}: {env_file['state']} ({env_file['source_kind']})")

    print("skills:")
    for skillset in status_payload["skills"]:
        total_targets = 0
        healthy_targets = 0
        for skill_entry in skillset["skills"]:
            for target in skill_entry["targets"]:
                total_targets += 1
                if target["state"] == "ok":
                    healthy_targets += 1

        lock_summary = "invalid" if skillset.get("lock_error") else ("present" if skillset["lock_present"] else "missing")
        print(
            f"  - {skillset['id']}: lock {lock_summary}, "
            f"{len(skillset['skills'])} skills, {healthy_targets}/{total_targets} targets healthy"
        )

    print("tasks:")
    for task in status_payload["tasks"]:
        summary = task.get("state", "pending")
        dependency_summary = ""
        dependency_ids = task.get("depends_on") or []
        if dependency_ids:
            dependency_summary = f", depends on {', '.join(dependency_ids)}"
        print(f"  - {task['id']}: {summary}{dependency_summary}")

    print("services:")
    for service in status_payload["services"]:
        summary = service.get("state", "declared")
        if service.get("pid") is not None:
            summary = f"{summary} (pid {service['pid']})"
        elif service.get("managed") is False and service.get("manager_reason"):
            summary = f"{summary} ({service['manager_reason']})"
        dependency_summary = ""
        dependency_ids = service.get("depends_on") or []
        if dependency_ids:
            dependency_summary = f", depends on {', '.join(dependency_ids)}"
        bootstrap_summary = ""
        bootstrap_task_ids = service.get("bootstrap_tasks") or []
        if bootstrap_task_ids:
            bootstrap_summary = f", bootstrap {', '.join(bootstrap_task_ids)}"
        print(f"  - {service['id']}: {summary}{dependency_summary}{bootstrap_summary}")

    print("logs:")
    for log_item in status_payload["logs"]:
        if log_item["present"]:
            print(
                f"  - {log_item['id']}: {log_item['files']} files, "
                f"{human_bytes(int(log_item['bytes']))}"
            )
        else:
            print(f"  - {log_item['id']}: missing")

    print("checks:")
    for check in status_payload["checks"]:
        state = "ok" if check.get("ok") else "missing"
        print(f"  - {check['id']}: {state}")


def print_service_actions_text(payload: dict[str, Any]) -> None:
    sync_actions = payload.get("sync_actions") or []
    if sync_actions:
        print("sync:")
        for action in sync_actions:
            print(f"  - {action}")

    task_results = payload.get("tasks") or payload.get("bootstrap_tasks") or []
    if task_results:
        print("tasks:")
        for item in task_results:
            summary = item.get("result", "unknown")
            if item.get("target"):
                summary = f"{summary} ({item['target']})"
            print(f"  - {item['id']}: {summary}")

    print("services:")
    for item in payload.get("services") or []:
        summary = item.get("result", "unknown")
        if item.get("pid") is not None:
            summary = f"{summary} (pid {item['pid']})"
        if item.get("reason"):
            summary = f"{summary} ({item['reason']})"
        print(f"  - {item['id']}: {summary}")


def print_service_logs_text(payload: dict[str, Any]) -> None:
    for item in payload.get("services") or []:
        print(f"[{item['id']}] {item['log_file']}")
        if not item.get("present"):
            print("(missing)")
        elif item.get("lines"):
            for line in item["lines"]:
                print(line)
        else:
            print("(empty)")


def print_client_blueprints_text(blueprints: list[dict[str, Any]]) -> None:
    if not blueprints:
        print("No client blueprints found.")
        return

    for blueprint in blueprints:
        description = blueprint.get("description") or "No description."
        print(f"{blueprint['id']}: {description}")
        variables = blueprint.get("variables") or []
        if not variables:
            print("  vars: none")
            continue
        rendered_variables: list[str] = []
        for variable in variables:
            summary = variable["name"]
            if variable.get("required"):
                summary += " (required)"
            elif variable.get("default") is not None:
                summary += f" (default: {variable['default']})"
            rendered_variables.append(summary)
        print(f"  vars: {', '.join(rendered_variables)}")


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def run_onboard(
    *,
    root_dir: Path,
    client_id: str,
    label: str | None,
    default_cwd: str | None,
    root_path: str | None,
    blueprint_name: str | None,
    set_args: list[str],
    dry_run: bool,
    force: bool,
    wait_seconds: float,
    fmt: str,
) -> int:
    """Macro: client-init → sync → bootstrap → up → context → doctor."""
    steps: list[dict[str, Any]] = []
    is_json = fmt == "json"

    def step(name: str, status: str, detail: Any = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"step": name, "status": status}
        if detail is not None:
            entry["detail"] = detail
        steps.append(entry)
        if not is_json:
            marker = "ok" if status == "ok" else ("skip" if status == "skip" else "FAIL")
            print(f"[{marker}] {name}")
        return entry

    # -- 1. Scaffold -----------------------------------------------------------
    try:
        cid = validate_client_id(client_id)
        assignments = parse_key_value_assignments(set_args, "--set")
        scaffold_actions, blueprint_metadata = scaffold_client_overlay(
            root_dir=root_dir,
            client_id=cid,
            label=label,
            default_cwd=default_cwd,
            root_path=root_path,
            blueprint_name=blueprint_name,
            blueprint_assignments=assignments,
            dry_run=dry_run,
            force=force,
        )
        scaffold_detail: dict[str, Any] = {"actions": scaffold_actions}
        if blueprint_metadata is not None:
            scaffold_detail["blueprint"] = blueprint_metadata
        step("scaffold", "ok", scaffold_detail)
    except RuntimeError as exc:
        step("scaffold", "fail", {"error": str(exc)})
        payload: dict[str, Any] = {
            "client_id": client_id,
            "dry_run": dry_run,
            "steps": steps,
        }
        payload.update(classify_error(exc, "onboard"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # In dry-run mode, the scaffold didn't write files, so the client won't
    # exist in the runtime model.  Report what *would* happen and stop early.
    if dry_run:
        for skip_name in ("sync", "bootstrap", "up", "context", "verify"):
            step(skip_name, "skip", {"reason": "dry-run"})
        payload = {
            "client_id": cid,
            "dry_run": True,
            "steps": steps,
            "next_actions": [f"onboard {cid} --format json"],
        }
        if is_json:
            emit_json(payload)
        return EXIT_OK

    # -- 2. Sync ---------------------------------------------------------------
    try:
        model = build_runtime_model(root_dir)
        active_profiles = normalize_active_profiles([])
        active_clients = normalize_active_clients(model, [cid])
        model = filter_model(model, active_profiles, active_clients)
        sync_actions = sync_runtime(model, dry_run=False)
        step("sync", "ok", {"actions": sync_actions})
    except RuntimeError as exc:
        step("sync", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "dry_run": False, "steps": steps}
        payload.update(classify_error(exc, "onboard"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # -- 3. Bootstrap ----------------------------------------------------------
    try:
        requested_tasks = select_tasks(model, [])
        tasks = resolve_tasks_for_run(model, requested_tasks)
        if tasks:
            ensure_required_env_files_ready(select_env_files_for_tasks(model, tasks))
            task_results = run_tasks(model, tasks, dry_run=False)
            step("bootstrap", "ok", {"tasks": task_results})
        else:
            step("bootstrap", "skip", {"reason": "no tasks declared"})
    except RuntimeError as exc:
        step("bootstrap", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "dry_run": False, "steps": steps}
        payload.update(classify_error(exc, "onboard"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # -- 4. Up -----------------------------------------------------------------
    try:
        requested_services = select_services(model, [])
        services = resolve_services_for_start(model, requested_services)
        if services:
            ensure_required_env_files_ready(select_env_files_for_services(model, services))
            service_results = start_services(
                model, services, dry_run=False, wait_seconds=wait_seconds,
            )
            step("up", "ok", {"services": service_results})
        else:
            step("up", "skip", {"reason": "no services declared"})
    except RuntimeError as exc:
        step("up", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "dry_run": False, "steps": steps}
        payload.update(classify_error(exc, "onboard"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # -- 5. Context ------------------------------------------------------------
    try:
        context_actions = sync_context(model, root_dir, dry_run=False)
        step("context", "ok", {"actions": context_actions})
    except RuntimeError as exc:
        step("context", "fail", {"error": str(exc)})

    # -- 6. Doctor (verify) ----------------------------------------------------
    doctor = doctor_results(model, root_dir)
    has_fail = any(r.status == "fail" for r in doctor)
    has_warn = any(r.status == "warn" for r in doctor)
    step(
        "verify",
        "fail" if has_fail else ("warn" if has_warn else "ok"),
        {"checks": [asdict(r) for r in doctor]},
    )

    payload = {
        "client_id": cid,
        "dry_run": False,
        "steps": steps,
        "next_actions": (
            [f"doctor --client {cid} --format json", f"status --client {cid} --format json"]
            if has_fail
            else [f"status --client {cid} --format json"]
        ),
    }
    emit_event("onboard.completed", cid, {
        "steps_ok": sum(1 for s in steps if s.get("status") == "ok"),
    }, root_dir)
    if is_json:
        emit_json(payload)
    return EXIT_DRIFT if has_fail else EXIT_OK


COMPOSE_OVERRIDES_DIR_REL = Path("workspace") / ".compose-overrides"


def generate_client_compose_override(
    root_dir: Path,
    model: dict[str, Any],
    client_id: str,
) -> Path:
    """Generate a docker-compose.client-{id}.yml with per-repo bind mounts."""
    env_values = model.get("env") or {}

    # Collect bind mounts from all repos in the filtered model.
    mounts: dict[str, str] = {}  # runtime_path -> host_path
    for repo in model.get("repos", []):
        host_path = repo.get("host_path")
        runtime_path = repo.get("path")
        if not host_path or not runtime_path:
            continue
        # Skip workspace-internal paths (they're already mounted via /workspace).
        if runtime_path.startswith(env_values.get("SKILLBOX_WORKSPACE_ROOT", "/workspace")):
            continue
        mounts[runtime_path] = host_path

    # Always include the swimmers repo so the binary install path works.
    swimmers_repo = env_values.get("SKILLBOX_SWIMMERS_REPO", "")
    if swimmers_repo and swimmers_repo not in mounts:
        from lib.runtime_model import runtime_path_to_host_path as _rp2hp
        swimmers_host = str(_rp2hp(root_dir, env_values, swimmers_repo))
        if Path(swimmers_host).exists():
            mounts[swimmers_repo] = swimmers_host

    # Remove child paths when a parent is already mounted (avoids redundant mounts).
    sorted_paths = sorted(mounts.keys())
    pruned: dict[str, str] = {}
    for rpath in sorted_paths:
        if any(rpath != parent and rpath.startswith(parent + "/") for parent in pruned):
            continue
        pruned[rpath] = mounts[rpath]

    # Build volume entries.
    volume_entries = [f"{host}:{container}" for container, host in sorted(pruned.items())]

    # Build compose override document.
    lines = [f"# Auto-generated by skillbox for client '{client_id}'. Do not edit."]
    lines.append("services:")
    for svc in ("workspace", "api", "web"):
        lines.append(f"  {svc}:")
        lines.append("    volumes:")
        for entry in volume_entries:
            lines.append(f"      - {entry}")

    out_dir = root_dir / COMPOSE_OVERRIDES_DIR_REL
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"docker-compose.client-{client_id}.yml"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def run_focus(
    *,
    root_dir: Path,
    client_id: str,
    service_filter: list[str],
    resume: bool,
    wait_seconds: float,
    fmt: str,
    context_dir: Path | None = None,
) -> int:
    """Focus macro: sync → bootstrap → up → collect live state → generate enriched context."""
    steps: list[dict[str, Any]] = []
    is_json = fmt == "json"

    def step(name: str, status: str, detail: Any = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"step": name, "status": status}
        if detail is not None:
            entry["detail"] = detail
        steps.append(entry)
        if not is_json:
            marker = "ok" if status == "ok" else ("skip" if status == "skip" else "FAIL")
            print(f"[{marker}] {name}")
        return entry

    focus_path = root_dir / FOCUS_STATE_REL

    # --- Resume path ----------------------------------------------------------
    if resume:
        if not focus_path.is_file():
            err = {"error": "No .focus.json found. Run focus with a client_id first."}
            if is_json:
                emit_json(err)
            else:
                print(err["error"], file=sys.stderr)
            return EXIT_ERROR
        try:
            saved = json.loads(focus_path.read_text(encoding="utf-8"))
            client_id = saved.get("client_id", client_id)
        except (json.JSONDecodeError, OSError) as exc:
            err = {"error": f"Failed to read .focus.json: {exc}"}
            if is_json:
                emit_json(err)
            else:
                print(err["error"], file=sys.stderr)
            return EXIT_ERROR

    # --- Validate client exists -----------------------------------------------
    try:
        cid = validate_client_id(client_id)
    except RuntimeError as exc:
        if is_json:
            emit_json(classify_error(exc, "focus"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    _, overlay_path, overlay_runtime_path = client_overlay_location(root_dir, cid)
    if not overlay_path.is_file():
        err_msg = (
            f"Client '{cid}' has no overlay at {overlay_runtime_path}. "
            f"Use 'onboard {cid}' to scaffold it first."
        )
        if is_json:
            emit_json(classify_error(RuntimeError(err_msg), "focus"))
        else:
            print(err_msg, file=sys.stderr)
        return EXIT_ERROR

    # --- Build model ----------------------------------------------------------
    try:
        model = build_runtime_model(root_dir)
        active_profiles = normalize_active_profiles([])
        active_clients = normalize_active_clients(model, [cid])
        model = filter_model(model, active_profiles, active_clients)
    except RuntimeError as exc:
        payload: dict[str, Any] = {"client_id": cid, "steps": steps}
        payload.update(classify_error(exc, "focus"))
        if is_json:
            emit_json(payload)
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    # --- 0. Compose override ---------------------------------------------------
    try:
        override_path = generate_client_compose_override(root_dir, model, cid)
        step("compose-override", "ok", {"path": str(override_path)})
    except Exception as exc:
        step("compose-override", "fail", {"error": str(exc)})

    # --- 1. Sync --------------------------------------------------------------
    try:
        sync_actions = sync_runtime(model, dry_run=False)
        step("sync", "ok", {"actions": sync_actions})
    except RuntimeError as exc:
        step("sync", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "steps": steps}
        payload.update(classify_error(exc, "focus"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # --- 2. Bootstrap ---------------------------------------------------------
    try:
        requested_tasks = select_tasks(model, [])
        tasks = resolve_tasks_for_run(model, requested_tasks)
        if tasks:
            ensure_required_env_files_ready(select_env_files_for_tasks(model, tasks))
            task_results = run_tasks(model, tasks, dry_run=False)
            step("bootstrap", "ok", {"tasks": task_results})
        else:
            step("bootstrap", "skip", {"reason": "no tasks declared"})
    except RuntimeError as exc:
        step("bootstrap", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "steps": steps}
        payload.update(classify_error(exc, "focus"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # --- 3. Up ----------------------------------------------------------------
    try:
        requested_services = select_services(model, service_filter)
        services = resolve_services_for_start(model, requested_services)
        if services:
            ensure_required_env_files_ready(
                select_env_files_for_tasks(
                    model, resolve_tasks_for_services(model, services),
                ) + select_env_files_for_services(model, services)
            )
            service_results = start_services(
                model, services, dry_run=False, wait_seconds=wait_seconds,
            )
            step("up", "ok", {"services": service_results})
        else:
            step("up", "skip", {"reason": "no services in scope"})
    except RuntimeError as exc:
        step("up", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "steps": steps}
        payload.update(classify_error(exc, "focus"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # --- 4. Collect live state ------------------------------------------------
    try:
        live = collect_live_state(model)
        step("collect", "ok")
    except Exception as exc:
        step("collect", "fail", {"error": str(exc)})
        live = {"collected_at": time.time(), "repos": [], "services": [], "checks": [], "logs": []}

    # --- 5. Generate skill context.yaml ---------------------------------------
    try:
        skill_ctx_actions = generate_skill_context(model, root_dir, dry_run=False)
        if skill_ctx_actions:
            step("skill-context", "ok", {"actions": skill_ctx_actions})
        else:
            step("skill-context", "skip", {"reason": "no client context declared"})
    except Exception as exc:
        step("skill-context", "fail", {"error": str(exc)})

    # --- 6. Generate enriched context -----------------------------------------
    try:
        context_actions = sync_live_context(model, live, root_dir, context_dir=context_dir)
        step("context", "ok", {"actions": context_actions})
    except RuntimeError as exc:
        step("context", "fail", {"error": str(exc)})

    # --- 7. Persist focus state -----------------------------------------------
    _, ctx_yaml_path, ctx_runtime_path = client_context_location(root_dir, cid)
    focus_data: dict[str, Any] = {
        "version": 1,
        "client_id": cid,
        "active_profiles": sorted(model.get("active_profiles") or []),
        "focused_at": time.time(),
        "service_filter": service_filter or None,
    }
    if ctx_yaml_path.is_file():
        focus_data["skill_context_path"] = str(ctx_runtime_path)
    try:
        focus_path.write_text(
            json.dumps(focus_data, indent=2), encoding="utf-8",
        )
        step("persist", "ok")
    except OSError as exc:
        step("persist", "fail", {"error": str(exc)})

    # --- Build summary --------------------------------------------------------
    has_fail = any(s.get("status") == "fail" for s in steps)

    # Compact counts for text output
    repos_present = sum(1 for r in live.get("repos", []) if r.get("present"))
    repos_dirty = sum(1 for r in live.get("repos", []) if r.get("dirty", 0) > 0)
    svcs_running = sum(1 for s in live.get("services", []) if s.get("healthy"))
    svcs_down = sum(
        1 for s in live.get("services", [])
        if s.get("state") in ("stopped", "not-running", "declared")
    )
    checks_ok = sum(1 for c in live.get("checks", []) if c.get("ok"))
    checks_total = len(live.get("checks", []))
    error_count = sum(
        len(lg.get("recent_errors", []))
        for lg in live.get("logs", [])
    )

    payload = {
        "client_id": cid,
        "steps": steps,
        "live_state": live,
        "summary": {
            "repos_present": repos_present,
            "repos_dirty": repos_dirty,
            "services_running": svcs_running,
            "services_down": svcs_down,
            "checks_passing": checks_ok,
            "checks_total": checks_total,
            "recent_errors": error_count,
        },
        "next_actions": next_actions_for_focus(cid, has_fail),
    }

    emit_event("focus.activated", cid, payload.get("summary", {}), root_dir)

    if is_json:
        emit_json(payload)
    else:
        print()
        print(f"  Client:    {cid}")
        print(f"  Repos:     {repos_present} present, {repos_dirty} dirty")
        print(f"  Services:  {svcs_running} running, {svcs_down} down")
        print(f"  Checks:    {checks_ok}/{checks_total} passing")
        if error_count:
            print(f"  Errors:    {error_count} recent error(s) in logs")

    return EXIT_DRIFT if has_fail else EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the internal skillbox runtime graph.")
    parser.add_argument(
        "--root-dir",
        default=None,
        help="Override the repo root for testing or embedding.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_profile_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--profile",
            action="append",
            default=[],
            help="Activate a runtime profile. Can be repeated. Selecting any profile also includes `core`.",
        )

    def add_client_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--client",
            action="append",
            default=[],
            help="Activate a runtime client overlay. Can be repeated.",
        )

    def add_service_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--service",
            action="append",
            default=[],
            help="Limit the command to one or more declared service ids. Can be repeated.",
        )

    def add_task_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--task",
            action="append",
            default=[],
            help="Limit the command to one or more declared task ids. Can be repeated.",
        )

    def add_context_dir_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--context-dir",
            default=None,
            help=(
                "Write CLAUDE.md and AGENTS.md into this directory instead of the mounted "
                "home/.claude and home/.codex roots. Path is resolved relative to the repo root."
            ),
        )

    render_parser = subparsers.add_parser("render", help="Print the resolved runtime graph.")
    render_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(render_parser)
    add_client_arg(render_parser)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Create managed runtime directories, repos, artifacts, and installed skill state.",
    )
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(sync_parser)
    add_client_arg(sync_parser)
    add_context_dir_arg(sync_parser)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate runtime graph, filesystem readiness, and installed skill integrity.",
    )
    doctor_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(doctor_parser)
    add_client_arg(doctor_parser)

    status_parser = subparsers.add_parser(
        "status",
        help="Summarize repo, artifact, skill, service, log, and check state.",
    )
    status_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(status_parser)
    add_client_arg(status_parser)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Sync runtime state and run one-shot bootstrap tasks for the active scope.",
    )
    bootstrap_parser.add_argument("--dry-run", action="store_true")
    bootstrap_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(bootstrap_parser)
    add_client_arg(bootstrap_parser)
    add_task_arg(bootstrap_parser)

    up_parser = subparsers.add_parser(
        "up",
        help="Sync runtime state and start manageable services for the active scope.",
    )
    up_parser.add_argument("--dry-run", action="store_true")
    up_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS)
    up_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(up_parser)
    add_client_arg(up_parser)
    add_service_arg(up_parser)

    down_parser = subparsers.add_parser(
        "down",
        help="Stop manageable services for the active scope.",
    )
    down_parser.add_argument("--dry-run", action="store_true")
    down_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_STOP_WAIT_SECONDS)
    down_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(down_parser)
    add_client_arg(down_parser)
    add_service_arg(down_parser)

    restart_parser = subparsers.add_parser(
        "restart",
        help="Restart manageable services for the active scope.",
    )
    restart_parser.add_argument("--dry-run", action="store_true")
    restart_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS)
    restart_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(restart_parser)
    add_client_arg(restart_parser)
    add_service_arg(restart_parser)

    logs_parser = subparsers.add_parser(
        "logs",
        help="Show recent logs for declared services in the active scope.",
    )
    logs_parser.add_argument("--lines", type=int, default=DEFAULT_LOG_TAIL_LINES)
    logs_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(logs_parser)
    add_client_arg(logs_parser)
    add_service_arg(logs_parser)

    context_parser = subparsers.add_parser(
        "context",
        help="Generate CLAUDE.md and AGENTS.md from the resolved runtime graph.",
    )
    context_parser.add_argument("--dry-run", action="store_true")
    context_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(context_parser)
    add_client_arg(context_parser)
    add_context_dir_arg(context_parser)

    client_init_parser = subparsers.add_parser(
        "client-init",
        help="Scaffold a new workspace client overlay and companion skill directories.",
    )
    client_init_parser.add_argument(
        "client_id",
        nargs="?",
        help="Lowercase client slug, for example `acme-studio`.",
    )
    client_init_parser.add_argument("--label", default=None, help="Human-friendly label for the client.")
    client_init_parser.add_argument(
        "--root-path",
        default=None,
        help="Runtime path for the client root. Defaults to ${SKILLBOX_MONOSERVER_ROOT}/<client-id>.",
    )
    client_init_parser.add_argument(
        "--default-cwd",
        default=None,
        help="Runtime default cwd for the client. Defaults to the client root path.",
    )
    client_init_parser.add_argument(
        "--blueprint",
        default=None,
        help="Apply a reusable client blueprint from workspace/client-blueprints/ or an explicit YAML path.",
    )
    client_init_parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Set a blueprint variable using KEY=VALUE. Can be repeated.",
    )
    client_init_parser.add_argument(
        "--list-blueprints",
        action="store_true",
        help="List discoverable client blueprints and their variables.",
    )
    client_init_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files.")
    client_init_parser.add_argument("--dry-run", action="store_true")
    client_init_parser.add_argument("--format", choices=("text", "json"), default="text")

    client_project_parser = subparsers.add_parser(
        "client-project",
        help="Compile a single-client runtime projection bundle with sanitized metadata.",
    )
    client_project_parser.add_argument(
        "client_id",
        help="Existing client slug to project (for example `personal`).",
    )
    client_project_parser.add_argument(
        "--output-dir",
        default=None,
        help="Projection output directory. Defaults to builds/clients/<client-id>.",
    )
    client_project_parser.add_argument("--force", action="store_true", help="Replace an existing projection bundle.")
    client_project_parser.add_argument("--dry-run", action="store_true")
    client_project_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(client_project_parser)

    onboard_parser = subparsers.add_parser(
        "onboard",
        help="Macro: scaffold a client, sync, bootstrap, start services, generate context, and verify.",
    )
    onboard_parser.add_argument(
        "client_id",
        help="Lowercase client slug, for example `acme-studio`.",
    )
    onboard_parser.add_argument("--label", default=None, help="Human-friendly label for the client.")
    onboard_parser.add_argument(
        "--root-path",
        default=None,
        help="Runtime path for the client root. Defaults to ${SKILLBOX_MONOSERVER_ROOT}/<client-id>.",
    )
    onboard_parser.add_argument(
        "--default-cwd",
        default=None,
        help="Runtime default cwd for the client. Defaults to the client root path.",
    )
    onboard_parser.add_argument(
        "--blueprint",
        default=None,
        help="Apply a reusable client blueprint from workspace/client-blueprints/ or an explicit YAML path.",
    )
    onboard_parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Set a blueprint variable using KEY=VALUE. Can be repeated.",
    )
    onboard_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files.")
    onboard_parser.add_argument("--dry-run", action="store_true")
    onboard_parser.add_argument(
        "--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS,
    )
    onboard_parser.add_argument("--format", choices=("text", "json"), default="text")

    focus_parser = subparsers.add_parser(
        "focus",
        help="Activate a client workspace with live state and enriched agent context.",
    )
    focus_parser.add_argument(
        "client_id",
        nargs="?",
        default="",
        help="Existing client slug to focus on (e.g. 'personal').",
    )
    focus_parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-activate the last focus session from .focus.json.",
    )
    focus_parser.add_argument(
        "--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS,
    )
    focus_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_service_arg(focus_parser)
    add_context_dir_arg(focus_parser)

    ack_parser = subparsers.add_parser(
        "ack",
        help="Acknowledge journal events to remove them from active context.",
    )
    ack_parser.add_argument(
        "--type", default=None, dest="event_type",
        help="Ack events of this type (e.g. pulse.service_restarted).",
    )
    ack_parser.add_argument(
        "--subject", default=None,
        help="Ack events with this subject (e.g. a service or client ID).",
    )
    ack_parser.add_argument(
        "--ts", type=float, default=None,
        help="Ack a specific event by its exact timestamp.",
    )
    ack_parser.add_argument(
        "--all", action="store_true", dest="ack_all",
        help="Ack all unacked events.",
    )
    ack_parser.add_argument("--reason", default="", help="Why this was acknowledged.")
    ack_parser.add_argument(
        "--list", action="store_true", dest="list_acks",
        help="List current acks instead of creating new ones.",
    )
    ack_parser.add_argument(
        "--prune", action="store_true",
        help="Remove expired acks from the store.",
    )
    ack_parser.add_argument("--format", choices=("text", "json"), default="text")

    args = parser.parse_args()
    root_dir = resolve_root_dir(args.root_dir)

    if args.command == "client-init":
        try:
            if args.list_blueprints:
                blueprints = list_client_blueprints(root_dir)
                if args.format == "json":
                    emit_json({"blueprints": blueprints})
                else:
                    print_client_blueprints_text(blueprints)
                return EXIT_OK

            if not args.client_id:
                raise RuntimeError("client-init requires <client_id> unless --list-blueprints is used.")

            assignments = parse_key_value_assignments(args.set, "--set")
            actions, blueprint_metadata = scaffold_client_overlay(
                root_dir=root_dir,
                client_id=args.client_id,
                label=args.label,
                default_cwd=args.default_cwd,
                root_path=args.root_path,
                blueprint_name=args.blueprint,
                blueprint_assignments=assignments,
                dry_run=args.dry_run,
                force=args.force,
            )
        except RuntimeError as exc:
            if args.format == "json":
                emit_json(classify_error(exc, "client-init"))
            else:
                print(str(exc), file=sys.stderr)
            return EXIT_ERROR

        cid = validate_client_id(args.client_id)
        payload: dict[str, Any] = {
            "client_id": cid,
            "dry_run": args.dry_run,
            "force": args.force,
            "actions": actions,
            "next_actions": next_actions_for_client_init(cid),
        }
        if blueprint_metadata is not None:
            payload["blueprint"] = blueprint_metadata
        if args.format == "json":
            emit_json(payload)
        else:
            if blueprint_metadata is not None:
                print(f"blueprint: {blueprint_metadata['id']}")
            print("\n".join(actions))
        return EXIT_OK

    if args.command == "onboard":
        return run_onboard(
            root_dir=root_dir,
            client_id=args.client_id,
            label=args.label,
            default_cwd=args.default_cwd,
            root_path=args.root_path,
            blueprint_name=args.blueprint,
            set_args=args.set,
            dry_run=args.dry_run,
            force=args.force,
            wait_seconds=max(0.0, float(args.wait_seconds)),
            fmt=args.format,
        )

    if args.command == "client-project":
        try:
            payload = project_client_bundle(
                root_dir=root_dir,
                client_id=args.client_id,
                profiles=args.profile,
                output_dir_arg=args.output_dir,
                dry_run=args.dry_run,
                force=args.force,
            )
        except RuntimeError as exc:
            if args.format == "json":
                emit_json(classify_error(exc, "client-project"))
            else:
                print(str(exc), file=sys.stderr)
            return EXIT_ERROR

        if args.format == "json":
            emit_json(payload)
        else:
            print(f"client: {payload['client_id']}")
            print(f"output_dir: {payload['output_dir']}")
            print(f"files: {payload['file_count']}")
            print(f"payload_tree_sha256: {payload['payload_tree_sha256']}")
            print()
            print("\n".join(payload["actions"]))
        return EXIT_OK

    if args.command == "focus":
        cid = args.client_id or ""
        if not cid and not args.resume:
            print("focus requires a client_id or --resume.", file=sys.stderr)
            return EXIT_ERROR
        return run_focus(
            root_dir=root_dir,
            client_id=cid,
            service_filter=getattr(args, "service", []),
            resume=args.resume,
            wait_seconds=max(0.0, float(args.wait_seconds)),
            fmt=args.format,
            context_dir=resolve_context_dir(root_dir, getattr(args, "context_dir", None)),
        )

    if args.command == "ack":
        if args.list_acks:
            ack_data = read_acks(root_dir)
            if args.format == "json":
                emit_json({"acks": ack_data, "count": len(ack_data)})
            else:
                if not ack_data:
                    print("No active acks.")
                else:
                    for key, entry in ack_data.items():
                        age = time.time() - entry.get("at", 0)
                        age_str = f"{int(age / 3600)}h ago" if age >= 3600 else f"{int(age / 60)}m ago"
                        reason = entry.get("reason", "")
                        reason_str = f" — {reason}" if reason else ""
                        print(f"  ts={key} acked {age_str}{reason_str}")
            return EXIT_OK

        if args.prune:
            pruned = prune_expired_acks(root_dir)
            if args.format == "json":
                emit_json({"pruned": pruned})
            else:
                print(f"Pruned {pruned} expired acks.")
            return EXIT_OK

        if not args.event_type and not args.subject and args.ts is None and not args.ack_all:
            print("ack requires --type, --subject, --ts, or --all.", file=sys.stderr)
            return EXIT_ERROR

        acked_items = ack_events(
            root_dir,
            event_type=args.event_type,
            subject=args.subject,
            ts=args.ts,
            ack_all=args.ack_all,
            reason=args.reason,
        )
        if args.format == "json":
            emit_json({"acked": acked_items, "count": len(acked_items), "next_actions": ["status --format json"]})
        else:
            if acked_items:
                for item in acked_items:
                    print(f"  acked: {item['type']} {item['subject']}")
                print(f"\n{len(acked_items)} events acknowledged.")
            else:
                print("No matching events to ack.")
        return EXIT_OK

    model = build_runtime_model(root_dir)
    active_profiles = normalize_active_profiles(getattr(args, "profile", []))
    active_clients = normalize_active_clients(model, getattr(args, "client", []))
    model = filter_model(model, active_profiles, active_clients)

    try:
        if args.command == "render":
            if args.format == "json":
                emit_json(model)
            else:
                print_render_text(model)
            return EXIT_OK

        if args.command == "sync":
            actions = sync_runtime(model, dry_run=args.dry_run)
            actions.extend(
                sync_context(
                    model,
                    root_dir,
                    dry_run=args.dry_run,
                    context_dir=resolve_context_dir(root_dir, getattr(args, "context_dir", None)),
                )
            )
            if args.format == "json":
                emit_json({"actions": actions, "dry_run": args.dry_run, "next_actions": next_actions_for_sync()})
            else:
                print("\n".join(actions))
            return EXIT_OK

        if args.command == "context":
            actions = sync_context(
                model,
                root_dir,
                dry_run=args.dry_run,
                context_dir=resolve_context_dir(root_dir, getattr(args, "context_dir", None)),
            )
            if args.format == "json":
                emit_json({"actions": actions, "dry_run": args.dry_run, "next_actions": next_actions_for_context()})
            else:
                print("\n".join(actions))
            return EXIT_OK

        if args.command == "doctor":
            results = doctor_results(model, root_dir)
            has_fail = any(result.status == "fail" for result in results)
            has_warn = any(result.status == "warn" for result in results)
            if args.format == "json":
                emit_json({
                    "checks": [asdict(result) for result in results],
                    "next_actions": next_actions_for_doctor(results),
                })
            else:
                print_doctor_text(results)
            if has_fail:
                return EXIT_DRIFT
            return EXIT_OK

        if args.command == "status":
            status_payload = runtime_status(model)
            if args.format == "json":
                status_payload["next_actions"] = next_actions_for_status(status_payload)
                emit_json(status_payload)
            else:
                print_status_text(status_payload)
            return EXIT_OK

        if args.command == "bootstrap":
            sync_actions = sync_runtime(model, dry_run=args.dry_run)
            requested_tasks = select_tasks(model, getattr(args, "task", []))
            tasks = resolve_tasks_for_run(model, requested_tasks)
            if not args.dry_run:
                ensure_required_env_files_ready(select_env_files_for_tasks(model, tasks))
            task_results = run_tasks(
                model,
                tasks,
                dry_run=args.dry_run,
            )
            payload: dict[str, Any] = {
                "dry_run": args.dry_run,
                "sync_actions": sync_actions,
                "tasks": task_results,
                "next_actions": next_actions_for_bootstrap(task_results),
            }
            if args.format == "json":
                emit_json(payload)
            else:
                print_service_actions_text(payload)
            return EXIT_OK

        requested_services = select_services(model, getattr(args, "service", []))

        if args.command == "up":
            sync_actions = sync_runtime(model, dry_run=args.dry_run)
            services = resolve_services_for_start(model, requested_services)
            bootstrap_tasks = resolve_tasks_for_services(model, services)
            if not args.dry_run:
                ensure_required_env_files_ready(
                    select_env_files_for_tasks(model, bootstrap_tasks) + select_env_files_for_services(model, services)
                )
            task_results = run_tasks(
                model,
                bootstrap_tasks,
                dry_run=args.dry_run,
            )
            service_results = start_services(
                model,
                services,
                dry_run=args.dry_run,
                wait_seconds=max(0.0, float(args.wait_seconds)),
            )
            payload = {
                "dry_run": args.dry_run,
                "sync_actions": sync_actions,
                "bootstrap_tasks": task_results,
                "services": service_results,
                "next_actions": next_actions_for_up(service_results),
            }
            if args.format == "json":
                emit_json(payload)
            else:
                print_service_actions_text(payload)
            return EXIT_OK

        if args.command == "down":
            services = resolve_services_for_stop(model, requested_services)
            service_results = stop_services(
                model,
                services,
                dry_run=args.dry_run,
                wait_seconds=max(0.0, float(args.wait_seconds)),
            )
            payload = {
                "dry_run": args.dry_run,
                "services": service_results,
                "next_actions": next_actions_for_down(),
            }
            if args.format == "json":
                emit_json(payload)
            else:
                print_service_actions_text(payload)
            return EXIT_OK

        if args.command == "restart":
            stop_targets = resolve_services_for_stop(model, requested_services)
            start_targets = resolve_services_for_start(model, stop_targets)
            bootstrap_tasks = resolve_tasks_for_services(model, start_targets)
            stop_results = stop_services(
                model,
                stop_targets,
                dry_run=args.dry_run,
                wait_seconds=max(0.0, float(args.wait_seconds)),
            )
            sync_actions = sync_runtime(model, dry_run=args.dry_run)
            if not args.dry_run:
                ensure_required_env_files_ready(
                    select_env_files_for_tasks(model, bootstrap_tasks) + select_env_files_for_services(model, start_targets)
                )
            task_results = run_tasks(
                model,
                bootstrap_tasks,
                dry_run=args.dry_run,
            )
            start_results = start_services(
                model,
                start_targets,
                dry_run=args.dry_run,
                wait_seconds=max(0.0, float(args.wait_seconds)),
            )
            payload = {
                "dry_run": args.dry_run,
                "stop_services": stop_results,
                "sync_actions": sync_actions,
                "bootstrap_tasks": task_results,
                "start_services": start_results,
                "next_actions": next_actions_for_up(start_results),
            }
            if args.format == "json":
                emit_json(payload)
            else:
                print("stop:")
                print_service_actions_text({"services": stop_results})
                print()
                print_service_actions_text({"sync_actions": sync_actions, "tasks": task_results, "services": start_results})
            return EXIT_OK

        logs_payload: dict[str, Any] = {
            "services": collect_service_logs(
                model,
                requested_services,
                line_count=max(0, int(args.lines)),
            ),
            "next_actions": ["status --format json"],
        }
        if args.format == "json":
            emit_json(logs_payload)
        else:
            print_service_logs_text(logs_payload)
        return EXIT_OK
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, args.command))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
