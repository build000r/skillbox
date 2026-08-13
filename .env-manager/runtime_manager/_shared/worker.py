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
from typing import Any, Callable, Mapping

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
from .textutil import (
    validate_client_id,
)

from .errors import (
    structured_error,
)

from .events import (
    log_runtime_event,
)

from .fs import (
    _append_jsonl,
    atomic_write_text,
    load_json_file,
    write_json_file,
)

WORKER_RUN_SCHEMA_VERSION = 1

WORKER_TASK_CLASSES = (
    "analysis",
    "interpretation",
    "recommendation",
    "drafting",
    "research",
    "ops_execution",
)

WORKER_RUNTIME_IDS = ("hermes",)

WORKER_WRITE_SCOPES = ("read_only", "propose_only", "repo_patch")

WORKER_MEMORY_SCOPES = ("none", "repo", "client")

WORKER_RUN_STATES = (
    "queued",
    "resolving",
    "blocked",
    "launching",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "review_pending",
)

WORKER_ERROR_CODES = (
    "WORKER_CONTEXT_UNRESOLVED",
    "WORKER_RUNTIME_UNKNOWN",
    "WORKER_POLICY_BLOCKED",
    "WORKER_LAUNCH_FAILED",
    "WORKER_RUN_NOT_FOUND",
    "WORKER_RESULT_NOT_READY",
    "WORKER_LEARNING_REVIEW_REQUIRED",
    "WORKER_WRITEBACK_REJECTED",
    "WORKER_RUN_EXISTS",
    "PLACEMENT_NOT_LOCAL",
)

WORKER_CONTEXT_UNRESOLVED = "WORKER_CONTEXT_UNRESOLVED"

WORKER_RUNTIME_UNKNOWN = "WORKER_RUNTIME_UNKNOWN"

WORKER_POLICY_BLOCKED = "WORKER_POLICY_BLOCKED"

WORKER_LAUNCH_FAILED = "WORKER_LAUNCH_FAILED"

WORKER_RUN_NOT_FOUND = "WORKER_RUN_NOT_FOUND"

WORKER_RESULT_NOT_READY = "WORKER_RESULT_NOT_READY"

WORKER_LEARNING_REVIEW_REQUIRED = "WORKER_LEARNING_REVIEW_REQUIRED"

WORKER_WRITEBACK_REJECTED = "WORKER_WRITEBACK_REJECTED"

WORKER_RUN_EXISTS = "WORKER_RUN_EXISTS"

PLACEMENT_NOT_LOCAL = "PLACEMENT_NOT_LOCAL"

REMOTE_RESULT_COMPLETED = "completed"

REMOTE_RESULT_UNAVAILABLE = "result_unavailable"

REMOTE_RESULT_COMMAND_FAILED = "command_failed"

# Mirrored from scripts/box.py DEFAULT_SSH_OPTS. Do not import scripts/lib.
WORKER_SSH_OPTS = (
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "BatchMode=yes",
)

WORKER_DEFAULT_SSH_USER = "skillbox"

WORKER_DEFAULT_RUNTIME_ID = "hermes"

WORKER_DEFAULT_WRITE_SCOPE = "propose_only"

WORKER_DEFAULT_MEMORY_SCOPE = "repo"

WORKER_DEFAULT_ARTIFACT_POLICY = "summary_and_files"

WORKER_DEFAULT_LAUNCH_TIMEOUT_SECONDS = 300.0

WORKER_LAUNCH_SETTLE_SECONDS = 0.2

_WORKER_ACTIVE_PROCESSES: dict[int, subprocess.Popen[Any]] = {}

WORKER_RUN_ID_PATTERN = re.compile(r"^wr_[0-9]{8}_[0-9]{6}_[a-f0-9]{6}$")

WORKER_TERMINAL_STATES = ("succeeded", "failed", "cancelled", "review_pending")

WORKER_BROKER_MCP_SURFACES = (
    "skillbox_worker_submit",
    "skillbox_worker_status",
    "skillbox_worker_artifacts",
    "skillbox_worker_promote_learning",
)

class WorkerRuntimeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.details = details or {}

@dataclass(frozen=True)
class WorkerTaskSpec:
    run_id: str
    task_class: str
    instruction: str
    requested_runtime: str
    artifact_policy: str
    write_scope: str
    memory_scope: str
    harness_session_ref: str
    inputs: list[dict[str, Any]]

@dataclass(frozen=True)
class WorkerRun:
    run_id: str
    runtime: str
    state: str
    submitted_at: float
    started_at: float | None
    finished_at: float | None
    blocked_reason: str | None

def worker_runtime_error_payload(exc: WorkerRuntimeError) -> dict[str, Any]:
    next_actions = None
    if isinstance(exc.details, dict):
        raw_actions = exc.details.get("next_actions")
        if isinstance(raw_actions, list):
            next_actions = [str(item) for item in raw_actions]
    payload = structured_error(
        str(exc),
        error_type=exc.code,
        recoverable=exc.recoverable,
        next_actions=next_actions,
    )
    if exc.details:
        payload["error"]["details"] = exc.details
    return payload


def classify_remote_result(exit_code: int | None, transport_failure: bool) -> str:
    """Classify a remote dispatch outcome without mutating worker state.

    Truth table (opslib.classify_ssh_failure semantics, no scripts/lib import):
    exit 0 and not transport_failure → completed;
    transport_failure after dispatch (rc 255 / TIMEOUT) → result_unavailable;
    nonzero remote rc → command_failed.
    """
    if transport_failure:
        return REMOTE_RESULT_UNAVAILABLE
    try:
        code = int(exit_code) if exit_code is not None else -1
    except (TypeError, ValueError):
        code = -1
    if code == 0:
        return REMOTE_RESULT_COMPLETED
    return REMOTE_RESULT_COMMAND_FAILED


