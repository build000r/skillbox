"""Shared operator-side safety helpers for box and MCP lifecycle scripts.

This module is intentionally leaf-only and standard-library-only. It owns the
strictest common validation and containment rules used before touching local
inventory or invoking host subprocesses.
"""
from __future__ import annotations

import os
import re
import json
import fcntl
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

INVENTORY_PATH_INVALID = "INVENTORY_PATH_INVALID"
INVENTORY_LOCK_TIMEOUT = "INVENTORY_LOCK_TIMEOUT"

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_SSH_USER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$")
_HOST_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,253}[a-zA-Z0-9])?$")

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b[a-zA-Z0-9_.-]*(?:token|authkey|secret|password|api[_-]?key)[a-zA-Z0-9_.-]*\s*[=:]\s*)([^\s]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bAuthorization:\s*Bearer\s+)([^\s]+)")
_SECRET_TOKEN_RE = re.compile(r"(?i)\b(?:tskey|dop_v1|ghp|github_pat)_[A-Za-z0-9_.-]+")


class InventoryPathError(ValueError):
    """Raised when an inventory path escapes approved operator roots."""

    error_code = INVENTORY_PATH_INVALID


class InventoryLockTimeout(TimeoutError):
    """Raised when an inventory sidecar lock cannot be acquired."""

    error_code = INVENTORY_LOCK_TIMEOUT


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _redact_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    return _SECRET_TOKEN_RE.sub("[REDACTED]", text)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON by temp-file + fsync + atomic replace.

    The temporary file lives in the target directory so ``os.replace`` does not
    cross filesystems. A crash mid-write leaves either the previous complete
    file or the new complete file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_json_if_present(path: Path, *, default: Any, tolerate_corrupt: bool) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        if tolerate_corrupt:
            return default
        raise


def locked_inventory_update(
    path: Path,
    updater_fn: Callable[[Any], Any],
    *,
    default: Any = None,
    timeout: float = 5.0,
    tolerate_corrupt: bool = False,
) -> Any:
    """Run a locked, atomic JSON read-modify-write cycle.

    A ``<path>.lock`` sidecar serializes concurrent writers. The updater sees
    the current parsed JSON value or ``default`` when the inventory is absent
    (and, only when requested for recovery, corrupt).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout)

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise InventoryLockTimeout(f"Timed out acquiring inventory lock {lock_path}") from exc
                time.sleep(0.02)
        try:
            current = _read_json_if_present(target, default=default, tolerate_corrupt=tolerate_corrupt)
            updated = updater_fn(current)
            atomic_write_json(target, updated)
            return updated
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def validate_identifier(value: str, kind: str) -> str:
    if not value:
        raise ValueError(f"Invalid {kind}: must not be empty")
    if "/" in value or "\\" in value:
        raise ValueError(f"Invalid {kind}: must not contain path separators")
    if value.startswith("-"):
        raise ValueError(f"Invalid {kind}: must not start with '-'")
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(
            f"Invalid {kind}: must be a slug matching [a-zA-Z0-9][a-zA-Z0-9._-]{{0,63}}"
        )
    return value


def validate_box_id(box_id: str) -> str:
    try:
        return validate_identifier(box_id, "box_id")
    except ValueError as exc:
        message = str(exc).replace("Invalid box_id", "invalid box_id", 1)
        message = message.replace(
            "must be a slug matching [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}",
            "must match [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}",
        )
        raise ValueError(message) from exc


def validate_profile_name(profile_name: str) -> str:
    name = str(profile_name or "").strip()
    try:
        validate_identifier(name, "box profile name")
    except ValueError as exc:
        message = str(exc).replace("Invalid box profile name", "Invalid box profile name", 1)
        message = message.replace(
            "must be a slug matching [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}",
            "must match [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}",
        )
        raise ValueError(message) from exc
    return name


def validate_ssh_user(user: str, kind: str = "ssh_user") -> str:
    if not isinstance(user, str) or not _SSH_USER_RE.match(user):
        raise ValueError(f"Invalid {kind}: {user!r}")
    return user


def validate_host(host: str, kind: str = "host") -> str:
    if not isinstance(host, str) or not _HOST_RE.match(host):
        raise ValueError(f"Invalid {kind}: {host!r}")
    return host


def resolve_inventory_path(
    *,
    repo_root: Path | None = None,
    state_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    env_map = os.environ if env is None else env
    root = (repo_root or _repo_root()).resolve()
    state_root_value = state_root or Path(env_map.get("SKILLBOX_STATE_ROOT") or (root / ".skillbox-state"))
    state_root_resolved = state_root_value.expanduser().resolve()

    override = str(env_map.get("SKILLBOX_BOX_INVENTORY") or "").strip()
    raw_path = Path(override).expanduser() if override else root / "workspace" / "boxes.json"
    resolved = raw_path.resolve()
    allowed_roots = (root, state_root_resolved)
    if any(_is_relative_to(resolved, allowed) for allowed in allowed_roots):
        return resolved
    roots = ", ".join(str(path) for path in allowed_roots)
    raise InventoryPathError(
        f"{INVENTORY_PATH_INVALID}: inventory path {resolved} must be under one of: {roots}"
    )


def run_checked(
    cmd: Sequence[str],
    timeout: int,
    redact: bool = True,
    *,
    cwd: str | Path | None = None,
    input_text: str | None = None,
) -> dict[str, Any]:
    start = time.monotonic()
    command = [str(part) for part in cmd]
    try:
        proc = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
        )
        stdout = _redact_text(proc.stdout) if redact else (proc.stdout or "")
        stderr = _redact_text(proc.stderr) if redact else (proc.stderr or "")
        return {
            "rc": proc.returncode,
            "stdout": stdout,
            "stderr_redacted": stderr,
            "elapsed": time.monotonic() - start,
        }
    except FileNotFoundError as exc:
        return {
            "rc": -1,
            "stdout": "",
            "stderr_redacted": _redact_text(str(exc)) if redact else str(exc),
            "elapsed": time.monotonic() - start,
            "error_code": "COMMAND_NOT_FOUND",
        }
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.output or "")
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return {
            "rc": exc.returncode,
            "stdout": _redact_text(stdout) if redact else stdout,
            "stderr_redacted": _redact_text(stderr) if redact else stderr,
            "elapsed": time.monotonic() - start,
            "error_code": "CHECK_FAILED",
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "rc": -1,
            "stdout": _redact_text(stdout) if redact else stdout,
            "stderr_redacted": _redact_text(stderr) if redact else stderr,
            "elapsed": time.monotonic() - start,
            "error_code": "TIMEOUT",
        }


__all__ = [
    "INVENTORY_LOCK_TIMEOUT",
    "INVENTORY_PATH_INVALID",
    "InventoryLockTimeout",
    "InventoryPathError",
    "atomic_write_json",
    "locked_inventory_update",
    "resolve_inventory_path",
    "run_checked",
    "validate_box_id",
    "validate_host",
    "validate_identifier",
    "validate_profile_name",
    "validate_ssh_user",
]
