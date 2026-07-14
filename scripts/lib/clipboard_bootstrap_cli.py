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
    apply_remote_restore_via_ssh,
    install_local,
    load_hosts,
    operator_platform_supported,
    plan_local_install,
    plan_remote_bootstrap,
    repo_root,
    resolve_profile,
    rollback_local,
    select_conference_route,
    static_conference_route,
    unsupported_operator_message,
    uninstall_local,
    verify_local_install,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Skillbox OSC52 clipboard helpers, tmux fragment, and xterm-ghostty terminfo.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="install",
        choices=("install", "uninstall", "rollback"),
        help="Lifecycle action (default: install)",
    )
    parser.add_argument("--root", type=Path, default=None, help="Skillbox repo root")
    parser.add_argument(
        "--profile",
        default="",
        help="Profile: local, d3, sweet, jeremy, conference1, conference1-fallback, generic",
    )
    parser.add_argument(
        "--target", default="", help="SSH target for generic profile or override"
    )
    parser.add_argument(
        "--dry-run", "--plan", action="store_true", dest="dry_run", help="Plan only"
    )
    parser.add_argument(
        "--apply-remote",
        action="store_true",
        help="Perform remote SSH install (default is plan)",
    )
    parser.add_argument(
        "--reload-current-tmux",
        action="store_true",
        help="source the managed config into the current local tmux server; affects every session on that server",
    )
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
            print(
                f"note: conference route={route.transport} target={route.ssh_target}",
                file=sys.stderr,
            )
        return key, route.ssh_target
    resolved = resolve_profile(profile, target=target, root=root)
    return key, resolved.get("ssh_target") or target or ""


def _remote_failure_class(stderr: bytes) -> str:
    message = stderr.decode("utf-8", errors="replace").lower()
    if "permission denied" in message or "authentication" in message:
        return "authentication_failed"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if "could not resolve" in message or "name or service not known" in message:
        return "target_unresolved"
    if "connection refused" in message or "no route to host" in message:
        return "target_unreachable"
    return "remote_command_failed"


def _resume_command(action: str, profile: str, target: str | None) -> str:
    parts = ["scripts/clipboard-bootstrap"]
    if action != "install":
        parts.append(action)
    parts.extend(("--profile", profile))
    if target:
        parts.extend(("--target", target))
    parts.append("--apply-remote")
    return " ".join(parts)


def _report_remote_failure(
    proc: object,
    *,
    action: str,
    profile: str,
    target: str | None,
) -> int:
    returncode = int(getattr(proc, "returncode"))
    stderr = bytes(getattr(proc, "stderr", b""))
    print(
        "clipboard-bootstrap: "
        f"remote {action} failed class={_remote_failure_class(stderr)} "
        f"exit={returncode}",
        file=sys.stderr,
    )
    if action == "install":
        print(
            "clipboard-bootstrap: local prerequisite remains installed; "
            "remote state may be partial",
            file=sys.stderr,
        )
    else:
        print(
            "clipboard-bootstrap: local reversal was not started; "
            "remote state may be partial",
            file=sys.stderr,
        )
    print(
        "clipboard-bootstrap: resume: " + _resume_command(action, profile, target),
        file=sys.stderr,
    )
    return returncode


