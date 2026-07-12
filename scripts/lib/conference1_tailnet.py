#!/usr/bin/env python3
"""Conference1 tailnet Serve and heavy-build lane surface for SBP.

Reads Conference1 metadata from the existing host registry
(scripts/clipboard/hosts.json, key ``conference1_tailnet``) and exposes:

  urls    offline: MagicDNS Serve URLs, raw-IP portproxy fallback, helper
          commands, Swimmers remote Rust lane, security posture
  status  live, read-only: run the Windows tailnet-serve helper ``list``
          and the ``netsh`` portproxy listing over SSH
  helper  print the exact helper commands without executing anything
  expose / remove
          mutate Tailscale Serve on Conference1; ALWAYS require --yes,
          default is a printed dry-run command

Security invariants enforced here:
  - Funnel is refused outright (no flag can enable it).
  - Rendered metadata is scanned for secret-shaped strings and refused.
  - Live remote output is redacted before printing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib.clipboard_bootstrap import load_hosts, repo_root  # noqa: E402

METADATA_KEY = "conference1_tailnet"
SSH_BASE_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8")
READ_ONLY_ACTIONS = ("list",)
MUTATING_ACTIONS = ("expose", "remove")

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"tskey-[A-Za-z0-9\-]*", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"auth[-_]?key\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-.]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class SecretLeakError(RuntimeError):
    """Rendered output contained a secret-shaped string."""


class FunnelRefusedError(RuntimeError):
    """Tailscale Funnel is never allowed from this surface."""


def load_conference1_tailnet(root: Path | None = None) -> dict[str, Any]:
    data = load_hosts(root)
    meta = data.get(METADATA_KEY)
    if not isinstance(meta, dict):
        raise KeyError(
            f"{METADATA_KEY} missing from scripts/clipboard/hosts.json; "
            "re-add the Conference1 tailnet metadata block"
        )
    return meta


def find_secret_leaks(text: str) -> list[str]:
    return [pattern.pattern for pattern in SECRET_PATTERNS if pattern.search(text)]


def assert_no_secrets(text: str) -> str:
    leaks = find_secret_leaks(text)
    if leaks:
        raise SecretLeakError(f"refusing to render output matching secret patterns: {leaks}")
    return text


def redact_secrets(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def refuse_funnel(tokens: list[str]) -> None:
    for token in tokens:
        if "funnel" in str(token).lower():
            raise FunnelRefusedError(
                "Tailscale Funnel is not allowed from this surface; "
                "tailnet-only Serve is the policy (see docs/conference1.md)"
            )


def magicdns_serve_urls(meta: dict[str, Any]) -> list[str]:
    return [f"http://{meta['magicdns']}:{port}" for port in meta["serve_ports"]]


def portproxy_fallback_urls(meta: dict[str, Any]) -> list[str]:
    listen = meta["portproxy_fallback"]["listen_address"]
    return [f"http://{listen}:{port}" for port in meta["serve_ports"]]


def serve_helper_remote_command(meta: dict[str, Any], action: str, port: int | None = None) -> str:
    refuse_funnel([action, "" if port is None else str(port)])
    allowed = tuple(meta.get("serve_helper_actions") or ())
    if action not in allowed:
        raise ValueError(f"unknown tailnet-serve action {action!r}; allowed: {', '.join(allowed)}")
    command = (
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {meta['serve_helper']} {action}"
    )
    if action in MUTATING_ACTIONS:
        if port is None:
            raise ValueError(f"tailnet-serve {action} requires a port")
        command += f" {int(port)}"
    return command


def windows_ssh_argv(meta: dict[str, Any], remote_command: str) -> list[str]:
    return ["ssh", *SSH_BASE_OPTS, meta["windows_ssh_target"], remote_command]


def serve_list_argv(meta: dict[str, Any]) -> list[str]:
    return windows_ssh_argv(meta, serve_helper_remote_command(meta, "list"))


def portproxy_list_argv(meta: dict[str, Any]) -> list[str]:
    return windows_ssh_argv(meta, meta["portproxy_fallback"]["list_command"])


def default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=45)


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _shell_join(argv: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(token) for token in argv)


def urls_payload(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "host": meta["host"],
        "role": meta["role"],
        "magicdns": meta["magicdns"],
        "tailscale_ip": meta["tailscale_ip"],
        "serve_backend_host": meta["serve_backend_host"],
        "magicdns_serve": {
            "kind": "primary",
            "urls": magicdns_serve_urls(meta),
        },
        "portproxy_fallback": {
            "kind": "fallback",
            "note": meta["portproxy_fallback"]["note"],
            "urls": portproxy_fallback_urls(meta),
        },
        "helper": {
            "windows_ssh_target": meta["windows_ssh_target"],
            "serve_helper": meta["serve_helper"],
            "list": _shell_join(serve_list_argv(meta)),
            "portproxy_list": _shell_join(portproxy_list_argv(meta)),
        },
        "swimmers_remote_rust": meta["swimmers_remote_rust"],
        "security": meta["security"],
        "next_actions": [
            "sbp conference1 status",
            "sbp conference1 helper",
        ],
    }


def render_urls_text(meta: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"conference1 tailnet ({meta['role']})")
    lines.append(
        f"host: {meta['host']}  magicdns: {meta['magicdns']}  "
        f"tailscale-ip: {meta['tailscale_ip']}  os: {meta['os']}"
    )
    lines.append("")
    lines.append("MagicDNS Serve URLs (primary; tailnet only):")
    for url in magicdns_serve_urls(meta):
        port = url.rsplit(":", 1)[-1]
        lines.append(f"  {url} -> {meta['serve_backend_host']}:{port}")
    lines.append("")
    lines.append("Raw-IP portproxy fallback (use only if MagicDNS Serve is down):")
    for url in portproxy_fallback_urls(meta):
        port = url.rsplit(":", 1)[-1]
        lines.append(f"  {url} -> {meta['serve_backend_host']}:{port}")
    lines.append("")
    rust = meta["swimmers_remote_rust"]
    lines.append("Swimmers remote Rust lane (heavy builds):")
    lines.append(f"  {rust['host_env']}={rust['host_values'][0]} (or {rust['host_values'][1]})")
    lines.append(f"  build side: {rust['build_side']} via {rust['wsl_ssh_target']}")
    lines.append(f"  cargo cache: {rust['cargo_cache']}")
    lines.append("")
    lines.append(f"Security posture: {meta['security']['posture']}")
    for rule in meta["security"]["rules"]:
        lines.append(f"  - {rule}")
    lines.append("")
    lines.append("next:")
    lines.append("  sbp conference1 status   live Serve + portproxy state over SSH (read-only)")
    lines.append("  sbp conference1 helper   exact tailnet-serve.ps1 commands")
    return "\n".join(lines)


def render_helper_text(meta: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("Conference1 tailnet-serve helper commands (nothing executed):")
    lines.append("")
    lines.append("read-only:")
    lines.append(f"  {_shell_join(serve_list_argv(meta))}")
    lines.append(f"  {_shell_join(portproxy_list_argv(meta))}")
    lines.append("")
    lines.append("mutating (require --yes; never use Funnel):")
    lines.append("  sbp conference1 expose <port> --yes")
    lines.append("  sbp conference1 remove <port> --yes")
    lines.append("")
    lines.append("underlying remote commands:")
    lines.append(f"  {serve_helper_remote_command(meta, 'list')}")
    lines.append(
        f"  {serve_helper_remote_command(meta, 'expose', 0).replace(' 0', ' <port>')}"
    )
    lines.append(
        f"  {serve_helper_remote_command(meta, 'remove', 0).replace(' 0', ' <port>')}"
    )
    return "\n".join(lines)


def _run_labeled(argv: list[str], runner: Runner) -> dict[str, Any]:
    try:
        proc = runner(argv)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return {"ok": False, "argv": argv, "error": str(exc)}
    return {
        "ok": proc.returncode == 0,
        "argv": argv,
        "returncode": proc.returncode,
        "stdout": redact_secrets((proc.stdout or "").strip()),
        "stderr": redact_secrets((proc.stderr or "").strip()),
    }


def status_payload(meta: dict[str, Any], runner: Runner | None = None) -> dict[str, Any]:
    runner = runner or default_runner
    serve = _run_labeled(serve_list_argv(meta), runner)
    portproxy = _run_labeled(portproxy_list_argv(meta), runner)
    ok = bool(serve.get("ok")) and bool(portproxy.get("ok"))
    payload: dict[str, Any] = {
        "ok": ok,
        "host": meta["host"],
        "magicdns": meta["magicdns"],
        "read_only": True,
        "magicdns_serve": {
            "kind": "primary",
            "expected_urls": magicdns_serve_urls(meta),
            "live": serve,
        },
        "portproxy_fallback": {
            "kind": "fallback",
            "expected_urls": portproxy_fallback_urls(meta),
            "live": portproxy,
        },
    }
    if not ok:
        payload["error"] = {
            "code": "CONFERENCE1_SSH_UNREACHABLE",
            "message": (
                f"could not read Serve/portproxy state via {meta['windows_ssh_target']}; "
                "Windows-side SSH may be down or unauthorized from this box"
            ),
            "next_actions": [
                "tailscale status | grep -i conference1",
                f"ssh -o BatchMode=yes -o ConnectTimeout=8 {meta['windows_ssh_target']} whoami",
                "check ~/.ssh/config Host conference1-ssh and its IdentityFile",
                "fallback (WSL side, no Serve control): "
                f"ssh {meta['wsl_ssh_target']} true",
            ],
        }
    return payload


def render_status_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"conference1 tailnet status (read-only) host={payload['host']}")
    lines.append("")
    serve = payload["magicdns_serve"]["live"]
    lines.append("[MagicDNS Serve URLs — primary — live via Windows helper]")
    if serve.get("ok"):
        lines.append(serve.get("stdout") or "(no output)")
    else:
        lines.append(f"  UNAVAILABLE rc={serve.get('returncode')} {serve.get('stderr') or serve.get('error') or ''}".rstrip())
    lines.append("")
    portproxy = payload["portproxy_fallback"]["live"]
    lines.append("[Raw Tailscale-IP portproxy — fallback only — live via netsh]")
    if portproxy.get("ok"):
        lines.append(portproxy.get("stdout") or "(no output)")
    else:
        lines.append(f"  UNAVAILABLE rc={portproxy.get('returncode')} {portproxy.get('stderr') or portproxy.get('error') or ''}".rstrip())
    error = payload.get("error")
    if error:
        lines.append("")
        lines.append(f"ERROR {error['code']}: {error['message']}")
        lines.append("next:")
        for step in error["next_actions"]:
            lines.append(f"  {step}")
    return "\n".join(lines)


def mutate_serve(
    meta: dict[str, Any],
    action: str,
    port: int,
    *,
    yes: bool,
    runner: Runner | None = None,
) -> dict[str, Any]:
    if action not in MUTATING_ACTIONS:
        raise ValueError(f"not a mutating action: {action!r}")
    argv = windows_ssh_argv(meta, serve_helper_remote_command(meta, action, port))
    if not yes:
        return {
            "ok": True,
            "action": action,
            "port": port,
            "dry_run": True,
            "executed": False,
            "command": _shell_join(argv),
            "next_actions": [f"sbp conference1 {action} {port} --yes"],
        }
    runner = runner or default_runner
    result = _run_labeled(argv, runner)
    result.update({"action": action, "port": port, "dry_run": False, "executed": True})
    return result


def _wants_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False)) or getattr(args, "format", "") == "json"


def _emit(payload: dict[str, Any], text: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(text)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sbp conference1",
        description="Conference1 tailnet Serve endpoints, helper commands, and heavy-build lane.",
    )
    parser.add_argument("--root", type=Path, default=None, help="Skillbox repo root")

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--json", action="store_true", help="JSON output")
        sub.add_argument("--format", default="", choices=["", "json", "text"], help="Output format")
        sub.add_argument("--root", type=Path, default=None, help="Skillbox repo root")

    subparsers = parser.add_subparsers(dest="command")
    add_common(subparsers.add_parser("urls", help="Offline endpoint + lane metadata (default)"))
    add_common(subparsers.add_parser("status", help="Live read-only Serve + portproxy state over SSH"))
    add_common(subparsers.add_parser("helper", help="Print exact tailnet-serve helper commands"))
    for action in MUTATING_ACTIONS:
        sub = subparsers.add_parser(action, help=f"tailnet-serve {action} <port> (requires --yes)")
        sub.add_argument("port", type=int)
        sub.add_argument("--yes", action="store_true", help="Actually run the mutation over SSH")
        add_common(sub)
    return parser


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    refuse_funnel(argv)
    known = {"urls", "status", "helper", *MUTATING_ACTIONS, "-h", "--help"}
    if not argv or (argv[0] not in known and argv[0].startswith("-")):
        argv.insert(0, "urls")
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = args.root or repo_root()
    meta = load_conference1_tailnet(root)
    as_json = _wants_json(args)

    command = args.command or "urls"
    if command == "urls":
        payload = urls_payload(meta)
        text = assert_no_secrets(render_urls_text(meta))
        _emit(payload, text, as_json)
        return 0
    if command == "helper":
        text = assert_no_secrets(render_helper_text(meta))
        _emit({"ok": True, "helper": urls_payload(meta)["helper"]}, text, as_json)
        return 0
    if command == "status":
        payload = status_payload(meta, runner=runner)
        _emit(payload, render_status_text(payload), as_json)
        return 0 if payload["ok"] else 1
    if command in MUTATING_ACTIONS:
        result = mutate_serve(meta, command, args.port, yes=args.yes, runner=runner)
        if as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
        elif result.get("dry_run"):
            print(f"conference1 {command} {args.port}: dry-run (pass --yes to apply)")
            print(f"would run: {result['command']}")
        else:
            print(result.get("stdout") or "")
            if result.get("stderr"):
                print(result["stderr"], file=sys.stderr)
        return 0 if result.get("ok") else 1
    parser.error(f"unknown command {command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
