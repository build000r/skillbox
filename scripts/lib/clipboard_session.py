#!/usr/bin/env python3
"""Exact focused-session registry for Skillbox smart paste."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import clipboard_route

SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 24 * 60 * 60
MAX_RECORD_BYTES = 64 * 1024
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._%:/@+-]{1,256}$")
SAFE_TRANSPORTS = {"local", "ssh", "mosh", "wsl"}
TMUX_ROUTE_OPTION = "@skillbox_paste_route"
TMUX_GENERATION_OPTION = "@skillbox_paste_generation"


class SessionError(RuntimeError):
    """A stale, ambiguous, or malformed focused-session record."""


def default_state_root() -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "skillbox" / "paste-routes"


def _private_root(root: Path | None = None) -> Path:
    raw = (root or default_state_root()).expanduser()
    if raw.is_symlink():
        raise SessionError("route registry root must not be a symlink")
    raw.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = raw.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SessionError("route registry root must be an owned directory")
    os.chmod(raw, 0o700)
    return raw.resolve(strict=True)


def _safe(value: str | None, label: str, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise SessionError(f"{label} is required")
        return None
    if SAFE_TOKEN.fullmatch(value) is None or any(ord(char) < 32 for char in value):
        raise SessionError(f"unsafe {label}")
    return value


def _identity(
    *,
    tmux_server: str | None,
    tmux_pane: str | None,
    tmux_client: str | None,
    terminal_id: str | None,
) -> str:
    if tmux_pane or tmux_client or tmux_server:
        if not tmux_pane or not tmux_client:
            raise SessionError("local tmux registration requires both pane and client")
        material = f"tmux\0{tmux_server or 'default'}\0{tmux_client}\0{tmux_pane}"
    elif terminal_id:
        material = f"terminal\0{terminal_id}"
    else:
        raise SessionError(
            "registration requires an exact tmux pane/client or terminal ID"
        )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _write_record(root: Path, record: Mapping[str, Any]) -> Path:
    if not isinstance(record["route_id"], str):
        raise SessionError("route ID is malformed")
    route_id = record["route_id"]
    if re.fullmatch(r"[0-9a-f]{32}", route_id) is None:
        raise SessionError("invalid route ID")
    final = root / f"{route_id}.json"
    fd, raw = tempfile.mkstemp(prefix=".route-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, final)
        os.chmod(final, 0o600)
        dir_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(raw)
        except FileNotFoundError:
            pass
    return final


def _tmux(
    args: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    completed = runner(
        ["tmux", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise SessionError(completed.stderr.strip() or "tmux command failed")
    return completed


def stamp_tmux_pane(
    pane: str,
    record_path: Path,
    generation: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    _tmux(
        ["set-option", "-p", "-t", pane, TMUX_ROUTE_OPTION, str(record_path)],
        runner=runner,
    )
    _tmux(
        ["set-option", "-p", "-t", pane, TMUX_GENERATION_OPTION, generation],
        runner=runner,
    )


def clear_tmux_pane(
    pane: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    for option in (TMUX_ROUTE_OPTION, TMUX_GENERATION_OPTION):
        completed = runner(
            ["tmux", "set-option", "-p", "-u", "-t", pane, option],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if (
            completed.returncode != 0
            and "invalid option" not in completed.stderr.lower()
        ):
            raise SessionError(
                completed.stderr.strip() or "could not clear tmux route option"
            )


def register(
    *,
    profile: str,
    transport: str,
    tmux_pane: str | None = None,
    tmux_client: str | None = None,
    tmux_server: str | None = None,
    terminal_id: str | None = None,
    target: str | None = None,
    remote_session: str | None = None,
    remote_home: str | None = None,
    root: Path | None = None,
    hosts_path: Path | None = None,
    now: float | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    generation: str | None = None,
    stamp_tmux: bool = True,
    tmux_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], Path]:
    if transport not in SAFE_TRANSPORTS:
        raise SessionError(f"unsupported transport: {transport}")
    if ttl_seconds < 1:
        raise SessionError("route TTL must be positive")
    tmux_pane = _safe(tmux_pane, "tmux pane")
    tmux_client = _safe(tmux_client, "tmux client")
    tmux_server = _safe(tmux_server, "tmux server")
    terminal_id = _safe(terminal_id, "terminal ID")
    remote_session = _safe(remote_session, "remote session")
    if hosts_path is None:
        source_hosts = (
            Path(__file__).resolve().parent.parent / "clipboard" / "hosts.json"
        )
        installed_hosts = Path.home() / ".config" / "skillbox" / "clipboard-hosts.json"
        default_hosts = source_hosts if source_hosts.is_file() else installed_hosts
        hosts_path = Path(os.environ.get("SKILLBOX_CLIPBOARD_HOSTS", default_hosts))
    route_data = clipboard_route.load_host_config(hosts_path)
    route = clipboard_route.resolve_profile(profile, data=route_data, target=target)
    if route.get("ssh_target") is None and transport != "local":
        raise SessionError("remote transport requires an SSH target")
    canonical_home = route.get("remote_home")
    if remote_home is not None and remote_home != canonical_home:
        raise SessionError("remote home contradicts the canonical route")
    route_id = _identity(
        tmux_server=tmux_server,
        tmux_pane=tmux_pane,
        tmux_client=tmux_client,
        terminal_id=terminal_id,
    )
    now = time.time() if now is None else now
    generation = generation or str(uuid.uuid4())
    try:
        uuid.UUID(generation)
    except ValueError as exc:
        raise SessionError("generation must be a UUID") from exc
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "route_id": route_id,
        "generation": generation,
        "profile": route["profile"],
        "ssh_target": route.get("ssh_target"),
        "transport": transport,
        "remote_home": canonical_home,
        "remote_session": remote_session,
        "local": {
            "tmux_server": tmux_server,
            "tmux_pane": tmux_pane,
            "tmux_client": tmux_client,
            "terminal_id": terminal_id,
        },
        "capabilities": route["capabilities"],
        "trust": route["trust"],
        "created_at": now,
        "updated_at": now,
        "expires_at": now + ttl_seconds,
        "owner_uid": os.getuid(),
        "cleanup_owner": "launching-process",
    }
    state_root = _private_root(root)
    path = _write_record(state_root, record)
    if tmux_pane and stamp_tmux:
        try:
            stamp_tmux_pane(tmux_pane, path, generation, runner=tmux_runner)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return record, path


def load_record(
    path: Path,
    *,
    now: float | None = None,
    expected_generation: str | None = None,
    expected_pane: str | None = None,
    expected_client: str | None = None,
) -> dict[str, Any]:
    parent = path.parent.lstat()
    if (
        stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise SessionError("route record parent has unsafe ownership or mode")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SessionError("route record must be a regular file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise SessionError("route record has unsafe ownership or mode")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if (
                (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) & 0o077
            ):
                raise SessionError("route record changed before it could be read")
            raw = os.read(fd, MAX_RECORD_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_RECORD_BYTES:
            raise SessionError("route record exceeds the size limit")
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionError("route record is malformed") from exc
    required = {
        "schema_version",
        "route_id",
        "generation",
        "profile",
        "ssh_target",
        "transport",
        "remote_home",
        "remote_session",
        "local",
        "capabilities",
        "trust",
        "created_at",
        "updated_at",
        "expires_at",
        "owner_uid",
        "cleanup_owner",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise SessionError("route record has an unknown schema")
    if (
        type(record["schema_version"]) is not int
        or record["schema_version"] != SCHEMA_VERSION
        or type(record["owner_uid"]) is not int
        or record["owner_uid"] != os.getuid()
    ):
        raise SessionError("route record version or owner does not match")
    route_id = str(record["route_id"])
    if (
        re.fullmatch(r"[0-9a-f]{32}", route_id) is None
        or path.name != f"{route_id}.json"
    ):
        raise SessionError("route record path does not match its route ID")
    if not isinstance(record["generation"], str):
        raise SessionError("route generation is not a UUID")
    try:
        uuid.UUID(record["generation"])
    except ValueError as exc:
        raise SessionError("route generation is not a UUID") from exc
    transport = record["transport"]
    if transport not in SAFE_TRANSPORTS:
        raise SessionError("route transport is unsupported")
    if not isinstance(record["profile"], str):
        raise SessionError("route profile is malformed")
    _safe(record["profile"], "profile", required=True)
    target = record["ssh_target"]
    if target is not None:
        if not isinstance(target, str):
            raise SessionError("route SSH target is malformed")
        try:
            clipboard_route.validate_ssh_target(target)
        except clipboard_route.HostConfigError as exc:
            raise SessionError("route SSH target is unsafe") from exc
    if transport != "local" and target is None:
        raise SessionError("remote route is missing its SSH target")
    if record["remote_home"] is not None and not isinstance(
        record["remote_home"], str
    ):
        raise SessionError("route remote home is malformed")
    remote_home = _safe(record["remote_home"], "remote home")
    if remote_home is not None:
        parts = Path(remote_home).parts
        if not remote_home.startswith("/") or ".." in parts:
            raise SessionError("route remote home is unsafe")
    if record["remote_session"] is not None and not isinstance(
        record["remote_session"], str
    ):
        raise SessionError("route remote session is malformed")
    _safe(record["remote_session"], "remote session")
    now = time.time() if now is None else now
    for field in ("created_at", "updated_at", "expires_at"):
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SessionError("route timestamps are malformed")
    if not (
        record["created_at"] <= record["updated_at"] < record["expires_at"]
    ):
        raise SessionError("route timestamps are contradictory")
    if (
        not isinstance(record["expires_at"], (int, float))
        or record["expires_at"] <= now
    ):
        raise SessionError("route record is stale")
    if expected_generation and record["generation"] != expected_generation:
        raise SessionError("route generation changed before paste")
    local = record["local"]
    if not isinstance(local, dict) or set(local) != {
        "tmux_server",
        "tmux_pane",
        "tmux_client",
        "terminal_id",
    }:
        raise SessionError("route local identity is malformed")
    for field, label in (
        ("tmux_server", "tmux server"),
        ("tmux_pane", "tmux pane"),
        ("tmux_client", "tmux client"),
        ("terminal_id", "terminal ID"),
    ):
        value = local[field]
        if value is not None and not isinstance(value, str):
            raise SessionError("route local identity is malformed")
        _safe(value, label)
    expected_route_id = _identity(
        tmux_server=local["tmux_server"],
        tmux_pane=local["tmux_pane"],
        tmux_client=local["tmux_client"],
        terminal_id=local["terminal_id"],
    )
    if expected_route_id != route_id:
        raise SessionError("route ID does not match its local identity")
    capabilities = record["capabilities"]
    if not isinstance(capabilities, dict) or set(capabilities) != {
        "outbound",
        "inbound",
    }:
        raise SessionError("route capabilities are malformed")
    flattened: dict[str, Any] = {}
    for direction in ("outbound", "inbound"):
        values = capabilities[direction]
        if not isinstance(values, dict) or any(
            not isinstance(value, bool) for value in values.values()
        ):
            raise SessionError("route capabilities are malformed")
        flattened.update(values)
    if any(key not in flattened for key in clipboard_route.CAPABILITY_KEYS):
        raise SessionError("route capabilities are incomplete")
    if (
        not isinstance(record["trust"], str)
        or record["trust"] not in clipboard_route.TRUST_LEVELS
    ):
        raise SessionError("route trust level is malformed")
    if record["cleanup_owner"] != "launching-process":
        raise SessionError("route cleanup owner is malformed")
    if expected_pane and local.get("tmux_pane") != expected_pane:
        raise SessionError("route pane does not match the focused pane")
    if expected_client and local.get("tmux_client") != expected_client:
        raise SessionError("route client does not match the focused client")
    return record


def resolve_tmux(
    *,
    pane: str,
    client: str,
    now: float | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], Path]:
    path_value = _tmux(
        ["show-option", "-p", "-v", "-t", pane, TMUX_ROUTE_OPTION], runner=runner
    ).stdout.strip()
    generation = _tmux(
        ["show-option", "-p", "-v", "-t", pane, TMUX_GENERATION_OPTION], runner=runner
    ).stdout.strip()
    if not path_value or not generation:
        raise SessionError("focused pane has no registered paste route")
    path = Path(path_value)
    record = load_record(
        path,
        now=now,
        expected_generation=generation,
        expected_pane=pane,
        expected_client=client,
    )
    return record, path


def unregister(
    path: Path,
    *,
    clear_tmux: bool = True,
    tmux_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    record = load_record(path, now=0)
    pane = record["local"].get("tmux_pane")
    clear_error: SessionError | None = None
    if pane and clear_tmux:
        try:
            clear_tmux_pane(pane, runner=tmux_runner)
        except SessionError as exc:
            # The pane may already be gone.  The launching process still owns
            # the record and must not leave an apparently live route behind.
            clear_error = exc
    path.unlink(missing_ok=True)
    if clear_error is not None:
        raise clear_error


def cleanup(root: Path | None = None, *, now: float | None = None) -> dict[str, int]:
    state_root = _private_root(root)
    now = time.time() if now is None else now
    removed = 0
    kept = 0
    for path in state_root.glob("*.json"):
        try:
            load_record(path, now=now)
        except SessionError:
            path.unlink(missing_ok=True)
            removed += 1
        else:
            kept += 1
    return {"removed": removed, "kept": kept}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register_parser = sub.add_parser("register")
    register_parser.add_argument("--profile", required=True)
    register_parser.add_argument(
        "--transport", required=True, choices=sorted(SAFE_TRANSPORTS)
    )
    register_parser.add_argument("--target")
    register_parser.add_argument("--remote-session")
    register_parser.add_argument("--remote-home")
    register_parser.add_argument("--tmux-pane")
    register_parser.add_argument("--tmux-client")
    register_parser.add_argument("--tmux-server")
    register_parser.add_argument("--terminal-id")
    register_parser.add_argument("--state-root", type=Path)
    register_parser.add_argument("--hosts", type=Path)
    register_parser.add_argument("--no-stamp", action="store_true")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--pane", required=True)
    resolve.add_argument("--client", required=True)
    unregister_parser = sub.add_parser("unregister")
    unregister_parser.add_argument("path", type=Path)
    unregister_parser.add_argument("--no-clear", action="store_true")
    cleanup_parser = sub.add_parser("cleanup")
    cleanup_parser.add_argument("--state-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "register":
            record, path = register(
                profile=args.profile,
                transport=args.transport,
                target=args.target,
                remote_session=args.remote_session,
                remote_home=args.remote_home,
                tmux_pane=args.tmux_pane,
                tmux_client=args.tmux_client,
                tmux_server=args.tmux_server,
                terminal_id=args.terminal_id,
                root=args.state_root,
                hosts_path=args.hosts,
                stamp_tmux=not args.no_stamp,
            )
            output: Mapping[str, Any] = {
                "ok": True,
                "path": str(path),
                "record": record,
            }
        elif args.command == "resolve":
            record, path = resolve_tmux(pane=args.pane, client=args.client)
            output = {"ok": True, "path": str(path), "record": record}
        elif args.command == "unregister":
            unregister(args.path, clear_tmux=not args.no_clear)
            output = {"ok": True, "removed": str(args.path)}
        else:
            output = {"ok": True, **cleanup(args.state_root)}
    except (
        OSError,
        SessionError,
        clipboard_route.HostConfigError,
        json.JSONDecodeError,
    ) as exc:
        print(f"clipboard-session: {exc}", file=sys.stderr)
        return 1
    json.dump(output, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