def _apply_remote(root: Path, profile: str, target: str | None) -> int:
    _profile_key, ssh_target = _resolve_remote_target(
        profile, target, root, live_probe=True
    )
    if not ssh_target:
        remote_plan = plan_remote_bootstrap(
            profile, target=target, dry_run=False, root=root, live_probe=True
        )
        ssh_target = remote_plan.ssh_target
    if not ssh_target:
        print("clipboard-bootstrap: remote profile missing ssh_target", file=sys.stderr)
        return 2

    resolved_apply = resolve_profile(
        _profile_key, target=target if _profile_key == "generic" else None, root=root
    )
    transport = resolved_apply.get("transport", "ssh")
    proc = apply_remote_via_ssh(ssh_target, root=root, transport=transport)
    if proc.returncode != 0:
        return _report_remote_failure(
            proc, action="install", profile=profile, target=target
        )
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
    writes_install = args.action == "install" and (
        profile == "local" or args.apply_remote
    )
    if writes_install and not args.dry_run and not operator_platform_supported():
        print(f"clipboard-bootstrap: {unsupported_operator_message()}", file=sys.stderr)
        return 2

    if args.action == "uninstall":
        if profile != "local":
            if not args.apply_remote:
                print(
                    "clipboard-bootstrap: remote uninstall requires --apply-remote",
                    file=sys.stderr,
                )
                return 2
            profile_key, ssh_target = _resolve_remote_target(
                profile, target, root, live_probe=True
            )
            resolved = resolve_profile(
                profile_key,
                target=target if profile_key == "generic" else None,
                root=root,
            )
            proc = apply_remote_restore_via_ssh(
                ssh_target, transport=resolved.get("transport", "ssh")
            )
            if proc.returncode != 0:
                return _report_remote_failure(
                    proc, action="uninstall", profile=profile, target=target
                )
            print(proc.stdout.decode("utf-8", errors="replace"), end="")
        result = uninstall_local(Path.home())
        print(
            f"clipboard-bootstrap: uninstall ok changed={str(result['changed']).lower()}"
        )
        return 0
    if args.action == "rollback":
        if profile != "local":
            if not args.apply_remote:
                print(
                    "clipboard-bootstrap: remote rollback requires --apply-remote",
                    file=sys.stderr,
                )
                return 2
            profile_key, ssh_target = _resolve_remote_target(
                profile, target, root, live_probe=True
            )
            resolved = resolve_profile(
                profile_key,
                target=target if profile_key == "generic" else None,
                root=root,
            )
            proc = apply_remote_restore_via_ssh(
                ssh_target, rollback=True, transport=resolved.get("transport", "ssh")
            )
            if proc.returncode != 0:
                return _report_remote_failure(
                    proc, action="rollback", profile=profile, target=target
                )
            print(proc.stdout.decode("utf-8", errors="replace"), end="")
        try:
            result = rollback_local(Path.home())
        except FileNotFoundError as exc:
            print(f"clipboard-bootstrap: {exc}", file=sys.stderr)
            return 1
        print(f"clipboard-bootstrap: rollback ok version={result['restored_version']}")
        return 0

    if profile == "local":
        plan = install_local(
            dry_run=args.dry_run,
            root=root,
            reload_current_tmux=args.reload_current_tmux,
        )
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

    profile_key, routed_target = _resolve_remote_target(
        profile, target, root, live_probe=live_probe
    )
    resolved = resolve_profile(
        profile_key, target=target if profile_key == "generic" else None, root=root
    )
    remote_plan = plan_remote_bootstrap(
        profile,
        target=target,
        dry_run=args.dry_run,
        root=root,
        ssh_target_override=routed_target if profile == "conference1" else None,
        live_probe=live_probe,
    )
    if args.dry_run:
        mode = "dry-run"
    elif args.apply_remote:
        mode = "apply"
    else:
        mode = "plan"
    print(
        f"clipboard-bootstrap: profile={resolved['profile']} target={remote_plan.ssh_target} ({mode})"
    )
    for step in remote_plan.steps:
        print(f"  - {step}")

    if profile == "conference1":
        route = (
            static_conference_route(root=root)
            if not live_probe
            else select_conference_route(root=root)
        )
        print(
            f"  - conference route: {route.transport} -> {route.ssh_target} ({route.reason})"
        )

    if args.dry_run or not args.apply_remote:
        if not args.dry_run:
            print("note: remote writes require --apply-remote")
        return 0

    local_plan = install_local(
        dry_run=False,
        root=root,
        reload_current_tmux=args.reload_current_tmux,
    )
    print("clipboard-bootstrap: local prerequisite installed")
    for step in local_plan.steps:
        print(f"  - {step}")
    local_issues = verify_local_install(Path.home())
    if local_issues:
        for issue in local_issues:
            print(f"warning: {issue}", file=sys.stderr)
        return 1
    return _apply_remote(root, profile, target)


if __name__ == "__main__":
    raise SystemExit(main())
