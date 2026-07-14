#!/usr/bin/env python3
"""Run a launcher command while owning one exact smart-paste route record."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import clipboard_session


class RouteExecError(RuntimeError):
    """Launcher route registration could not be made exact."""


def _tmux_value(pane: str, fmt: str) -> str:
    completed = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, fmt],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RouteExecError(completed.stderr.strip() or f"tmux did not report {fmt}")
    return completed.stdout.strip()


def _tmux_client_value(
    pane: str,
    *,
    attempts: int = 20,
    delay_seconds: float = 0.05,
) -> str:
    """Wait briefly for a just-created tmux client to attach to its first pane."""
    last_error: RouteExecError | None = None
    for attempt in range(attempts):
        try:
            return _tmux_value(pane, "#{client_name}")
        except RouteExecError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(delay_seconds)
    raise last_error or RouteExecError("tmux did not report #{client_name}")


def identity_from_environment(
    env: dict[str, str] | None = None,
) -> dict[str, str | None]:
    env = os.environ if env is None else env
    pane = env.get("TMUX_PANE")
    if pane and env.get("TMUX"):
        return {
            "tmux_pane": pane,
            "tmux_client": _tmux_client_value(pane),
            "tmux_server": _tmux_value(pane, "#{socket_path}"),
            "terminal_id": None,
        }
    terminal_id = env.get("GHOSTTY_SURFACE_ID") or env.get("TERM_SESSION_ID")
    if not terminal_id:
        raise RouteExecError(
            "direct terminal has no stable Ghostty or TERM_SESSION_ID identity"
        )
    return {
        "tmux_pane": None,
        "tmux_client": None,
        "tmux_server": None,
        "terminal_id": terminal_id,
    }


def run_registered(
    command: Sequence[str],
    *,
    profile: str,
    transport: str,
    target: str | None,
    remote_session: str | None,
    remote_home: str | None,
    hosts_path: Path | None = None,
    state_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> int:
    if not command:
        raise RouteExecError("missing launcher command after --")
    identity = identity_from_environment(env)
    record, path = clipboard_session.register(
        profile=profile,
        transport=transport,
        target=target,
        remote_session=remote_session,
        remote_home=remote_home,
        hosts_path=hosts_path,
        root=state_root,
        **identity,
    )
    child: subprocess.Popen[bytes] | None = None
    previous: dict[int, signal.Handlers] = {}

    def forward(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    try:
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
        child = subprocess.Popen(list(command), env=env)
        return child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        try:
            clipboard_session.unregister(path)
        except (OSError, clipboard_session.SessionError) as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "warning": "route_cleanup_failed",
                        "route_id": record["route_id"],
                        "message": str(exc),
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--transport", required=True)
    parser.add_argument("--target")
    parser.add_argument("--remote-session")
    parser.add_argument("--remote-home")
    parser.add_argument("--hosts", type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    try:
        return run_registered(
            command,
            profile=args.profile,
            transport=args.transport,
            target=args.target,
            remote_session=args.remote_session,
            remote_home=args.remote_home,
            hosts_path=args.hosts,
            state_root=args.state_root,
        )
    except (OSError, RouteExecError, clipboard_session.SessionError) as exc:
        print(f"clipboard-route-exec: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