def build_remote_submit_command(
    decision: Mapping[str, Any] | None,
    task_argv: list[str] | tuple[str, ...] | None,
) -> list[str]:
    """Compose an ssh argv from a placement decision. Never executes it."""
    payload = decision if isinstance(decision, Mapping) else {}
    target = str(payload.get("box_id") or payload.get("machine_id") or "").strip()
    remote_parts = [str(part) for part in (task_argv or ())]
    command = [
        "ssh",
        *WORKER_SSH_OPTS,
        "--",
        f"{WORKER_DEFAULT_SSH_USER}@{target}",
    ]
    if remote_parts:
        command.append(" ".join(shlex.quote(part) for part in remote_parts))
    return command

def _worker_now() -> float:
    return time.time()

def _generate_worker_run_id() -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    return f"wr_{stamp}_{os.urandom(3).hex()}"

def _normalize_worker_run_id(run_id: str) -> str:
    normalized = str(run_id or "").strip()
    if not normalized:
        normalized = _generate_worker_run_id()
    if not WORKER_RUN_ID_PATTERN.fullmatch(normalized):
        raise WorkerRuntimeError(
            WORKER_RUN_NOT_FOUND,
            "worker run_id must match wr_YYYYMMDD_HHMMSS_xxxxxx",
            details={"run_id": normalized},
        )
    return normalized

def _normalize_worker_choice(
    raw_value: str | None,
    *,
    field_name: str,
    allowed: tuple[str, ...],
    default: str | None = None,
    error_code: str = WORKER_POLICY_BLOCKED,
) -> str:
    value = str(raw_value or "").strip()
    if not value and default is not None:
        value = default
    if value not in allowed:
        raise WorkerRuntimeError(
            error_code,
            f"Unsupported worker {field_name} {value!r}. Use one of: {', '.join(allowed)}.",
            details={"field": field_name, "allowed": list(allowed), "value": value},
        )
    return value

def _normalize_worker_runtime(raw_runtime: str | None) -> str:
    return _normalize_worker_choice(
        raw_runtime,
        field_name="runtime",
        allowed=WORKER_RUNTIME_IDS,
        default=WORKER_DEFAULT_RUNTIME_ID,
        error_code=WORKER_RUNTIME_UNKNOWN,
    )

def _normalize_worker_client_id(raw_client_id: str | None) -> str:
    client_id = str(raw_client_id or "").strip()
    if not client_id:
        return ""
    return validate_client_id(client_id)

def worker_runs_root(root_dir: Path) -> Path:
    try:
        model = build_runtime_model(root_dir)
    except RuntimeError:
        model = {}
    storage = model.get("storage") or {}
    state_root = str(storage.get("state_root") or "").strip()
    if state_root:
        return Path(state_root).expanduser() / "worker-runs"
    return root_dir / ".skillbox-state" / "worker-runs"


def _normalize_worker_needs(
    needs: list[str] | dict[str, Any] | None,
    need_trust: str | None,
    allow_unverified: bool,
) -> dict[str, Any] | None:
    caps: list[str] = []
    trust = str(need_trust or "").strip() or None
    unverified = bool(allow_unverified)
    given = False
    if isinstance(needs, dict):
        given = True
        for item in needs.get("caps") or ():
            token = str(item).strip()
            if token and token not in caps:
                caps.append(token)
        raw_trust = needs.get("trust")
        if raw_trust not in (None, ""):
            trust = str(raw_trust).strip() or trust
        if "allow_unverified" in needs:
            unverified = bool(needs.get("allow_unverified"))
    elif needs:
        given = True
        for item in needs:
            token = str(item).strip()
            if not token:
                continue
            if "=" in token and ":" not in token.split("=", 1)[0]:
                key, value = token.split("=", 1)
                token = f"{key.strip()}:{value.strip()}"
            if token and token not in caps:
                caps.append(token)
    if trust or unverified:
        given = True
    if not given:
        return None
    return {
        "caps": caps,
        "trust": trust,
        "allow_unverified": unverified,
        "allow_provision": False,
    }


def _load_worker_boxes(root_dir: Path) -> list[Any]:
    path = root_dir / "workspace" / "boxes.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        boxes = payload.get("boxes")
        return boxes if isinstance(boxes, list) else []
    return payload if isinstance(payload, list) else []


def _load_worker_box_profiles(root_dir: Path) -> list[dict[str, Any]]:
    profiles_dir = root_dir / "workspace" / "box-profiles"
    if yaml is None or not profiles_dir.is_dir():
        return []
    profiles: list[dict[str, Any]] = []
    for path in sorted(profiles_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, Exception):
            continue
        if not isinstance(raw, dict):
            continue
        profiles.append(
            {
                "id": str(raw.get("id") or path.stem),
                "image": raw.get("image"),
                "size": raw.get("size"),
            }
        )
    return profiles


