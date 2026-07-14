#!/usr/bin/env python3
"""Contained, content-addressed paste artifact transport.

The receiver is intentionally usable as a standalone remote command.  It reads
exactly one bounded artifact from stdin, verifies the caller-provided digest,
and publishes it atomically under a private per-user cache directory.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, BinaryIO

SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_QUOTA_BYTES = 200 * 1024 * 1024
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 10.0
ARTIFACT_NAME = re.compile(r"^(?P<sha>[0-9a-f]{64})\.(?P<ext>[a-z0-9]{1,8})$")
SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,8}$")
SAFE_TARGET = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+$")


class TransferError(RuntimeError):
    """A fail-closed transport or receiver error."""


def _private_dir(path: Path) -> Path:
    """Create *path* without accepting a symlink at any existing component."""
    raw_path = path.expanduser()
    if raw_path.is_symlink():
        raise TransferError(
            f"artifact root component is not a real directory: {raw_path}"
        )
    path = raw_path.resolve(strict=False)
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TransferError(
                f"artifact root component is not a real directory: {current}"
            )
        if current == path and info.st_uid != os.getuid():
            raise TransferError(f"artifact root is not owned by this user: {current}")
    os.chmod(path, 0o700)
    return path.resolve(strict=True)


def default_artifact_root() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache).expanduser() if cache else Path.home() / ".cache"
    return base / "skillbox" / "paste-artifacts"


def validate_digest(value: str) -> str:
    value = value.lower()
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise TransferError(
            "sha256 must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def validate_extension(value: str) -> str:
    value = value.lower().lstrip(".")
    if SAFE_EXTENSION.fullmatch(value) is None:
        raise TransferError(
            "extension must contain 1-8 lowercase alphanumeric characters"
        )
    return value


def validate_target(value: str) -> str:
    if SAFE_TARGET.fullmatch(value) is None or value.startswith("-"):
        raise TransferError("unsafe SSH target")
    return value


def _regular_owned_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TransferError(f"artifact path is not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise TransferError(f"artifact path is not owned by this user: {path}")
    return info


def _read_regular_owned_file(
    path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> bytes:
    """Read one bounded file without following or racing a path replacement."""
    before = _regular_owned_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TransferError(f"artifact path could not be opened safely: {path}") from exc
    try:
        after = os.fstat(fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_uid != os.getuid()
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise TransferError(f"artifact path changed before it could be read: {path}")
        if after.st_size > max_bytes:
            raise TransferError(f"artifact exceeds {max_bytes} byte limit")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, min(1024 * 1024, max_bytes + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise TransferError(f"artifact exceeds {max_bytes} byte limit")
        data = b"".join(chunks)
        if len(data) != after.st_size:
            raise TransferError(f"artifact changed while it was being read: {path}")
        return data
    finally:
        os.close(fd)


def sha256_file(path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[str, int]:
    data = _read_regular_owned_file(path, max_bytes=max_bytes)
    return hashlib.sha256(data).hexdigest(), len(data)


def _artifact_payload(
    path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[bytes, str]:
    data = _read_regular_owned_file(path, max_bytes=max_bytes)
    return data, hashlib.sha256(data).hexdigest()


def _artifact_files(root: Path) -> list[tuple[Path, os.stat_result]]:
    result: list[tuple[Path, os.stat_result]] = []
    for path in root.iterdir():
        if ARTIFACT_NAME.fullmatch(path.name) is None:
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise TransferError(f"symlink found in artifact store: {path.name}")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise TransferError(f"unowned or non-regular artifact found: {path.name}")
        result.append((path, info))
    return result


def cleanup_store(
    root: Path,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    quota_bytes: int = DEFAULT_QUOTA_BYTES,
    now: float | None = None,
    preserve: set[Path] | None = None,
) -> dict[str, int]:
    """Delete only owned content-addressed artifacts, oldest first."""
    if ttl_seconds < 0 or quota_bytes < 1:
        raise TransferError("cleanup TTL and quota must be positive")
    now = time.time() if now is None else now
    preserve = preserve or set()
    removed_files = 0
    removed_bytes = 0
    files = _artifact_files(root)
    for path, info in files:
        if path in preserve or now - info.st_mtime <= ttl_seconds:
            continue
        path.unlink()
        removed_files += 1
        removed_bytes += info.st_size

    files = sorted(
        _artifact_files(root), key=lambda item: (item[1].st_mtime, item[0].name)
    )
    total = sum(info.st_size for _, info in files)
    for path, info in files:
        if total <= quota_bytes:
            break
        if path in preserve:
            continue
        path.unlink()
        removed_files += 1
        removed_bytes += info.st_size
        total -= info.st_size
    return {
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "remaining_bytes": total,
    }


def delete_artifact(
    *,
    sha256: str,
    extension: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Remove one exact owned content-addressed artifact after a canceled paste."""
    digest = validate_digest(sha256)
    extension = validate_extension(extension)
    root = _private_dir(root or default_artifact_root())
    path = root / f"{digest}.{extension}"
    if path.parent != root or ARTIFACT_NAME.fullmatch(path.name) is None:
        raise TransferError("artifact path escaped the store")
    lock_path = root / ".lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not path.exists() and not path.is_symlink():
            removed = False
        else:
            _regular_owned_file(path)
            observed, _ = sha256_file(path)
            if observed != digest:
                raise TransferError("content-addressed artifact digest changed")
            path.unlink()
            removed = True
    finally:
        os.close(lock_fd)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "sha256": digest,
        "removed": removed,
    }


