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
from .digest import (
    normalize_sha256,
)

PACKAGE_DIR = Path(__file__).resolve().parents[1]

SCRIPT_DIR = PACKAGE_DIR.parent

DEFAULT_ROOT_DIR = SCRIPT_DIR.parent.resolve()

SCRIPTS_DIR = DEFAULT_ROOT_DIR / "scripts"

CONTEXT_CLAUDE_REL = Path("home") / ".claude" / "CLAUDE.md"

CONTEXT_CODEX_REL = Path("home") / ".codex" / "AGENTS.md"

CONTEXT_SYMLINK_TARGET = os.path.join("..", ".claude", "CLAUDE.md")

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

def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    ensure_directory(path.parent, dry_run=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")

def resolve_root_dir(raw_root: str | None) -> Path:
    if raw_root:
        return Path(raw_root).resolve()
    return DEFAULT_ROOT_DIR

_ATOMICITY_KILL_AT_ENV = "SKILLBOX_TEST_ATOMICITY_KILL_AT"

_ATOMICITY_COUNTER_ENV = "SKILLBOX_TEST_ATOMICITY_COUNTER_FILE"

_atomicity_checkpoint_count = 0

def _atomicity_checkpoint(label: str) -> None:
    """Test-only kill hook for sync atomicity harnesses.

    Production runs do nothing: the hook is enabled only by an intentionally
    obscure test env var. Keeping the hook in the shared write primitive lets
    tests SIGKILL at deterministic publish boundaries without real sleeps.
    """
    raw_target = os.environ.get(_ATOMICITY_KILL_AT_ENV)
    if not raw_target:
        return
    try:
        target = int(raw_target)
    except ValueError:
        return

    global _atomicity_checkpoint_count
    _atomicity_checkpoint_count += 1
    checkpoint = _atomicity_checkpoint_count

    counter_path = os.environ.get(_ATOMICITY_COUNTER_ENV)
    if counter_path:
        try:
            Path(counter_path).parent.mkdir(parents=True, exist_ok=True)
            Path(counter_path).write_text(
                json.dumps({"checkpoint": checkpoint, "label": label}) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    if checkpoint == target:
        os.kill(os.getpid(), signal.SIGKILL)

def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    mode: int | None = None,
    fsync: bool = True,
) -> None:
    # Crash-safe byte write: stage to a sibling temp file, then os.replace().
    path = Path(path)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(payload)
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        _atomicity_checkpoint(f"file-before-replace:{path}")
        os.replace(tmp_path, path)
        if fsync:
            _fsync_directory(path.parent)
        _atomicity_checkpoint(f"file-after-replace:{path}")
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise

def atomic_write_text(path: Path, content: str, *, fsync: bool = True) -> None:
    atomic_write_bytes(path, content.encode("utf-8"), fsync=fsync)

def write_text_file(path: Path, content: str, dry_run: bool) -> None:
    ensure_directory(path.parent, dry_run)
    if dry_run:
        return
    atomic_write_text(path, content)

DEFAULT_STATE_LOCK_TIMEOUT_SECONDS = 5.0

_STATE_LOCK_POLL_INTERVAL_SECONDS = 0.02

_STATE_LOCK_WAIT_WARN_SECONDS = 0.1

_flock_unsupported_warned = False

class StateLockTimeout(RuntimeError):
    """Raised when a state lock cannot be acquired within the timeout.

    Carries the lock path and the lock file's age (seconds since its mtime) so
    callers can surface *which* lock is stuck and roughly how long it has been
    held without ever blocking forever.
    """

    def __init__(self, lock_path: Path, age_seconds: float | None) -> None:
        self.lock_path = Path(lock_path)
        self.age_seconds = age_seconds
        age_text = "unknown" if age_seconds is None else f"{age_seconds:.3f}s"
        super().__init__(
            f"Timed out acquiring state lock {self.lock_path} (held for {age_text})."
        )

def _state_lock_age_seconds(lock_path: Path) -> float | None:
    try:
        return max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return None

def _warn_flock_unsupported(lock_path: Path, exc: OSError) -> None:
    """Emit a one-time stderr warning that flock is unavailable here."""
    global _flock_unsupported_warned
    if _flock_unsupported_warned:
        return
    _flock_unsupported_warned = True
    print(
        f"[state-fs] advisory flock unsupported on {lock_path} ({exc}); "
        "falling back to atomic-rename-only writes (no read-modify-write "
        "serialization).",
        file=sys.stderr,
    )

def read_json_tolerant(path: Path, default: Any = None) -> Any:
    """Read+parse a JSON file, tolerating a single torn-read.

    On ``JSONDecodeError`` (which can happen on NFS-ish filesystems where a
    rename is briefly observed mid-flight) we retry the read ONCE before
    giving up and returning ``default``. Missing files return ``default``
    immediately.

    INVARIANT: pure reads need NO lock. Because every writer publishes via an
    atomic ``os.replace`` rename, a reader either sees the whole old file or
    the whole new file — never a partial one. Do NOT add ritual read-locks
    around callers of this function; that only reintroduces contention without
    buying any consistency.
    """
    path = Path(path)
    for attempt in range(2):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except json.JSONDecodeError:
            if attempt == 0:
                continue
            return default
        except OSError:
            return default
    return default

def _atomic_write_json_serialized(path: Path, serialized: str) -> None:
    """Temp-write + flush + fsync + atomic rename of pre-serialized JSON."""
    atomic_write_text(Path(path), serialized, fsync=True)

def atomic_write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Crash-safe overwrite of a JSON file (no lock).

    For plain overwrites (full-snapshot writers, not read-modify-write) this is
    sufficient on its own: a mid-write crash leaves the previous file intact,
    and readers using ``read_json_tolerant`` never see a partial file. When a
    writer must read-then-modify shared state, use ``locked_json_update``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=indent, default=str) + "\n"
    _atomic_write_json_serialized(path, serialized)