def _decide_worker_placement(
    root_dir: Path,
    needs: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    from runtime_manager import machines as machines_mod
    from runtime_manager.placement import decide, gather_observations

    machines_path = root_dir / machines_mod.MACHINES_FILE_NAME
    try:
        if machines_path.is_file():
            config = machines_mod.load_machines_config(machines_path)
        else:
            config = machines_mod.load_machines_config(root_dir=root_dir)
    except machines_mod.MachinesConfigError as exc:
        raise WorkerRuntimeError(
            WORKER_POLICY_BLOCKED,
            f"Cannot decide placement: {exc}",
            details={"field": "needs"},
        ) from exc
    boxes = _load_worker_boxes(root_dir)
    profiles = _load_worker_box_profiles(root_dir)
    current_id = machines_mod.detect_machine_id(config)
    observations = gather_observations(current_id, boxes)
    return decide(needs, config, boxes, observations, profiles, current_id), current_id


def _placement_not_local_next_actions(decision: Mapping[str, Any]) -> list[str]:
    actions: list[str] = []
    for item in decision.get("next_actions") or ():
        text = str(item).strip()
        if text and text not in actions:
            actions.append(text)
    target = str(decision.get("box_id") or decision.get("machine_id") or "").strip()
    if target:
        ssh_line = f"python3 scripts/box.py ssh {target}"
        if ssh_line not in actions:
            actions.append(ssh_line)
    if not actions:
        actions.append("python3 scripts/box.py place --format json")
    return actions


def _raise_placement_not_local(decision: Mapping[str, Any]) -> None:
    next_actions = _placement_not_local_next_actions(decision)
    raise WorkerRuntimeError(
        PLACEMENT_NOT_LOCAL,
        "Placement selected a machine that is not the current host.",
        recoverable=True,
        details={
            "decision": dict(decision),
            "next_actions": next_actions,
        },
    )


def _worker_idempotency_path(root_dir: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return worker_runs_root(root_dir) / "idempotency" / digest


def _claim_worker_idempotency_key(root_dir: Path, key: str, run_id: str) -> str | None:
    """O_EXCL claim. Returns the existing run_id on duplicate, else None."""
    path = _worker_idempotency_path(root_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"key": key, "run_id": run_id}, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags, 0o644)
    except FileExistsError:
        try:
            existing = load_json_file(path)
        except RuntimeError:
            existing = {}
        return str(existing.get("run_id") or "").strip()
    try:
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return None


def _ensure_new_worker_run_path(run_path: Path, run_id: str) -> None:
    if run_path.exists():
        raise WorkerRuntimeError(
            WORKER_RUN_EXISTS,
            f"Worker run already exists: {run_id}",
            recoverable=True,
            details={"run_id": run_id, "path": str(run_path)},
        )
    run_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(run_path), flags, 0o644)
    except FileExistsError:
        raise WorkerRuntimeError(
            WORKER_RUN_EXISTS,
            f"Worker run already exists: {run_id}",
            recoverable=True,
            details={"run_id": run_id, "path": str(run_path)},
        ) from None
    os.close(descriptor)

def worker_run_paths(root_dir: Path, run_id: str) -> dict[str, Path]:
    normalized_run_id = _normalize_worker_run_id(run_id)
    runs_root = worker_runs_root(root_dir)
    run_dir = runs_root / normalized_run_id
    return {
        "runs_root": runs_root,
        "run_dir": run_dir,
        "run_path": run_dir / "run.json",
        "events_path": run_dir / "events.jsonl",
    }

def _iter_worker_run_paths(root_dir: Path) -> list[Path]:
    runs_root = worker_runs_root(root_dir)
    if not runs_root.is_dir():
        return []
    return sorted(runs_root.glob("*/run.json"))

def read_worker_run(root_dir: Path, run_id: str) -> dict[str, Any]:
    normalized_run_id = _normalize_worker_run_id(run_id)
    paths = worker_run_paths(root_dir, normalized_run_id)
    if not paths["run_path"].is_file():
        raise WorkerRuntimeError(
            WORKER_RUN_NOT_FOUND,
            f"Worker run not found: {normalized_run_id}",
            recoverable=True,
            details={"run_id": normalized_run_id},
        )
    payload = load_json_file(paths["run_path"])
    if str(payload.get("run_id") or "").strip() != normalized_run_id:
        raise WorkerRuntimeError(
            WORKER_RUN_NOT_FOUND,
            f"Worker run metadata does not match requested run_id: {normalized_run_id}",
            recoverable=False,
            details={"run_id": normalized_run_id},
        )
    return payload

