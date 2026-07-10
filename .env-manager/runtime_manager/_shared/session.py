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
from .textutil import (
    validate_client_id,
)

from .next_actions import (
    next_actions_for_session_end,
    next_actions_for_session_event,
    next_actions_for_session_resume,
    next_actions_for_session_start,
    next_actions_for_session_status,
)

from .events import (
    RUNTIME_LOG_REL,
    log_runtime_event,
    merge_runtime_event_detail,
)

from .fs import (
    _append_jsonl,
    client_overlay_location,
    ensure_directory,
    load_json_file,
    repo_rel,
    write_json_file,
    write_text_file,
)

SESSION_SCHEMA_VERSION = 1

SESSION_ACTIVE_STATUS = "active"

SESSION_TERMINAL_STATUSES = {"completed", "failed", "abandoned"}

DEFAULT_EVENT_FEED_LIMIT = 50

DEFAULT_EVENT_FEED_POLL_INTERVAL_SECONDS = 0.25

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

def _durable_session_event_paths(
    root_dir: Path,
    *,
    client_id: str | None,
    target_session: str,
) -> list[Path]:
    paths: list[Path] = []
    for sessions_root in _session_event_roots(root_dir, client_id=client_id):
        if not sessions_root.is_dir():
            continue
        pattern = f"{target_session}/events.jsonl" if target_session else "*/events.jsonl"
        paths.extend(sessions_root.glob(pattern))
    return paths

def _load_durable_session_meta(meta_path: Path) -> dict[str, Any]:
    if not meta_path.is_file():
        return {}
    try:
        return load_json_file(meta_path)
    except RuntimeError:
        return {}

def _iter_jsonl_dicts(path: Path) -> list[tuple[int, dict[str, Any]]]:
    items: list[tuple[int, dict[str, Any]]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append((line_number, item))
    return items

def _durable_session_event_payload(
    root_dir: Path,
    events_path: Path,
    line_number: int,
    item: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    fallback_client = str(meta.get("client_id") or events_path.parent.parent.parent.name).strip()
    fallback_session = str(meta.get("session_id") or events_path.parent.name).strip()
    detail = item.get("detail")
    if not isinstance(detail, dict):
        detail = {}
    ts = float(item.get("ts") or 0.0)
    return {
        "source": "session",
        "line_number": line_number,
        "ts": ts,
        "time": datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": str(item.get("type") or "").strip(),
        "subject": f"{fallback_client}:{fallback_session}",
        "client_id": str(item.get("client_id") or fallback_client).strip() or None,
        "session_id": str(item.get("session_id") or fallback_session).strip() or None,
        "message": str(detail.get("message") or "").strip() or None,
        "detail": detail,
        "paths": {
            "events": repo_rel(root_dir, events_path),
            "meta": repo_rel(root_dir, events_path.parent / "meta.json"),
        },
    }

def read_durable_session_events(
    root_dir: Path,
    *,
    client_id: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    target_session = str(session_id or "").strip()
    events: list[dict[str, Any]] = []
    for events_path in _durable_session_event_paths(
        root_dir,
        client_id=client_id,
        target_session=target_session,
    ):
        meta = _load_durable_session_meta(events_path.parent / "meta.json")
        for line_number, item in _iter_jsonl_dicts(events_path):
            event = _durable_session_event_payload(root_dir, events_path, line_number, item, meta)
            if _event_matches_scope(event, client_id=client_id, session_id=session_id):
                events.append(event)
    # `_durable_session_event_paths` globs session directories, so the raw
    # append order follows filesystem iteration order rather than time. Sort
    # deterministically by timestamp (then source/line/subject) so callers that
    # do not run their own sort still get a stable, chronological feed.
    events.sort(
        key=lambda item: (
            float(item.get("ts") or 0.0),
            str(item.get("source") or ""),
            int(item.get("line_number") or 0),
            str(item.get("subject") or ""),
        )
    )
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
