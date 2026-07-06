#!/usr/bin/env python3
"""CLI for Skillbox clipboard bootstrap."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib.clipboard_bootstrap import (
    apply_remote_via_ssh,
    install_local,
    load_hosts,
    plan_local_install,
    plan_remote_bootstrap,
    repo_root,
    resolve_profile,
    select_conference_route,
    static_conference_route,
    verify_local_install,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Skillbox OSC52 clipboard helpers, tmux fragment, and xterm-ghostty terminfo.",
    )
    parser.add_argument("--root", type=Path, default=None, help="Skillbox repo root")
    parser.add_argument(
        "--profile",
        default="",
        help="Profile: local, d3, sweet, jeremy, conference1, conference1-fallback, generic",
    )
    parser.add_argument("--target", default="", help="SSH target for generic profile or override")
    parser.add_argument("--dry-run", "--plan", action="store_true", dest="dry_run", help="Plan only")
    parser.add_argument("--apply-remote", action="store_true", help="Perform remote SSH install (default is plan)")
    return parser


def _print_help_profiles(root: Path) -> None:
    hosts = load_hosts(root)
    profiles = sorted(hosts["profiles"])
    print("Supported profiles:", ", ".join(profiles))
    print("")
    local = plan_local_install(dry_run=True, root=root)
    print("Local steps:")
    for step in local.steps:
        print(f"  - {step}")
    print("")
    for name in ("d3", "sweet", "jeremy", "conference1"):
        remote = plan_remote_bootstrap(name, dry_run=True, root=root, live_probe=False)
        print(f"Remote profile {name} ({remote.ssh_target}):")
        for step in remote.steps:
            print(f"  - {step}")
        print("")


def _effective_profile(profile: str, target: str | None) -> str:
    if profile:
        return profile
    if target:
        return "generic"
    return "local"


def _resolve_remote_target(
    profile: str,
    target: str | None,
    root: Path,
    *,
    live_probe: bool,
) -> tuple[str, str]:
    """Return (profile_key, ssh_target) after conference routing."""
    key = profile
    if profile == "conference1":
        route = (
            select_conference_route(root=root, live_probe=live_probe)
            if live_probe
            else static_conference_route(root=root)
        )
        if route.used_fallback:
            key = "conference1-fallback"
            if live_probe:
                print(
                    f"note: direct WSL unreachable; routing to fallback {route.ssh_target} (OSC52-hostile)",
                    file=sys.stderr,
                )
        elif live_probe:
            print(f"note: conference route={route.transport} target={route.ssh_target}", file=sys.stderr)
        return key, route.ssh_target
    resolved = resolve_profile(profile, target=target, root=root)
    return key, resolved.get("ssh_target") or target or ""


def _apply_remote(root: Path, profile: str, target: str | None) -> int:
    _profile_key, ssh_target = _resolve_remote_target(profile, target, root, live_probe=True)
    if not ssh_target:
        remote_plan = plan_remote_bootstrap(profile, target=target, dry_run=False, root=root, live_probe=True)
        ssh_target = remote_plan.ssh_target
    if not ssh_target:
        print("clipboard-bootstrap: remote profile missing ssh_target", file=sys.stderr)
        return 2

    resolved_apply = resolve_profile(_profile_key, target=target if _profile_key == "generic" else None, root=root)
    transport = resolved_apply.get("transport", "ssh")
    proc = apply_remote_via_ssh(ssh_target, root=root, transport=transport)
    if proc.returncode != 0:
        print(proc.stdout.decode("utf-8", errors="replace"), end="")
        print(proc.stderr.decode("utf-8", errors="replace"), end="", file=sys.stderr)
        return proc.returncode
    print(proc.stdout.decode("utf-8", errors="replace"), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        root = repo_root()
        for index, arg in enumerate(argv):
            if arg == "--root" and index + 1 < len(argv):
                root = Path(argv[index + 1])
        _print_help_profiles(root)
        return 0

    parser = _build_parser()
    args = parser.parse_args(argv)
    root = args.root or repo_root()
    profile = (args.profile or "").strip().lower()
    target = (args.target or "").strip() or None
    profile = _effective_profile(profile, target)
    live_probe = not args.dry_run and args.apply_remote

    if profile == "local":
        plan = install_local(dry_run=args.dry_run, root=root)
        mode = "dry-run" if args.dry_run else "apply"
        print(f"clipboard-bootstrap: local profile ({mode})")
        for step in plan.steps:
            print(f"  - {step}")
        if not args.dry_run:
            issues = verify_local_install(Path.home())
            if issues:
                for issue in issues:
                    print(f"warning: {issue}", file=sys.stderr)
                return 1
        return 0

    profile_key, routed_target = _resolve_remote_target(profile, target, root, live_probe=live_probe)
    resolved = resolve_profile(profile_key, target=target if profile_key == "generic" else None, root=root)
    remote_plan = plan_remote_bootstrap(
        profile,
        target=target,
        dry_run=args.dry_run,
        root=root,
        ssh_target_override=routed_target if profile == "conference1" else None,
        live_probe=live_probe,
    )
    mode = "dry-run" if args.dry_run else "apply"
    print(f"clipboard-bootstrap: profile={resolved['profile']} target={remote_plan.ssh_target} ({mode})")
    for step in remote_plan.steps:
        print(f"  - {step}")

    if profile == "conference1":
        route = static_conference_route(root=root) if not live_probe else select_conference_route(root=root)
        print(f"  - conference route: {route.transport} -> {route.ssh_target} ({route.reason})")

    if args.dry_run or not args.apply_remote:
        if not args.dry_run:
            print("note: remote writes require --apply-remote")
        return 0

    return _apply_remote(root, profile, target)


if __name__ == "__main__":
    raise SystemExit(main())