def _worker_client_index(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    clients: dict[str, dict[str, Any]] = {}
    for raw_client in model.get("clients") or []:
        if not isinstance(raw_client, dict):
            continue
        client_id = str(raw_client.get("id") or "").strip()
        if client_id:
            clients[client_id] = raw_client
    return clients

def _worker_safe_path(raw_path: str) -> Path | None:
    value = str(raw_path or "").strip()
    if not value or "${" in value:
        return None
    try:
        return Path(value).expanduser().resolve()
    except OSError:
        return Path(value).expanduser().absolute()

def _worker_client_match_paths(client: dict[str, Any]) -> list[Path]:
    raw_paths: list[str] = []
    raw_paths.extend(
        str(client.get(key) or "")
        for key in ("default_cwd_host_path", "default_cwd")
    )
    context = client.get("context") or {}
    if isinstance(context, dict):
        raw_paths.extend(str(path) for path in context.get("cwd_match") or [])
        deploy = context.get("deploy") or {}
        if isinstance(deploy, dict):
            raw_paths.append(str(deploy.get("repo_root") or ""))
    paths = [_worker_safe_path(path) for path in raw_paths]
    return [path for path in paths if path is not None]

def _worker_path_contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents

def _worker_client_id_from_cwd(clients: dict[str, dict[str, Any]], cwd: str) -> tuple[str, str | None]:
    cwd_path = _worker_safe_path(cwd)
    if cwd_path is None:
        return "", WORKER_CONTEXT_UNRESOLVED
    matches = [
        client_id
        for client_id, client in clients.items()
        if any(_worker_path_contains(path, cwd_path) for path in _worker_client_match_paths(client))
    ]
    if len(matches) == 1:
        return matches[0], None
    return "", WORKER_CONTEXT_UNRESOLVED

def _worker_repo_id(
    model: dict[str, Any],
    repo_hint: str,
    repo_root: str,
    fallback_repo_id: str = "",
) -> str:
    if repo_hint:
        return repo_hint
    root_path = _worker_safe_path(repo_root)
    containing_repo_id = ""
    for repo_item in model.get("repos") or []:
        if not isinstance(repo_item, dict) or root_path is None:
            continue
        repo_path = _worker_safe_path(str(repo_item.get("host_path") or repo_item.get("path") or ""))
        if repo_path == root_path:
            return str(repo_item.get("id") or "")
        if repo_path and _worker_path_contains(repo_path, root_path):
            containing_repo_id = containing_repo_id or str(repo_item.get("id") or "")
    return fallback_repo_id or containing_repo_id

def _worker_resolved_context(
    model: dict[str, Any],
    client: dict[str, Any],
    *,
    cwd: str,
    repo_hint: str,
    write_scope: str,
    memory_scope: str,
) -> dict[str, Any]:
    context = client.get("context") or {}
    deploy = context.get("deploy") if isinstance(context, dict) else {}
    deploy = deploy if isinstance(deploy, dict) else {}
    repo_root = str(deploy.get("repo_root") or client.get("default_cwd_host_path") or client.get("default_cwd") or cwd)
    effective_cwd = str(cwd or client.get("default_cwd_host_path") or client.get("default_cwd") or repo_root)
    return {
        "client_id": client["id"],
        "repo_id": _worker_repo_id(
            model,
            repo_hint,
            repo_root,
            str(deploy.get("repo_id") or deploy.get("repo_slug") or ""),
        ),
        "repo_root": repo_root,
        "effective_cwd": effective_cwd,
        "profiles": list(model.get("active_profiles") or []),
        "allowed_tools": [],
        "mcp_surfaces": list(WORKER_BROKER_MCP_SURFACES),
        "write_scope": write_scope,
        "memory_scope": memory_scope,
    }

def _resolve_worker_context(
    root_dir: Path,
    *,
    client_id: str,
    cwd: str,
    repo_hint: str,
    write_scope: str,
    memory_scope: str,
) -> tuple[str, str, str | None, dict[str, Any] | None, str]:
    if not client_id and not cwd:
        return "blocked", "unresolved", WORKER_CONTEXT_UNRESOLVED, None, client_id
    try:
        model = build_runtime_model(root_dir)
    except RuntimeError:
        return "queued", "resolving", None, None, client_id
    clients = _worker_client_index(model)
    if not clients:
        return "queued", "resolving", None, None, client_id
    resolved_client_id = client_id
    if not resolved_client_id:
        resolved_client_id, match_error = _worker_client_id_from_cwd(clients, cwd)
        if match_error:
            return "blocked", "unresolved", match_error, None, ""
    client = clients.get(resolved_client_id)
    if client is None:
        return "blocked", "unresolved", WORKER_CONTEXT_UNRESOLVED, None, resolved_client_id
    resolved_context = _worker_resolved_context(
        model,
        client,
        cwd=cwd,
        repo_hint=repo_hint,
        write_scope=write_scope,
        memory_scope=memory_scope,
    )
    return "queued", "resolved", None, resolved_context, resolved_client_id

def _ensure_worker_write_scope_allowed(write_scope: str) -> None:
    if write_scope == "repo_patch":
        raise WorkerRuntimeError(
            WORKER_POLICY_BLOCKED,
            "Worker write scope 'repo_patch' requires an explicit launch policy.",
            details={"field": "write_scope", "value": write_scope},
        )

def _worker_hermes_command() -> list[str] | None:
    raw_command = str(
        os.environ.get("SKILLBOX_WORKER_HERMES_COMMAND")
        or os.environ.get("SKILLBOX_HERMES_COMMAND")
        or ""
    ).strip()
    if raw_command:
        return shlex.split(raw_command)
    raw_bin = str(
        os.environ.get("SKILLBOX_WORKER_HERMES_BIN")
        or os.environ.get("SKILLBOX_HERMES_BIN")
        or ""
    ).strip()
    if raw_bin:
        return [raw_bin]
    hermes_bin = shutil.which("hermes")
    return [hermes_bin] if hermes_bin else None

def _worker_effective_cwd(root_dir: Path, payload: dict[str, Any]) -> Path:
    resolved_context = payload.get("resolved_context") or {}
    effective_cwd = _worker_safe_path(str(resolved_context.get("effective_cwd") or ""))
    return effective_cwd if effective_cwd and effective_cwd.is_dir() else root_dir

def _worker_launch_paths(paths: dict[str, Path]) -> dict[str, Path]:
    run_dir = paths["run_dir"]
    return {
        "task_path": run_dir / "task.json",
        "result_path": run_dir / "result.json",
        "summary_path": run_dir / "summary.md",
        "stdout_path": run_dir / "stdout.log",
        "stderr_path": run_dir / "stderr.log",
    }

def _worker_launch_env(
    root_dir: Path,
    payload: dict[str, Any],
    launch_paths: dict[str, Path],
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "SKILLBOX_ROOT_DIR": str(root_dir),
            "SKILLBOX_WORKER_RUN_ID": payload["run_id"],
            "SKILLBOX_WORKER_TASK_PATH": str(launch_paths["task_path"]),
            "SKILLBOX_WORKER_RESULT_PATH": str(launch_paths["result_path"]),
        }
    )
    return env

def _worker_launch_timeout_seconds() -> float:
    raw_timeout = str(os.environ.get("SKILLBOX_WORKER_LAUNCH_TIMEOUT_SECONDS") or "").strip()
    if not raw_timeout:
        return WORKER_DEFAULT_LAUNCH_TIMEOUT_SECONDS
    try:
        return max(1.0, float(raw_timeout))
    except ValueError:
        return WORKER_DEFAULT_LAUNCH_TIMEOUT_SECONDS

