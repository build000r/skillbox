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
import random
import subprocess
import tempfile
import time
from dataclasses import dataclass
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
SSH_TRANSPORT_ERROR_PATTERNS = (
    "connection timed out",
    "connection refused",
    "connection reset",
    "kex_exchange",
)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int
    backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0)
    jitter_seconds: float = 0.25
    total_deadline: float = 30.0


SSH_READ_RETRY_POLICY = RetryPolicy(attempts=3)


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


def classify_ssh_failure(result: dict[str, Any]) -> dict[str, Any]:
    """Classify an SSH subprocess result for retry and diagnostics.

    OpenSSH reports transport setup failures as exit 255. We only treat the
    table below as retryable transport failures; other nonzero exits are remote
    command failures and must not be retried automatically.
    """
    rc = int(result.get("rc", -1))
    if rc == 0:
        return {"failure_class": "success", "retryable": False}

    error_code = str(result.get("error_code") or "")
    if error_code == "TIMEOUT":
        return {"failure_class": "ssh_transport", "retryable": True, "matched_pattern": "timeout"}

    stderr = str(result.get("stderr_redacted") or result.get("stderr") or "").lower()
    if rc == 255:
        for pattern in SSH_TRANSPORT_ERROR_PATTERNS:
            if pattern in stderr:
                return {
                    "failure_class": "ssh_transport",
                    "retryable": True,
                    "matched_pattern": pattern,
                }

    return {"failure_class": "remote_command", "retryable": False}


def _run_checked_once(
    cmd: Sequence[str],
    timeout: int,
    *,
    redact: bool,
    cwd: str | Path | None = None,
    input_text: str | None = None,
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    start = monotonic()
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
            "elapsed": monotonic() - start,
        }
    except FileNotFoundError as exc:
        return {
            "rc": -1,
            "stdout": "",
            "stderr_redacted": _redact_text(str(exc)) if redact else str(exc),
            "elapsed": monotonic() - start,
            "error_code": "COMMAND_NOT_FOUND",
        }
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.output or "")
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return {
            "rc": exc.returncode,
            "stdout": _redact_text(stdout) if redact else stdout,
            "stderr_redacted": _redact_text(stderr) if redact else stderr,
            "elapsed": monotonic() - start,
            "error_code": "CHECK_FAILED",
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "rc": -1,
            "stdout": _redact_text(stdout) if redact else stdout,
            "stderr_redacted": _redact_text(stderr) if redact else stderr,
            "elapsed": monotonic() - start,
            "error_code": "TIMEOUT",
        }


def _retry_backoff_seconds(
    policy: RetryPolicy,
    attempt_index: int,
    *,
    jitter: Callable[[float], float] | None,
) -> float:
    base = policy.backoff_seconds[min(attempt_index, len(policy.backoff_seconds) - 1)] if policy.backoff_seconds else 0.0
    jitter_limit = max(0.0, float(policy.jitter_seconds))
    jitter_value = 0.0
    if jitter_limit:
        jitter_value = float(jitter(jitter_limit) if jitter is not None else random.uniform(0.0, jitter_limit))
        jitter_value = max(0.0, jitter_value)
    return max(0.0, float(base) + jitter_value)


def run_checked(
    cmd: Sequence[str],
    timeout: int,
    redact: bool = True,
    *,
    cwd: str | Path | None = None,
    input_text: str | None = None,
    retry_policy: RetryPolicy | None = None,
    retry_classifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    jitter: Callable[[float], float] | None = None,
) -> dict[str, Any]:
    total_start = monotonic()
    attempts_allowed = max(1, int(retry_policy.attempts)) if retry_policy is not None else 1
    deadline_at = total_start + max(0.0, float(retry_policy.total_deadline)) if retry_policy is not None else None
    attempt_details: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None

    for attempt_number in range(1, attempts_allowed + 1):
        attempt_timeout: float = float(timeout)
        if deadline_at is not None:
            remaining = deadline_at - monotonic()
            if remaining <= 0 and attempt_number > 1:
                if final is not None:
                    final["deadline_exhausted"] = True
                break
            attempt_timeout = max(0.001, min(float(timeout), max(0.0, remaining)))

        result = _run_checked_once(
            cmd,
            timeout=attempt_timeout,
            redact=redact,
            cwd=cwd,
            input_text=input_text,
            monotonic=monotonic,
        )
        classification = retry_classifier(result) if retry_classifier is not None else {}
        failure_class = classification.get("failure_class")
        retryable = bool(classification.get("retryable", False))
        if failure_class:
            result["failure_class"] = failure_class
        result["retryable_hint"] = retryable
        final = result

        detail: dict[str, Any] = {
            "attempt": attempt_number,
            "rc": int(result.get("rc", -1)),
            "elapsed": float(result.get("elapsed") or 0.0),
            "retryable": retryable,
        }
        if failure_class:
            detail["failure_class"] = failure_class
        if classification.get("matched_pattern"):
            detail["matched_pattern"] = classification["matched_pattern"]
        attempt_details.append(detail)

        if int(result.get("rc", -1)) == 0 or not retryable or attempt_number >= attempts_allowed:
            break

        backoff = _retry_backoff_seconds(retry_policy, attempt_number - 1, jitter=jitter)
        if deadline_at is not None and monotonic() + backoff >= deadline_at:
            result["deadline_exhausted"] = True
            break
        detail["sleep_before_next"] = backoff
        sleep(backoff)

    if final is None:
        final = {
            "rc": -1,
            "stdout": "",
            "stderr_redacted": "",
            "elapsed": monotonic() - total_start,
            "error_code": "TIMEOUT",
            "deadline_exhausted": True,
            "retryable_hint": bool(retry_classifier),
        }

    final["attempts"] = len(attempt_details)
    final["attempt_details"] = attempt_details
    final["elapsed"] = monotonic() - total_start
    return final


__all__ = [
    "INVENTORY_LOCK_TIMEOUT",
    "INVENTORY_PATH_INVALID",
    "InventoryLockTimeout",
    "InventoryPathError",
    "RetryPolicy",
    "SSH_READ_RETRY_POLICY",
    "SSH_TRANSPORT_ERROR_PATTERNS",
    "atomic_write_json",
    "classify_ssh_failure",
    "locked_inventory_update",
    "resolve_inventory_path",
    "run_checked",
    "validate_box_id",
    "validate_host",
    "validate_identifier",
    "validate_profile_name",
    "validate_ssh_user",
]