def locked_json_update(
    path: Path,
    mutate_fn: Callable[[Any], Any],
    *,
    timeout: float = DEFAULT_STATE_LOCK_TIMEOUT_SECONDS,
    indent: int = 2,
) -> Any:
    """Serialize + atomize a read-modify-write cycle on a JSON state file.

    Acquires an advisory ``fcntl.flock`` on a sidecar ``<path>.lock`` (created
    if absent) using a bounded ``LOCK_NB`` retry loop — NEVER a blocking flock,
    so contention always times out rather than deadlocking. Then reads the
    current JSON tolerantly, calls ``mutate_fn(current)`` to produce the new
    value, writes it to a temp file in the SAME directory, ``flush``es +
    ``os.fsync``s, and ``os.replace``s it into place (atomic rename). The lock
    is always released in a ``finally``.

    Returns whatever ``mutate_fn`` returned (the persisted value).

    Raises ``StateLockTimeout`` (carrying the lock path + age) if the lock is
    not acquired within ``timeout`` seconds.

    Degrades gracefully where flock is unsupported: if ``fcntl.flock`` raises an
    ``OSError`` (e.g. on a filesystem without lock support), it falls back to
    an atomic-rename-only write with a one-time stderr warning — the write
    still succeeds, it just loses read-modify-write serialization.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")

    def _commit() -> Any:
        current = read_json_tolerant(path, default=None)
        new_value = mutate_fn(current)
        serialized = json.dumps(new_value, indent=indent, default=str) + "\n"
        _atomic_write_json_serialized(path, serialized)
        return new_value

    lock_fd: int | None = None
    try:
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            # Could not even create the sidecar lock (read-only dir, etc.).
            # Atomic rename still protects readers; proceed without the lock.
            _warn_flock_unsupported(lock_path, OSError("cannot open lock file"))
            return _commit()

        deadline = time.monotonic() + max(0.0, timeout)
        wait_started = time.monotonic()
        acquired = False
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateLockTimeout(lock_path, _state_lock_age_seconds(lock_path))
                time.sleep(_STATE_LOCK_POLL_INTERVAL_SECONDS)
            except OSError as exc:
                # flock unsupported on this filesystem — degrade gracefully.
                _warn_flock_unsupported(lock_path, exc)
                return _commit()

        waited = time.monotonic() - wait_started
        if acquired and waited > _STATE_LOCK_WAIT_WARN_SECONDS:
            print(
                f"[state-fs] waited {waited * 1000:.0f}ms for state lock {lock_path}.",
                file=sys.stderr,
            )
        return _commit()
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)

def normalize_host_rel_path(root_dir: Path, path: Path) -> str:
    rel_path = os.path.relpath(path, root_dir)
    if rel_path == ".":
        return rel_path
    if rel_path.startswith("."):
        return rel_path
    return f"./{rel_path}"

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

def _recover_interrupted_tree_swap(target_dir: Path, backup_dir: Path) -> None:
    if backup_dir.exists() and not target_dir.exists():
        backup_dir.replace(target_dir)
        _fsync_directory(target_dir.parent)
        return
    if backup_dir.exists():
        remove_path(backup_dir)

def atomic_replace_tree(
    target_dir: Path,
    build_fn: Callable[[Path], None],
    *,
    root_mode: int | None = None,
    fsync: bool = False,
) -> None:
    """Build a directory in a sibling stage and publish it with rename.

    Directory replacement cannot portably exchange a populated existing tree in
    one syscall, so an existing target is first renamed to a sibling backup and
    restored on failure. A SIGKILL during that narrow publish window can leave
    the target absent, but never half-populated; lockfile-last sync ordering
    lets doctor report the item as unsynced and the next sync restores it.
    """
    target_dir = Path(target_dir)
    parent = target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    backup_dir = parent / f".{target_dir.name}.old"
    _recover_interrupted_tree_swap(target_dir, backup_dir)

    stage_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{target_dir.name}.stage.",
            dir=str(parent),
        )
    )
    try:
        build_fn(stage_dir)
        if root_mode is not None:
            stage_dir.chmod(root_mode)
        if fsync:
            for file_path in sorted(path for path in stage_dir.rglob("*") if path.is_file()):
                if file_path.is_symlink():
                    continue
                try:
                    with file_path.open("rb") as handle:
                        os.fsync(handle.fileno())
                except OSError:
                    pass
        _atomicity_checkpoint(f"tree-before-swap:{target_dir}")
        if target_dir.exists() or target_dir.is_symlink():
            if backup_dir.exists():
                remove_path(backup_dir)
            target_dir.replace(backup_dir)
            _atomicity_checkpoint(f"tree-after-backup:{target_dir}")
        stage_dir.replace(target_dir)
        if fsync:
            _fsync_directory(target_dir)
        _fsync_directory(parent)
        _atomicity_checkpoint(f"tree-after-publish:{target_dir}")
        if backup_dir.exists():
            remove_path(backup_dir)
            _fsync_directory(parent)
    except BaseException:
        try:
            if stage_dir.exists():
                remove_path(stage_dir)
            if backup_dir.exists() and not target_dir.exists():
                backup_dir.replace(target_dir)
                _fsync_directory(parent)
        except OSError:
            pass
        raise

def copy_tree_atomic(source_dir: Path, target_dir: Path) -> None:
    def _build(stage_dir: Path) -> None:
        shutil.copytree(source_dir, stage_dir, dirs_exist_ok=True)

    atomic_replace_tree(target_dir, _build)

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
    atomic_write_text(path, serialized)
    return True

def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