def _worker_result_payload(payload: dict[str, Any], *, state: str, summary: str) -> dict[str, Any]:
    return {
        "run_id": payload["run_id"],
        "state": state,
        "summary": summary,
        "findings": [],
        "actions_taken": [],
        "next_action": "",
    }

def _worker_summary_artifact(
    payload: dict[str, Any],
    launch_paths: dict[str, Path],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    summary = str(result.get("summary") or "").strip()
    if not summary:
        return None
    atomic_write_text(launch_paths["summary_path"], summary + "\n")
    return {
        "artifact_id": f"art_{payload['run_id']}_summary",
        "run_id": payload["run_id"],
        "kind": "summary",
        "path": str(launch_paths["summary_path"]),
        "mime_type": "text/markdown",
        "summary": summary[:240],
    }

def _worker_apply_terminal_result(
    payload: dict[str, Any],
    *,
    state: str,
    finished_at: float,
    result: dict[str, Any],
    artifacts: list[dict[str, Any]] | None = None,
    learning_proposals: list[dict[str, Any]] | None = None,
    blocked_reason: str | None = None,
) -> None:
    payload["state"] = state
    payload["result"] = result
    payload["artifacts"] = artifacts or []
    payload["learning_proposals"] = learning_proposals or []
    payload["review_required"] = _worker_learning_review_required(payload)
    payload["run"]["state"] = state
    payload["run"]["finished_at"] = finished_at
    payload["run"]["blocked_reason"] = blocked_reason

def _worker_mark_launching(payload: dict[str, Any], started_at: float, command: list[str]) -> None:
    payload["state"] = "launching"
    payload["run"]["state"] = "launching"
    payload["run"]["started_at"] = started_at
    payload["launch"] = {
        "attempted": True,
        "runtime": payload["runtime"],
        "started_at": started_at,
        "blocked_reason": None,
        "command": command,
    }

def _worker_mark_running(payload: dict[str, Any], *, pid: int) -> None:
    payload["state"] = "running"
    payload["run"]["state"] = "running"
    payload["run"]["blocked_reason"] = None
    payload["launch"]["pid"] = pid

def _worker_mark_launch_failed(
    payload: dict[str, Any],
    *,
    finished_at: float,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    result = _worker_result_payload(payload, state="failed", summary=message)
    result["error"] = {
        "type": WORKER_LAUNCH_FAILED,
        "message": message,
        "details": details or {},
    }
    _worker_apply_terminal_result(
        payload,
        state="failed",
        finished_at=finished_at,
        result=result,
        blocked_reason=WORKER_LAUNCH_FAILED,
    )
    payload["launch"]["finished_at"] = finished_at
    payload["launch"]["blocked_reason"] = WORKER_LAUNCH_FAILED
    payload["launch"]["error"] = result["error"]

def _worker_loaded_result(
    payload: dict[str, Any],
    launch_paths: dict[str, Path],
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    raw_result = load_json_file(launch_paths["result_path"]) if launch_paths["result_path"].is_file() else {}
    if not isinstance(raw_result, dict):
        raw_result = {}
    state = str(raw_result.get("state") or "succeeded").strip()
    if state not in WORKER_TERMINAL_STATES:
        state = "succeeded"
    result = _worker_result_payload(
        payload,
        state=state,
        summary=str(raw_result.get("summary") or "Worker completed."),
    )
    for key in ("findings", "actions_taken", "next_action"):
        if key in raw_result:
            result[key] = raw_result[key]
    artifacts = [item for item in raw_result.get("artifacts") or [] if isinstance(item, dict)]
    if not artifacts:
        summary_artifact = _worker_summary_artifact(payload, launch_paths, result)
        artifacts = [summary_artifact] if summary_artifact else []
    learning_proposals = [item for item in raw_result.get("learning_proposals") or [] if isinstance(item, dict)]
    return state, result, artifacts, learning_proposals

def _worker_apply_terminal_from_launch_paths(
    root_dir: Path,
    paths: dict[str, Path],
    payload: dict[str, Any],
    launch_paths: dict[str, Path],
    *,
    finished_at: float | None = None,
    returncode: int | None = None,
) -> dict[str, Any]:
    resolved_finished_at = finished_at if finished_at is not None else _worker_now()
    state, result, artifacts, learning_proposals = _worker_loaded_result(payload, launch_paths)
    _worker_apply_terminal_result(
        payload,
        state=state,
        finished_at=resolved_finished_at,
        result=result,
        artifacts=artifacts,
        learning_proposals=learning_proposals,
    )
    payload["launch"]["finished_at"] = resolved_finished_at
    if returncode is not None:
        payload["launch"]["returncode"] = returncode
    _persist_worker_payload(root_dir, paths, payload, f"worker.{state}")
    return payload

def _worker_launch_pid(payload: dict[str, Any]) -> int:
    launch = payload.get("launch") or {}
    raw_pid = launch.get("pid")
    try:
        return int(raw_pid)
    except (TypeError, ValueError):
        return 0

def _worker_reap_active_process(pid: int) -> int | None:
    process = _WORKER_ACTIVE_PROCESSES.get(pid)
    if not process:
        return None
    returncode = process.poll()
    if returncode is not None:
        _WORKER_ACTIVE_PROCESSES.pop(pid, None)
    return returncode

def _worker_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if _worker_reap_active_process(pid) is not None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True

def _worker_stderr_tail(launch_paths: dict[str, Path], limit: int = 4000) -> str:
    stderr_path = launch_paths.get("stderr_path")
    if not stderr_path or not stderr_path.is_file():
        return ""
    try:
        return stderr_path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""

def _reconcile_worker_payload(root_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    state = _worker_payload_state(payload)
    if state not in {"launching", "running"}:
        return payload

    paths = worker_run_paths(root_dir, payload["run_id"])
    launch_paths = _worker_launch_paths(paths)
    if launch_paths["result_path"].is_file():
        _worker_reap_active_process(_worker_launch_pid(payload))
        return _worker_apply_terminal_from_launch_paths(root_dir, paths, payload, launch_paths)

    launch = payload.get("launch") or {}
    pid = _worker_launch_pid(payload)
    if _worker_pid_running(pid):
        return payload

    details: dict[str, Any] = {
        "runtime": payload.get("runtime"),
        "pid": pid or None,
        "stderr": _worker_stderr_tail(launch_paths),
    }
    command = launch.get("command")
    if command:
        details["command"] = command
    _worker_mark_launch_failed(
        payload,
        finished_at=_worker_now(),
        message="Hermes worker runtime exited before writing a result.",
        details=details,
    )
    _persist_worker_payload(root_dir, paths, payload, "worker.launch_failed")
    return payload

def _persist_worker_payload(
    root_dir: Path,
    paths: dict[str, Path],
    payload: dict[str, Any],
    event_type: str,
) -> None:
    write_json_file(paths["run_path"], payload)
    _append_jsonl(
        paths["events_path"],
        {
            "ts": _worker_now(),
            "type": event_type,
            "run_id": payload["run_id"],
            "state": payload["state"],
            "blocked_reason": payload["run"].get("blocked_reason"),
        },
    )
    log_runtime_event(
        event_type,
        payload["run_id"],
        {"state": payload["state"], "client_id": payload.get("client_id")},
        root_dir,
    )

def _launch_worker_if_ready(root_dir: Path, paths: dict[str, Path], payload: dict[str, Any]) -> dict[str, Any]:
    if payload["state"] != "queued" or payload.get("context_state") != "resolved":
        return payload
    launch_paths = _worker_launch_paths(paths)
    write_json_file(
        launch_paths["task_path"],
        {
            "task_spec": payload["task_spec"],
            "resolved_context": payload["resolved_context"],
        },
    )
    command = _worker_hermes_command()
    if not command:
        _worker_mark_launching(payload, _worker_now(), ["hermes"])
        _worker_mark_launch_failed(
            payload,
            finished_at=_worker_now(),
            message="Hermes worker runtime is not installed or configured.",
            details={"runtime": payload["runtime"]},
        )
        _persist_worker_payload(root_dir, paths, payload, "worker.launch_failed")
        return payload

    _worker_mark_launching(payload, _worker_now(), command)
    _persist_worker_payload(root_dir, paths, payload, "worker.launching")
    try:
        with (
            launch_paths["stdout_path"].open("a", encoding="utf-8") as stdout_file,
            launch_paths["stderr_path"].open("a", encoding="utf-8") as stderr_file,
        ):
            process = subprocess.Popen(
                command,
                cwd=_worker_effective_cwd(root_dir, payload),
                env=_worker_launch_env(root_dir, payload, launch_paths),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
            )
    except OSError as exc:
        _worker_mark_launch_failed(
            payload,
            finished_at=_worker_now(),
            message=f"Hermes worker runtime failed to start: {exc}",
            details={"runtime": payload["runtime"], "command": command},
        )
        _persist_worker_payload(root_dir, paths, payload, "worker.launch_failed")
        return payload
    try:
        returncode = process.wait(timeout=WORKER_LAUNCH_SETTLE_SECONDS)
    except subprocess.TimeoutExpired:
        _WORKER_ACTIVE_PROCESSES[process.pid] = process
        _worker_mark_running(payload, pid=process.pid)
        _persist_worker_payload(root_dir, paths, payload, "worker.running")
        return payload

    if returncode != 0:
        _worker_mark_launch_failed(
            payload,
            finished_at=_worker_now(),
            message="Hermes worker runtime exited before completing the run.",
            details={
                "runtime": payload["runtime"],
                "command": command,
                "returncode": returncode,
                "stderr": _worker_stderr_tail(launch_paths),
            },
        )
        _persist_worker_payload(root_dir, paths, payload, "worker.launch_failed")
        return payload

    return _worker_apply_terminal_from_launch_paths(
        root_dir,
        paths,
        payload,
        launch_paths,
        returncode=returncode,
    )

def create_worker_run(
    root_dir: Path,
    *,
    task_class: str,
    instruction: str,
    client_id: str | None = None,
    cwd: str | None = None,
    repo_hint: str | None = None,
    runtime: str | None = None,
    artifact_policy: str | None = None,
    write_scope: str | None = None,
    memory_scope: str | None = None,
    harness_session_ref: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    needs: list[str] | dict[str, Any] | None = None,
    need_trust: str | None = None,
    allow_unverified: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    normalized_task_class = _normalize_worker_choice(
        task_class,
        field_name="task_class",
        allowed=WORKER_TASK_CLASSES,
        error_code=WORKER_POLICY_BLOCKED,
    )
    normalized_instruction = str(instruction or "").strip()
    if not normalized_instruction:
        raise WorkerRuntimeError(
            WORKER_POLICY_BLOCKED,
            "Worker instruction is required.",
            details={"field": "instruction"},
        )
    normalized_runtime = _normalize_worker_runtime(runtime)
    normalized_write_scope = _normalize_worker_choice(
        write_scope,
        field_name="write_scope",
        allowed=WORKER_WRITE_SCOPES,
        default=WORKER_DEFAULT_WRITE_SCOPE,
        error_code=WORKER_POLICY_BLOCKED,
    )
    _ensure_worker_write_scope_allowed(normalized_write_scope)
    normalized_memory_scope = _normalize_worker_choice(
        memory_scope,
        field_name="memory_scope",
        allowed=WORKER_MEMORY_SCOPES,
        default=WORKER_DEFAULT_MEMORY_SCOPE,
        error_code=WORKER_POLICY_BLOCKED,
    )
    placement_needs = _normalize_worker_needs(needs, need_trust, allow_unverified)
    placement_block: dict[str, Any] | None = None
    if placement_needs is not None:
        placement_block, current_id = _decide_worker_placement(root_dir, placement_needs)
        selected = str(placement_block.get("machine_id") or "").strip()
        current = str(current_id or "").strip()
        if (
            placement_block.get("decision") != "selected"
            or not selected
            or selected != current
        ):
            _raise_placement_not_local(placement_block)
    normalized_run_id = _normalize_worker_run_id(str(run_id or ""))
    normalized_idempotency_key = str(idempotency_key or "").strip()
    if normalized_idempotency_key:
        existing_run_id = _claim_worker_idempotency_key(
            root_dir, normalized_idempotency_key, normalized_run_id
        )
        if existing_run_id:
            existing = _reconcile_worker_payload(
                root_dir, read_worker_run(root_dir, existing_run_id)
            )
            existing["duplicate"] = True
            return existing
    normalized_client_id = _normalize_worker_client_id(client_id)
    normalized_cwd = str(cwd or "").strip()
    normalized_repo_hint = str(repo_hint or "").strip()
    submitted_at = _worker_now()
    state, context_state, blocked_reason, resolved_context, normalized_client_id = _resolve_worker_context(
        root_dir,
        client_id=normalized_client_id,
        cwd=normalized_cwd,
        repo_hint=normalized_repo_hint,
        write_scope=normalized_write_scope,
        memory_scope=normalized_memory_scope,
    )

    task_spec = WorkerTaskSpec(
        run_id=normalized_run_id,
        task_class=normalized_task_class,
        instruction=normalized_instruction,
        requested_runtime=normalized_runtime,
        artifact_policy=str(artifact_policy or WORKER_DEFAULT_ARTIFACT_POLICY).strip(),
        write_scope=normalized_write_scope,
        memory_scope=normalized_memory_scope,
        harness_session_ref=str(harness_session_ref or "").strip(),
        inputs=list(inputs or []),
    )
    run = WorkerRun(
        run_id=normalized_run_id,
        runtime=normalized_runtime,
        state=state,
        submitted_at=submitted_at,
        started_at=None,
        finished_at=None,
        blocked_reason=blocked_reason,
    )
    paths = worker_run_paths(root_dir, normalized_run_id)
    _ensure_new_worker_run_path(paths["run_path"], normalized_run_id)
    launch = {
        "attempted": False,
        "blocked_reason": blocked_reason,
    }
    payload: dict[str, Any] = {
        "version": WORKER_RUN_SCHEMA_VERSION,
        "run_id": normalized_run_id,
        "runtime": normalized_runtime,
        "state": state,
        "task_class": normalized_task_class,
        "context_state": context_state,
        "review_required": False,
        "client_id": normalized_client_id,
        "cwd": normalized_cwd,
        "repo_hint": normalized_repo_hint,
        "task_spec": asdict(task_spec),
        "run": asdict(run),
        "resolved_context": resolved_context,
        "result": None,
        "artifacts": [],
        "learning_proposals": [],
        "launch": launch,
        "paths": {
            "run": str(paths["run_path"]),
            "events": str(paths["events_path"]),
        },
        "next_actions": [f"worker-status {normalized_run_id} --format json"],
    }
    if placement_block is not None:
        payload["placement"] = placement_block
        payload["machine_id"] = placement_block.get("machine_id")

    write_json_file(paths["run_path"], payload)
    _append_jsonl(
        paths["events_path"],
        {
            "ts": submitted_at,
            "type": "worker.blocked" if state == "blocked" else "worker.queued",
            "run_id": normalized_run_id,
            "state": state,
            "blocked_reason": blocked_reason,
        },
    )
    log_runtime_event(
        "worker.blocked" if state == "blocked" else "worker.queued",
        normalized_run_id,
        {"state": state, "client_id": normalized_client_id, "cwd": normalized_cwd},
        root_dir,
    )
    return _launch_worker_if_ready(root_dir, paths, payload)

def worker_status_payload(root_dir: Path, run_id: str) -> dict[str, Any]:
    payload = _reconcile_worker_payload(root_dir, read_worker_run(root_dir, run_id))
    run = payload.get("run") or {}
    state = _worker_payload_state(payload)
    status: dict[str, Any] = {
        "run_id": payload["run_id"],
        "runtime": payload.get("runtime") or run.get("runtime"),
        "state": state,
        "summary": (payload.get("result") or {}).get("summary") if payload.get("result") else None,
        "blocked_reason": run.get("blocked_reason"),
        "artifacts_ready": state in WORKER_TERMINAL_STATES,
        "learning_review_required": _worker_learning_review_required(payload),
        "run": run,
    }
    if payload.get("placement"):
        status["placement"] = payload["placement"]
    if payload.get("machine_id"):
        status["machine_id"] = payload["machine_id"]
    return status

def worker_artifacts_payload(root_dir: Path, run_id: str) -> dict[str, Any]:
    payload = _reconcile_worker_payload(root_dir, read_worker_run(root_dir, run_id))
    state = _worker_payload_state(payload)
    if state not in WORKER_TERMINAL_STATES:
        raise WorkerRuntimeError(
            WORKER_RESULT_NOT_READY,
            f"Worker result is not ready for run {payload['run_id']}.",
            details={"run_id": payload["run_id"], "state": state},
        )
    return {
        "run_id": payload["run_id"],
        "state": state,
        "result": payload.get("result"),
        "artifacts": payload.get("artifacts") or [],
        "learning_proposals": payload.get("learning_proposals") or [],
    }

def _worker_payload_state(payload: dict[str, Any]) -> str:
    run = payload.get("run") or {}
    return str(payload.get("state") or run.get("state") or "").strip()

def _worker_learning_review_required(payload: dict[str, Any]) -> bool:
    proposals = [item for item in payload.get("learning_proposals") or [] if isinstance(item, dict)]
    return any(
        bool(proposal.get("requires_review"))
        and str(proposal.get("status") or "") == "pending_review"
        for proposal in proposals
    )

def _find_worker_learning_proposal(root_dir: Path, proposal_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    normalized_proposal_id = str(proposal_id or "").strip()
    if not normalized_proposal_id:
        raise WorkerRuntimeError(
            WORKER_WRITEBACK_REJECTED,
            "proposal_id is required.",
            details={"field": "proposal_id"},
        )
    for run_path in _iter_worker_run_paths(root_dir):
        payload = load_json_file(run_path)
        for proposal in payload.get("learning_proposals") or []:
            if isinstance(proposal, dict) and str(proposal.get("proposal_id") or "") == normalized_proposal_id:
                return run_path, payload, proposal
    raise WorkerRuntimeError(
        WORKER_WRITEBACK_REJECTED,
        f"Learning proposal not found: {normalized_proposal_id}",
        details={"proposal_id": normalized_proposal_id},
    )

def _worker_promotion_response(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": proposal["proposal_id"],
        "status": str(proposal.get("status") or ""),
        "target_kind": proposal.get("target_kind"),
        "target_location": proposal.get("target_location"),
        "run_id": proposal.get("run_id"),
    }

def _worker_proposal_status(proposal: dict[str, Any]) -> str:
    return str(proposal.get("status") or "").strip()

def _reject_pending_worker_proposal(proposal: dict[str, Any], status: str) -> None:
    if bool(proposal.get("requires_review")) or status in {"pending", "pending_review"}:
        raise WorkerRuntimeError(
            WORKER_LEARNING_REVIEW_REQUIRED,
            f"Learning proposal {proposal['proposal_id']} requires review before promotion.",
            details={"proposal_id": proposal["proposal_id"], "status": status},
        )

def _normalize_worker_promotion_mode(promotion_mode: str) -> str:
    normalized_mode = str(promotion_mode or "promote").strip() or "promote"
    if normalized_mode != "promote":
        raise WorkerRuntimeError(
            WORKER_WRITEBACK_REJECTED,
            f"Unsupported learning promotion mode: {normalized_mode}",
            details={"promotion_mode": normalized_mode},
        )
    return normalized_mode

def _worker_proposal_target_matches(
    proposal: dict[str, Any],
    target_kind: str,
    target_location: str,
) -> bool:
    return (
        target_kind == str(proposal.get("target_kind") or "").strip()
        and target_location == str(proposal.get("target_location") or "").strip()
    )

def _ensure_worker_proposal_promotable(
    proposal: dict[str, Any],
    *,
    target_kind: str,
    target_location: str,
) -> None:
    status = _worker_proposal_status(proposal)
    _reject_pending_worker_proposal(proposal, status)
    if status == "approved" and _worker_proposal_target_matches(proposal, target_kind, target_location):
        return
    raise WorkerRuntimeError(
        WORKER_WRITEBACK_REJECTED,
        f"Learning proposal {proposal['proposal_id']} cannot be promoted to the requested target.",
        details={
            "proposal_id": proposal["proposal_id"],
            "status": status,
            "target_kind": target_kind,
            "target_location": target_location,
        },
    )

def _mark_worker_proposal_promoted(
    proposal: dict[str, Any],
    *,
    approved_by: str,
    promotion_mode: str,
) -> None:
    proposal["status"] = "promoted"
    proposal["promoted_at"] = _worker_now()
    proposal["approved_by"] = str(approved_by or "").strip()
    proposal["promotion_mode"] = promotion_mode

def promote_worker_learning(
    root_dir: Path,
    *,
    proposal_id: str,
    approved_by: str,
    target_kind: str,
    target_location: str,
    promotion_mode: str = "promote",
) -> dict[str, Any]:
    run_path, payload, proposal = _find_worker_learning_proposal(root_dir, proposal_id)
    requested_target_kind = str(target_kind or "").strip()
    requested_target_location = str(target_location or "").strip()
    normalized_mode = _normalize_worker_promotion_mode(promotion_mode)
    if _worker_proposal_status(proposal) == "promoted":
        return _worker_promotion_response(proposal)
    _ensure_worker_proposal_promotable(
        proposal,
        target_kind=requested_target_kind,
        target_location=requested_target_location,
    )
    _mark_worker_proposal_promoted(
        proposal,
        approved_by=approved_by,
        promotion_mode=normalized_mode,
    )
    write_json_file(run_path, payload)
    _append_jsonl(
        run_path.parent / "events.jsonl",
        {
            "ts": proposal["promoted_at"],
            "type": "worker.learning_promoted",
            "run_id": payload.get("run_id"),
            "proposal_id": proposal["proposal_id"],
            "target_kind": requested_target_kind,
            "target_location": requested_target_location,
        },
    )
    log_runtime_event(
        "worker.learning_promoted",
        str(proposal["proposal_id"]),
        {"run_id": payload.get("run_id"), "target_kind": requested_target_kind},
        root_dir,
    )
    return _worker_promotion_response(proposal)