def receive_artifact(
    stream: BinaryIO,
    *,
    expected_sha256: str,
    expected_size: int,
    extension: str,
    root: Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    quota_bytes: int = DEFAULT_QUOTA_BYTES,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    """Receive one artifact and return a redacted receipt."""
    digest_value = validate_digest(expected_sha256)
    extension = validate_extension(extension)
    if expected_size < 1 or expected_size > max_bytes:
        raise TransferError(f"size must be between 1 and {max_bytes} bytes")
    root = _private_dir(root or default_artifact_root())
    final = root / f"{digest_value}.{extension}"
    if final.parent != root or ARTIFACT_NAME.fullmatch(final.name) is None:
        raise TransferError("artifact path escaped the store")

    lock_path = root / ".lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    tmp_path: Path | None = None
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if final.exists() or final.is_symlink():
            _regular_owned_file(final)
            observed, size = sha256_file(final, max_bytes=max_bytes)
            if observed != digest_value or size != expected_size:
                raise TransferError(
                    "existing content-addressed artifact does not match receipt"
                )
            cleanup = cleanup_store(
                root,
                ttl_seconds=ttl_seconds,
                quota_bytes=quota_bytes,
                now=now,
                preserve={final},
            )
            return _receipt(final, digest_value, size, reused=True, cleanup=cleanup)

        fd, raw_path = tempfile.mkstemp(prefix=".incoming-", dir=root)
        tmp_path = Path(raw_path)
        os.fchmod(fd, 0o600)
        digest = hashlib.sha256()
        received = 0
        try:
            with os.fdopen(fd, "wb", closefd=True) as output:
                while received < expected_size:
                    chunk = stream.read(min(1024 * 1024, expected_size - received))
                    if not chunk:
                        raise TransferError(
                            f"partial artifact: received {received} of {expected_size} bytes"
                        )
                    received += len(chunk)
                    digest.update(chunk)
                    output.write(chunk)
                extra = stream.read(1)
                if extra:
                    raise TransferError(
                        "artifact stream contained bytes beyond declared size"
                    )
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise

        if digest.hexdigest() != digest_value:
            raise TransferError("artifact sha256 mismatch")
        os.replace(tmp_path, final)
        tmp_path = None
        os.chmod(final, 0o600)
        dir_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        cleanup = cleanup_store(
            root,
            ttl_seconds=ttl_seconds,
            quota_bytes=quota_bytes,
            now=now,
            preserve={final},
        )
        return _receipt(
            final, digest_value, expected_size, reused=False, cleanup=cleanup
        )
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
        os.close(lock_fd)


def _receipt(
    path: Path,
    digest: str,
    size: int,
    *,
    reused: bool,
    cleanup: dict[str, int],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "path": str(path),
        "sha256": digest,
        "byte_size": size,
        "mode": "0600",
        "reused": reused,
        "cleanup": cleanup,
    }


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def _redacted_remote_failure(stderr: bytes, returncode: int) -> str:
    """Classify SSH/receiver failures without persisting remote stderr."""
    message = stderr.decode("utf-8", "replace").lower()
    if "permission denied" in message or "authentication" in message:
        return "authentication failed"
    if any(
        marker in message
        for marker in (
            "connection refused",
            "connection timed out",
            "no route to host",
            "could not resolve hostname",
        )
    ):
        return "target is offline or unreachable"
    return f"receiver rejected the request (exit {returncode})"


def transfer_artifact(
    local_path: Path,
    *,
    ssh_target: str,
    extension: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    runner: Runner = subprocess.run,
    remote_command: str = "clipboard-artifact-receive",
) -> dict[str, Any]:
    """Send one local file to a registered SSH route."""
    target = validate_target(ssh_target)
    local_path = local_path.expanduser()
    payload, digest = _artifact_payload(local_path, max_bytes=max_bytes)
    size = len(payload)
    extension = validate_extension(extension or local_path.suffix)
    command: Sequence[str] = (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(timeout_seconds))}",
        target,
        remote_command,
        "put",
        "--sha256",
        digest,
        "--size",
        str(size),
        "--extension",
        extension,
        "--max-bytes",
        str(max_bytes),
    )
    try:
        completed = runner(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TransferError(
            f"artifact transfer timed out after {timeout_seconds:g}s"
        ) from exc
    if completed.returncode != 0:
        raise TransferError(
            "remote artifact receiver failed: "
            + _redacted_remote_failure(completed.stderr, completed.returncode)
        )
    try:
        receipt = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransferError("remote receiver returned a malformed receipt") from exc
    expected_keys = {
        "schema_version",
        "ok",
        "path",
        "sha256",
        "byte_size",
        "mode",
        "reused",
        "cleanup",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        raise TransferError("remote receiver returned an unknown receipt schema")
    if (
        receipt["schema_version"] != SCHEMA_VERSION
        or receipt["ok"] is not True
        or receipt["sha256"] != digest
        or receipt["byte_size"] != size
        or receipt["mode"] != "0600"
        or not isinstance(receipt["path"], str)
        or not receipt["path"].startswith("/")
        or any(ord(char) < 32 for char in receipt["path"])
    ):
        raise TransferError("remote receipt does not match the sent artifact")
    remote_path = PurePosixPath(receipt["path"])
    expected_name = f"{digest}.{extension}"
    if (
        ".." in remote_path.parts
        or remote_path.name != expected_name
        or remote_path.parent.parts[-3:] != (".cache", "skillbox", "paste-artifacts")
    ):
        raise TransferError("remote receipt path escaped the artifact store")
    cleanup = receipt["cleanup"]
    if (
        not isinstance(cleanup, dict)
        or set(cleanup)
        != {"removed_files", "removed_bytes", "remaining_bytes"}
        or any(not isinstance(value, int) or value < 0 for value in cleanup.values())
    ):
        raise TransferError("remote receipt cleanup metadata is invalid")
    return receipt


def delete_remote_artifact(
    *,
    ssh_target: str,
    sha256: str,
    extension: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runner: Runner = subprocess.run,
    remote_command: str = "clipboard-artifact-receive",
) -> dict[str, Any]:
    """Delete an exact artifact created by a gesture that lost authorization."""
    target = validate_target(ssh_target)
    digest = validate_digest(sha256)
    extension = validate_extension(extension)
    command: Sequence[str] = (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(timeout_seconds))}",
        target,
        remote_command,
        "delete",
        "--sha256",
        digest,
        "--extension",
        extension,
    )
    try:
        completed = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TransferError(
            f"remote artifact cleanup timed out after {timeout_seconds:g}s"
        ) from exc
    if completed.returncode != 0:
        raise TransferError(
            "remote artifact cleanup failed: "
            + _redacted_remote_failure(completed.stderr, completed.returncode)
        )
    try:
        receipt = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransferError("remote cleanup returned a malformed receipt") from exc
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema_version", "ok", "sha256", "removed"}
        or receipt["schema_version"] != SCHEMA_VERSION
        or receipt["ok"] is not True
        or receipt["sha256"] != digest
        or not isinstance(receipt["removed"], bool)
    ):
        raise TransferError("remote cleanup receipt does not match the artifact")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    put = sub.add_parser("put", help="receive one artifact from stdin")
    put.add_argument("--sha256", required=True)
    put.add_argument("--size", required=True, type=int)
    put.add_argument("--extension", required=True)
    put.add_argument("--root", type=Path)
    put.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    put.add_argument("--quota-bytes", type=int, default=DEFAULT_QUOTA_BYTES)
    put.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    clean = sub.add_parser("cleanup", help="apply TTL and quota cleanup")
    clean.add_argument("--root", type=Path)
    clean.add_argument("--quota-bytes", type=int, default=DEFAULT_QUOTA_BYTES)
    clean.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    delete = sub.add_parser("delete", help="remove one exact canceled artifact")
    delete.add_argument("--sha256", required=True)
    delete.add_argument("--extension", required=True)
    delete.add_argument("--root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, *, stdin: BinaryIO | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = _private_dir(args.root or default_artifact_root())
        if args.command == "put":
            receipt = receive_artifact(
                stdin or sys.stdin.buffer,
                expected_sha256=args.sha256,
                expected_size=args.size,
                extension=args.extension,
                root=root,
                max_bytes=args.max_bytes,
                quota_bytes=args.quota_bytes,
                ttl_seconds=args.ttl_seconds,
            )
        elif args.command == "cleanup":
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "cleanup": cleanup_store(
                    root,
                    ttl_seconds=args.ttl_seconds,
                    quota_bytes=args.quota_bytes,
                ),
            }
        else:
            receipt = delete_artifact(
                sha256=args.sha256,
                extension=args.extension,
                root=root,
            )
    except (OSError, TransferError) as exc:
        print(f"clipboard-artifact-receive: {exc}", file=sys.stderr)
        return 1
    json.dump(receipt, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
