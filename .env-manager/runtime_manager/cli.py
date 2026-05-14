from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any, Callable

from .shared import *
from .validation import *
from .publish import *
from .runtime_ops import *
from .skill_visibility import *
from .mmdx_open import *
from .operator_booking import *
from .context_rendering import *
from .text_renderers import *
from .workflows import *
from .mcp_visibility import *
from .parity_report import *
from .swimmers_launch import launch_swimmers_batch, swimmers_launch_text_lines


class DistributionPreviewError(RuntimeError):
    pass


class DistributionRollbackError(RuntimeError):
    pass


MANAGE_COMMAND_NAMES = {
    "acceptance",
    "bootstrap",
    "capabilities",
    "client-diff",
    "client-init",
    "client-open",
    "client-project",
    "client-publish",
    "context",
    "distribution-preview",
    "distribution-publish",
    "distribution-rollback",
    "doctor",
    "down",
    "first-box",
    "focus",
    "logs",
    "mcp-audit",
    "mmdx",
    "onboard",
    "operator-booking",
    "overlay",
    "parity-report",
    "private-init",
    "render",
    "restart",
    "robot-docs",
    "robot-triage",
    "session-end",
    "session-event",
    "session-resume",
    "session-start",
    "session-status",
    "skill",
    "skill-audit",
    "skills",
    "status",
    "stewardship-report",
    "swimmers-launch",
    "sync",
    "up",
    "worker-artifacts",
    "worker-promote-learning",
    "worker-status",
    "worker-submit",
}
JSON_FLAG_ALIASES = {
    "--json": "--format json",
    "--jason": "--format json",
    "--jsno": "--format json",
    "--jsson": "--format json",
}


class SkillboxArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        command_hint = _command_suggestion_from_error(message)
        hint_lines = [
            "",
            "Agent hint: run `manage.py capabilities --json` for the machine-readable command contract.",
        ]
        if command_hint:
            hint_lines.append(f"Did you mean: `manage.py {command_hint}`?")
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n" + "\n".join(hint_lines) + "\n")


def _command_suggestion_from_error(message: str) -> str:
    marker = "invalid choice: "
    if marker not in message:
        return ""
    remainder = message.split(marker, 1)[1]
    raw = remainder.split("(", 1)[0].strip().strip("'\"")
    matches = difflib.get_close_matches(raw, sorted(MANAGE_COMMAND_NAMES), n=1, cutoff=0.68)
    return matches[0] if matches else ""


def _normalize_agent_argv(argv: list[str] | None) -> tuple[list[str], list[str]]:
    raw = list(sys.argv[1:] if argv is None else argv)
    normalized: list[str] = []
    diagnostics: list[str] = []
    command_seen = False
    pending_json = False

    for token in raw:
        if token == "--robot-help":
            normalized.extend(["robot-docs", "guide"])
            command_seen = True
            continue
        if token in JSON_FLAG_ALIASES:
            if token != "--json":
                diagnostics.append(
                    f"Interpreting {token} as --format json. Exact command: manage.py <command> --format json"
                )
            if command_seen:
                normalized.extend(["--format", "json"])
            else:
                pending_json = True
            continue
        normalized.append(token)
        if not command_seen and token in MANAGE_COMMAND_NAMES:
            command_seen = True
            if pending_json:
                normalized.extend(["--format", "json"])
                pending_json = False

    if pending_json:
        normalized.extend(["status", "--format", "json"])
    return normalized, diagnostics


def publish_skill_release(**kwargs: Any) -> dict[str, Any]:
    try:
        from .distribution.publish import publish_skill_release as publish
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("cryptography"):
            raise RuntimeError(
                "distribution-publish requires the optional 'cryptography' package"
            ) from exc
        raise

    return publish(**kwargs)


def preview_manifest(**kwargs: Any) -> dict[str, Any]:
    try:
        from .distribution.preview import preview_manifest as preview
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("cryptography"):
            raise RuntimeError(
                "distribution-preview requires the optional 'cryptography' package"
            ) from exc
        raise

    return preview(**kwargs)


def cached_versions(**kwargs: Any) -> list[int]:
    state_root = Path(kwargs["state_root"])
    skill_name = str(kwargs["skill_name"])
    cache_dir = state_root / "bundle-cache" / skill_name
    if not cache_dir.is_dir():
        return []
    prefix = f"{skill_name}-v"
    suffix = ".skillbundle.tar.gz"
    versions: list[int] = []
    for path in cache_dir.iterdir():
        name = path.name
        if not path.is_file() or not name.startswith(prefix) or not name.endswith(suffix):
            continue
        try:
            versions.append(int(name[len(prefix):-len(suffix)]))
        except ValueError:
            continue
    return sorted(set(versions))


def rollback_distributor_skill(**kwargs: Any) -> dict[str, Any]:
    try:
        from .distribution.rollback import rollback_distributor_skill as rollback
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("cryptography"):
            raise RuntimeError(
                "distribution-rollback requires the optional 'cryptography' package"
            ) from exc
        raise

    return rollback(**kwargs)


def _add_profile_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="Activate a runtime profile. Can be repeated. Selecting any profile also includes `core`.",
    )


def _add_client_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--client",
        action="append",
        default=[],
        help="Activate a runtime client overlay. Can be repeated.",
    )


def _add_cwd_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used to infer the active client overlay when --client is omitted.",
    )


def _add_service_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--service",
        action="append",
        default=[],
        help="Limit the command to one or more declared service ids. Can be repeated.",
    )


def _add_task_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Limit the command to one or more declared task ids. Can be repeated.",
    )


def _add_context_dir_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--context-dir",
        default=None,
        help=(
            "Write CLAUDE.md and AGENTS.md into this directory instead of the mounted "
            "home/.claude and home/.codex roots. Path is resolved relative to the repo root."
        ),
    )


def _add_skill_lifecycle_common(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument("--format", choices=("text", "json"), default="text")
    command_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used to infer client overlays, repo roots, and project categories.",
    )
    command_parser.add_argument(
        "--to",
        choices=("auto", "global", "project", "category"),
        default="auto",
        help="Where to install/link the skill. auto uses skill-scope policy.",
    )
    command_parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Project category to target. Can be repeated.",
    )
    command_parser.add_argument(
        "--source",
        default=None,
        help="Explicit skill directory or parent source directory.",
    )
    command_parser.add_argument("--dry-run", action="store_true")
    command_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing non-symlink files and override global policy blocks.",
    )
    command_parser.add_argument(
        "--allow-directories",
        action="store_true",
        help="Allow remove/prune/move to delete real skill directories, not just symlinks/files.",
    )
    command_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm remove/prune/move actions that unlink existing installs.",
    )
    _add_profile_arg(command_parser)
    _add_client_arg(command_parser)


def _add_skill_from_scope_arg(command_parser: argparse.ArgumentParser, *, help_text: str) -> None:
    command_parser.add_argument(
        "--from",
        dest="from_scope",
        choices=("global", "project", "all"),
        default="all",
        help=help_text,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = SkillboxArgumentParser(description="Manage the internal skillbox runtime graph.")
    parser.add_argument(
        "--root-dir",
        default=None,
        help="Override the repo root for testing or embedding.",
    )
    parser.add_argument(
        "--robot-triage",
        action="store_true",
        help="Emit a compact JSON triage packet for agents and exit.",
    )
    subparsers = parser.add_subparsers(dest="command")

    capabilities_parser = subparsers.add_parser(
        "capabilities",
        help="Print the machine-readable Skillbox CLI contract.",
    )
    capabilities_parser.add_argument("--format", choices=("json",), default="json")

    robot_docs_parser = subparsers.add_parser(
        "robot-docs",
        help="Print agent-oriented in-tool documentation.",
    )
    robot_docs_parser.add_argument("topic", nargs="?", default="guide", choices=("guide",))
    robot_docs_parser.add_argument("--format", choices=("text", "json"), default="text")

    robot_triage_parser = subparsers.add_parser(
        "robot-triage",
        help="Emit a compact JSON triage packet for agents.",
    )
    robot_triage_parser.add_argument("--format", choices=("json",), default="json")

    render_parser = subparsers.add_parser("render", help="Print the resolved runtime graph.")
    render_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(render_parser)
    _add_client_arg(render_parser)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Create managed runtime directories, repos, artifacts, and installed skill state.",
    )
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(sync_parser)
    _add_client_arg(sync_parser)
    _add_context_dir_arg(sync_parser)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate runtime graph, filesystem readiness, and installed skill integrity.",
    )
    doctor_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(doctor_parser)
    _add_client_arg(doctor_parser)

    status_parser = subparsers.add_parser(
        "status",
        help="Summarize repo, artifact, skill, service, log, and check state.",
    )
    status_parser.add_argument("--format", choices=("text", "json"), default="text")
    status_parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON for agent inspection instead of the full raw status payload.",
    )
    _add_profile_arg(status_parser)
    _add_client_arg(status_parser)
    _add_cwd_arg(status_parser)

    skills_parser = subparsers.add_parser(
        "skills",
        help="Show effective skill availability across global, client, and project layers.",
    )
    skills_parser.add_argument("--format", choices=("text", "json"), default="text")
    skills_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used for pwd-match and project-local skill discovery. Defaults to the current cwd.",
    )
    skills_parser.add_argument(
        "--full",
        action="store_true",
        help="Show every effective skill in text output and full raw JSON instead of compact JSON.",
    )
    skills_parser.add_argument(
        "--show-shadowed",
        action="store_true",
        help="Show lower-precedence skill declarations hidden by the effective layer.",
    )
    skills_parser.add_argument(
        "--show-sources",
        action="store_true",
        help="Scan configured source roots for skills that are not currently synced. This can be noisy.",
    )
    skills_parser.add_argument(
        "--issues-only",
        action="store_true",
        help="Show compact policy issues and recommendations instead of the effective skill list.",
    )
    skills_parser.add_argument(
        "--limit",
        type=int,
        default=80,
        help="Maximum effective skills to show in text output unless --full is set.",
    )
    skills_parser.add_argument(
        "--no-global",
        action="store_true",
        help="Do not inspect ~/.claude/skills or ~/.codex/skills.",
    )
    skills_parser.add_argument(
        "--no-project",
        action="store_true",
        help="Do not inspect project-local .claude/.codex skill directories near --cwd.",
    )
    _add_profile_arg(skills_parser)
    _add_client_arg(skills_parser)

    skill_audit_parser = subparsers.add_parser(
        "skill-audit",
        help="Audit skill scope policy across configured downstream repos.",
    )
    skill_audit_parser.add_argument("--format", choices=("text", "json"), default="text")
    skill_audit_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used for the one-time global drift summary. Defaults to the current cwd.",
    )
    skill_audit_parser.add_argument(
        "--scan-root",
        action="append",
        default=None,
        help="Root to scan for git repos. Can be repeated. Defaults to skill_install_scan_roots from skill-scope.yaml.",
    )
    skill_audit_parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Maximum directory depth under each scan root when finding git repos.",
    )
    skill_audit_parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximum repo rows and names to show in text output.",
    )
    skill_audit_parser.add_argument(
        "--all",
        action="store_true",
        help="Report clean repos as well as repos with skill policy issues.",
    )
    skill_audit_parser.add_argument(
        "--no-global",
        action="store_true",
        help="Skip the one-time global skill drift summary.",
    )
    _add_profile_arg(skill_audit_parser)
    _add_client_arg(skill_audit_parser)

    mcp_audit_parser = subparsers.add_parser(
        "mcp-audit",
        help="Audit Claude JSON and Codex TOML MCP config parity for a repo.",
    )
    mcp_audit_parser.add_argument("--format", choices=("text", "json"), default="text")
    mcp_audit_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used to find the target repo root. Defaults to the current cwd.",
    )
    mcp_audit_parser.add_argument(
        "--config-root",
        default=None,
        help="Explicit repo/config root containing .mcp.json and .codex/config.toml.",
    )
    _add_profile_arg(mcp_audit_parser)
    _add_client_arg(mcp_audit_parser)

    mmdx_parser = subparsers.add_parser(
        "mmdx",
        help="Fuzzy-find and open Mermaid/MMDX files from the current repo.",
    )
    mmdx_parser.add_argument(
        "query",
        nargs="*",
        help="File path, stem, or fuzzy path fragment. Omit to list recent diagrams.",
    )
    mmdx_parser.add_argument("--format", choices=("text", "json"), default="text")
    mmdx_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used for relative paths and default repo-root discovery. Defaults to $PWD.",
    )
    mmdx_parser.add_argument(
        "--search-root",
        action="append",
        default=[],
        help="Directory to scan for .mmdx/.mmd files. Can be repeated.",
    )
    mmdx_parser.add_argument("--limit", type=int, default=MMDX_DEFAULT_LIMIT)
    open_group = mmdx_parser.add_mutually_exclusive_group()
    open_group.add_argument("--open", dest="open", action="store_true", default=True)
    open_group.add_argument("--no-open", dest="open", action="store_false")
    mmdx_parser.add_argument(
        "--tmux",
        action="store_true",
        help="Open with the MMDX skill's local tmux handoff bridge.",
    )
    mmdx_parser.add_argument(
        "--tmux-submit",
        action="store_true",
        help="With --tmux, press Enter after the viewer sends a handoff packet.",
    )
    mmdx_parser.add_argument(
        "--allow-parser-install",
        action="store_true",
        help="Allow the MMDX script to install its Mermaid parser dependency if missing.",
    )
    _add_profile_arg(mmdx_parser)
    _add_client_arg(mmdx_parser)

    swimmers_launch_parser = subparsers.add_parser(
        "swimmers-launch",
        help="Launch one Swimmers agent session per directory through /v1/sessions/batch.",
    )
    swimmers_launch_parser.add_argument("dirs", nargs="*", help="Directories to launch agents in.")
    swimmers_launch_parser.add_argument(
        "--dir",
        dest="dir_flags",
        action="append",
        default=[],
        help="Directory to launch. Can be repeated.",
    )
    swimmers_launch_parser.add_argument(
        "--cwd",
        dest="cwd_flags",
        action="append",
        default=[],
        help="Directory to launch. Alias for --dir for agent command consistency.",
    )
    swimmers_launch_parser.add_argument(
        "--dirs-file",
        default=None,
        help="File containing one launch directory per line.",
    )
    swimmers_launch_parser.add_argument(
        "--group",
        default=None,
        help="Resolve launch directories from a Swimmers /v1/dirs group.",
    )
    swimmers_launch_parser.add_argument(
        "--path",
        dest="group_path",
        default=None,
        help="Optional /v1/dirs path used with --group.",
    )
    swimmers_launch_parser.add_argument(
        "--managed-only",
        action="store_true",
        help="With --group, ask /v1/dirs for managed entries only.",
    )
    swimmers_launch_parser.add_argument(
        "--request",
        "--prompt",
        dest="request",
        default=None,
        help="Initial request sent to every launched agent.",
    )
    swimmers_launch_parser.add_argument(
        "--request-file",
        "--prompt-file",
        dest="request_file",
        default=None,
        help="Read the initial request from a file.",
    )
    swimmers_launch_parser.add_argument("--tool", choices=("codex", "claude"), default="codex")
    swimmers_launch_parser.add_argument(
        "--target",
        default=None,
        help="Optional Swimmers launch target id. Defaults to local.",
    )
    swimmers_launch_parser.add_argument(
        "--base-url",
        default=None,
        help="Swimmers API base URL. Defaults to SWIMMERS_URL, SWIMMERS_TUI_URL, or http://127.0.0.1:3210.",
    )
    swimmers_launch_parser.add_argument(
        "--auth-token-env",
        default=None,
        help="Environment variable containing the bearer token for token-auth Swimmers APIs.",
    )
    swimmers_launch_parser.add_argument("--timeout", type=float, default=30.0)
    swimmers_launch_parser.add_argument("--dry-run", action="store_true")
    swimmers_launch_parser.add_argument(
        "--invoke-cwd",
        default=None,
        help=argparse.SUPPRESS,
    )
    swimmers_launch_parser.add_argument("--format", choices=("text", "json"), default="text")

    operator_booking_parser = subparsers.add_parser(
        "operator-booking",
        help="Fetch human-operator availability or create an x402 booking hold from client config.",
    )
    operator_booking_parser.add_argument(
        "action",
        nargs="?",
        default="availability",
        choices=("availability", "times", "list", "config", "book"),
    )
    operator_booking_parser.add_argument("--format", choices=("text", "json"), default="text")
    operator_booking_parser.add_argument("--date", default=None, help="Booking date in YYYY-MM-DD format.")
    operator_booking_parser.add_argument("--slot", default=None, help="Slot type to book, e.g. AM or PM.")
    operator_booking_parser.add_argument("--email", default=None, help="Client email for booking/account email.")
    operator_booking_parser.add_argument("--name", default=None, help="Client display name for the booking.")
    operator_booking_parser.add_argument(
        "--redirect-url",
        default=None,
        help="Optional magic-link redirect URL for account sign-in.",
    )
    operator_booking_parser.add_argument(
        "--origin",
        default=None,
        help="Optional Origin header override for browser-style publishable-key requests.",
    )
    operator_booking_parser.add_argument(
        "--access-token-env",
        default=None,
        help="Optional env var containing a verified user JWT; binds bookings to that account.",
    )
    operator_booking_parser.add_argument(
        "--send-magic-link",
        action="store_true",
        help="Request a passwordless account email before creating the x402 hold.",
    )
    operator_booking_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For book, print the planned SPAPS requests without sending them.",
    )
    operator_booking_parser.add_argument("--limit", type=int, default=8)
    _add_profile_arg(operator_booking_parser)
    _add_client_arg(operator_booking_parser)
    _add_cwd_arg(operator_booking_parser)

    skill_parser = subparsers.add_parser(
        "skill",
        help="Plan and apply skill installs, moves, removals, and policy syncs.",
    )
    skill_subparsers = skill_parser.add_subparsers(dest="skill_action", required=True)

    for action_name, help_text in (
        ("plan", "Preview where a skill would be installed."),
        ("add", "Install/link a skill into the selected global, project, or category scope."),
        ("activate", "Install/link a skill and print an activation packet for the current session."),
        ("move", "Install/link a skill into a new scope and remove old installs for that skill."),
    ):
        action_parser = skill_subparsers.add_parser(action_name, help=help_text)
        action_parser.add_argument("skill_name")
        _add_skill_lifecycle_common(action_parser)

    remove_parser = skill_subparsers.add_parser("remove", help="Remove installed links/files for a skill.")
    remove_parser.add_argument("skill_name")
    _add_skill_lifecycle_common(remove_parser)
    _add_skill_from_scope_arg(remove_parser, help_text="Installed scope to remove from.")

    prune_parser = skill_subparsers.add_parser("prune", help="Remove installed skills that violate skill-scope policy.")
    _add_skill_lifecycle_common(prune_parser)
    _add_skill_from_scope_arg(
        prune_parser,
        help_text="Installed scope to prune. Use project for repo-local cleanup without touching global skills.",
    )

    sync_skills_parser = skill_subparsers.add_parser(
        "sync",
        help="Install/link a named skill or all literal skills missing for the current cwd policy.",
    )
    sync_skills_parser.add_argument("skill_name", nargs="?")
    _add_skill_lifecycle_common(sync_skills_parser)
    sync_skills_parser.add_argument(
        "--prune",
        action="store_true",
        help="Also unlink existing policy violations after installing missing skills.",
    )
    _add_skill_from_scope_arg(
        sync_skills_parser,
        help_text="Installed scope to prune when --prune is set.",
    )

    overlay_parser = subparsers.add_parser(
        "overlay",
        help="List, enable, disable, toggle, or activate skill scope overlays (e.g. marketing).",
    )
    overlay_parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=("list", "on", "off", "toggle", "activate"),
        help="list (default), on, off, toggle, or activate.",
    )
    overlay_parser.add_argument("name", nargs="?", help="Overlay name, e.g. marketing.")
    overlay_parser.add_argument("--cwd", default=None, help="Target cwd for scoped unlinks. Defaults to $PWD.")
    overlay_parser.add_argument(
        "--keep",
        action="store_true",
        help="When turning an overlay off, keep existing symlinks. Default is to unlink overlay-scoped symlinks for --scope.",
    )
    overlay_parser.add_argument(
        "--to",
        choices=("project", "global", "category", "auto"),
        default="project",
        help="Activation destination. Defaults to project so hot overlays stay scoped to --cwd.",
    )
    overlay_parser.add_argument(
        "--scope",
        choices=("project", "global", "all"),
        default="project",
        help="Symlink removal scope for off/toggle. Defaults to project.",
    )
    overlay_parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Project category to target when --to category. Can be repeated.",
    )
    overlay_parser.add_argument(
        "--source",
        default=None,
        help="Explicit skill directory or parent source directory for activation.",
    )
    overlay_parser.add_argument("--dry-run", action="store_true")
    overlay_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing non-symlink files and override global policy blocks.",
    )
    overlay_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(overlay_parser)
    _add_client_arg(overlay_parser)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Sync runtime state and run one-shot bootstrap tasks for the active scope.",
    )
    bootstrap_parser.add_argument("--dry-run", action="store_true")
    bootstrap_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(bootstrap_parser)
    _add_client_arg(bootstrap_parser)
    _add_cwd_arg(bootstrap_parser)
    _add_task_arg(bootstrap_parser)

    up_parser = subparsers.add_parser(
        "up",
        help="Sync runtime state and start manageable services for the active scope.",
    )
    up_parser.add_argument("--dry-run", action="store_true")
    up_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS)
    up_parser.add_argument("--format", choices=("text", "json"), default="text")
    # --mode is the first-class startup-behavior selector introduced by
    # local_runtime_core_cutover (shared.md:428-469). It is orthogonal to
    # --profile (which answers "which graph?") per Business Rule 2. We do
    # NOT constrain choices at argparse level here because an unknown value
    # must surface as a structured LOCAL_RUNTIME_MODE_UNSUPPORTED envelope
    # rather than an argparse usage error. Validation happens immediately
    # after parse_args() and BEFORE any mutation.
    up_parser.add_argument(
        "--mode",
        default=None,
        help="Local runtime startup behavior. One of: reuse (default), prod, fresh.",
    )
    _add_profile_arg(up_parser)
    _add_client_arg(up_parser)
    _add_cwd_arg(up_parser)
    _add_service_arg(up_parser)

    down_parser = subparsers.add_parser(
        "down",
        help="Stop manageable services for the active scope.",
    )
    down_parser.add_argument("--dry-run", action="store_true")
    down_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_STOP_WAIT_SECONDS)
    down_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(down_parser)
    _add_client_arg(down_parser)
    _add_cwd_arg(down_parser)
    _add_service_arg(down_parser)

    restart_parser = subparsers.add_parser(
        "restart",
        help="Restart manageable services for the active scope.",
    )
    restart_parser.add_argument("--dry-run", action="store_true")
    restart_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS)
    restart_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(restart_parser)
    _add_client_arg(restart_parser)
    _add_cwd_arg(restart_parser)
    _add_service_arg(restart_parser)

    logs_parser = subparsers.add_parser(
        "logs",
        help="Show recent logs for declared services in the active scope.",
    )
    logs_parser.add_argument("--lines", type=int, default=DEFAULT_LOG_TAIL_LINES)
    logs_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(logs_parser)
    _add_client_arg(logs_parser)
    _add_cwd_arg(logs_parser)
    _add_service_arg(logs_parser)

    context_parser = subparsers.add_parser(
        "context",
        help="Generate CLAUDE.md and AGENTS.md from the resolved runtime graph.",
    )
    context_parser.add_argument("--dry-run", action="store_true")
    context_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(context_parser)
    _add_client_arg(context_parser)
    _add_context_dir_arg(context_parser)

    client_init_parser = subparsers.add_parser(
        "client-init",
        help="Scaffold a new workspace client overlay and companion skill directories.",
    )
    client_init_parser.add_argument(
        "client_id",
        nargs="?",
        help="Lowercase client slug, for example `acme-studio`.",
    )
    client_init_parser.add_argument("--label", default=None, help="Human-friendly label for the client.")
    client_init_parser.add_argument(
        "--root-path",
        default=None,
        help="Runtime path for the client root. Defaults to ${SKILLBOX_MONOSERVER_ROOT}.",
    )
    client_init_parser.add_argument(
        "--default-cwd",
        default=None,
        help="Runtime default cwd for the client. Defaults to the client root path.",
    )
    client_init_parser.add_argument(
        "--blueprint",
        default=None,
        help="Apply a reusable client blueprint from workspace/client-blueprints/ or an explicit YAML path.",
    )
    client_init_parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Set a blueprint variable using KEY=VALUE. Can be repeated.",
    )
    client_init_parser.add_argument(
        "--list-blueprints",
        action="store_true",
        help="List discoverable client blueprints and their variables.",
    )
    client_init_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files.")
    client_init_parser.add_argument("--dry-run", action="store_true")
    client_init_parser.add_argument("--format", choices=("text", "json"), default="text")

    private_init_parser = subparsers.add_parser(
        "private-init",
        help="Attach or initialize a private client-config repo for this checkout.",
    )
    private_init_parser.add_argument(
        "--path",
        default=None,
        help="Private repo path. Defaults to ../skillbox-config.",
    )
    private_init_parser.add_argument("--format", choices=("text", "json"), default="text")

    client_project_parser = subparsers.add_parser(
        "client-project",
        help="Compile a single-client runtime projection bundle with sanitized metadata.",
    )
    client_project_parser.add_argument(
        "client_id",
        help="Existing client slug to project (for example `personal`).",
    )
    client_project_parser.add_argument(
        "--output-dir",
        default=None,
        help="Projection output directory. Defaults to builds/clients/<client-id>.",
    )
    client_project_parser.add_argument("--force", action="store_true", help="Replace an existing projection bundle.")
    client_project_parser.add_argument("--dry-run", action="store_true")
    client_project_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(client_project_parser)

    client_open_parser = subparsers.add_parser(
        "client-open",
        help="Project one client into a safe surface with scoped context and MCP config.",
    )
    client_open_parser.add_argument(
        "client_id",
        help="Existing client slug to open (for example `personal`).",
    )
    client_open_parser.add_argument(
        "--output-dir",
        default=None,
        help="Open-surface output directory. Defaults to sand/<client-id>.",
    )
    client_open_parser.add_argument(
        "--from-bundle",
        default=None,
        help="Existing client-project bundle to open instead of building a fresh one.",
    )
    client_open_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(client_open_parser)

    client_publish_parser = subparsers.add_parser(
        "client-publish",
        help="Promote a client projection bundle into a git-backed control-plane repo.",
    )
    client_publish_parser.add_argument(
        "client_id",
        help="Existing client slug to publish (for example `personal`).",
    )
    client_publish_parser.add_argument(
        "--target-dir",
        help="Git repo that receives clients/<client>/current/ and publish.json.",
    )
    client_publish_parser.add_argument(
        "--from-bundle",
        default=None,
        help="Existing client-project bundle to publish instead of building a fresh one.",
    )
    client_publish_parser.add_argument(
        "--acceptance",
        action="store_true",
        help="Run acceptance first and persist compact acceptance evidence with the published client payload.",
    )
    client_publish_parser.add_argument(
        "--deploy-artifact",
        action="store_true",
        help="Build a pinned source archive plus deploy.json for offline box installs.",
    )
    client_publish_parser.add_argument(
        "--commit",
        action="store_true",
        help="Create one local git commit in the target repo when the publish changes files.",
    )
    client_publish_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(client_publish_parser)

    client_diff_parser = subparsers.add_parser(
        "client-diff",
        help="Compare a client projection bundle against the current published payload in a target repo.",
    )
    client_diff_parser.add_argument(
        "client_id",
        help="Existing client slug to diff (for example `personal`).",
    )
    client_diff_parser.add_argument(
        "--target-dir",
        help="Git repo that holds clients/<client>/current/ and publish.json.",
    )
    client_diff_parser.add_argument(
        "--from-bundle",
        default=None,
        help="Existing client-project bundle to diff instead of building a fresh one.",
    )
    client_diff_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(client_diff_parser)

    distribution_publish_parser = subparsers.add_parser(
        "distribution-publish",
        help="Publish one local skill version into a signed schema_version=2 manifest.",
    )
    distribution_publish_parser.add_argument("skill_path")
    distribution_publish_parser.add_argument("--version", type=int, required=True)
    distribution_publish_parser.add_argument("--manifest-path", required=True)
    distribution_publish_parser.add_argument("--artifact-root", required=True)
    distribution_publish_parser.add_argument("--signing-key", required=True)
    distribution_publish_parser.add_argument("--skill-name", default=None)
    distribution_publish_parser.add_argument("--distributor-id", default="local")
    distribution_publish_parser.add_argument("--client-id", default="local")
    distribution_publish_parser.add_argument("--target", action="append", default=[])
    distribution_publish_parser.add_argument("--capability", action="append", default=[])
    distribution_publish_parser.add_argument("--changelog", default=None)
    distribution_publish_parser.add_argument("--min-version", type=int, default=None)
    distribution_publish_parser.add_argument("--min-version-reason", default=None)
    distribution_publish_parser.add_argument("--download-prefix", default="/skills")
    distribution_publish_parser.add_argument("--format", choices=("text", "json"), default="text")

    distribution_preview_parser = subparsers.add_parser(
        "distribution-preview",
        help="Preview selected artifacts from a signed manifest without mutating local state.",
    )
    distribution_preview_parser.add_argument("--manifest-path", required=True)
    distribution_preview_parser.add_argument("--public-key", required=True)
    distribution_preview_parser.add_argument("--distributor-id", default="local")
    distribution_preview_parser.add_argument("--state-root", default=".skillbox-state")
    distribution_preview_parser.add_argument("--pick", action="append", default=[])
    distribution_preview_parser.add_argument("--pin", action="append", default=[])
    distribution_preview_parser.add_argument("--target-env", default=None)
    distribution_preview_parser.add_argument("--lockfile", default=None)
    distribution_preview_parser.add_argument("--format", choices=("text", "json"), default="text")

    distribution_rollback_parser = subparsers.add_parser(
        "distribution-rollback",
        help="Rollback one skill to a verified cached bundle version.",
    )
    distribution_rollback_parser.add_argument("--manifest-path", default=None)
    distribution_rollback_parser.add_argument("--public-key", default=None)
    distribution_rollback_parser.add_argument("--distributor-id", default="local")
    distribution_rollback_parser.add_argument("--skill", required=True)
    distribution_rollback_parser.add_argument("--version", type=int, default=None)
    distribution_rollback_parser.add_argument("--state-root", default=".skillbox-state")
    distribution_rollback_parser.add_argument("--install-target", action="append", default=[])
    distribution_rollback_parser.add_argument("--lockfile", default=None)
    distribution_rollback_parser.add_argument("--reason", default=None)
    distribution_rollback_parser.add_argument("--emergency-override", action="store_true")
    distribution_rollback_parser.add_argument("--list", action="store_true")
    distribution_rollback_parser.add_argument("--format", choices=("text", "json"), default="text")

    onboard_parser = subparsers.add_parser(
        "onboard",
        help="Macro: scaffold a client, sync, bootstrap, start services, generate context, and verify.",
    )
    onboard_parser.add_argument(
        "client_id",
        help="Lowercase client slug, for example `acme-studio`.",
    )
    onboard_parser.add_argument("--label", default=None, help="Human-friendly label for the client.")
    onboard_parser.add_argument(
        "--root-path",
        default=None,
        help="Runtime path for the client root. Defaults to ${SKILLBOX_MONOSERVER_ROOT}.",
    )
    onboard_parser.add_argument(
        "--default-cwd",
        default=None,
        help="Runtime default cwd for the client. Defaults to the client root path.",
    )
    onboard_parser.add_argument(
        "--blueprint",
        default=None,
        help="Apply a reusable client blueprint from workspace/client-blueprints/ or an explicit YAML path.",
    )
    onboard_parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Set a blueprint variable using KEY=VALUE. Can be repeated.",
    )
    onboard_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files.")
    onboard_parser.add_argument("--dry-run", action="store_true")
    onboard_parser.add_argument(
        "--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS,
    )
    onboard_parser.add_argument("--format", choices=("text", "json"), default="text")

    first_box_parser = subparsers.add_parser(
        "first-box",
        help="Canonical first-run path: attach the private repo, reuse or scaffold the client, prove readiness, and open a client surface.",
    )
    first_box_parser.add_argument(
        "client_id",
        nargs="?",
        default="personal",
        help="Client slug to prepare. Defaults to `personal`.",
    )
    first_box_parser.add_argument(
        "--private-path",
        default=None,
        help="Private config repo path. Defaults to ../skillbox-config.",
    )
    first_box_parser.add_argument(
        "--output-dir",
        default=None,
        help="Open-surface output directory. Defaults to sand/<client-id>.",
    )
    first_box_parser.add_argument("--label", default=None, help="Human-friendly label for the client when scaffolding.")
    first_box_parser.add_argument(
        "--root-path",
        default=None,
        help="Runtime path for the client root when scaffolding. Defaults to ${SKILLBOX_MONOSERVER_ROOT}.",
    )
    first_box_parser.add_argument(
        "--default-cwd",
        default=None,
        help="Runtime default cwd for the client when scaffolding. Defaults to the client root path.",
    )
    first_box_parser.add_argument(
        "--blueprint",
        default=None,
        help="Apply a reusable client blueprint when scaffolding a missing client.",
    )
    first_box_parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Set a blueprint variable using KEY=VALUE. Can be repeated.",
    )
    first_box_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files when onboarding.")
    first_box_parser.add_argument(
        "--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS,
    )
    first_box_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(first_box_parser)

    focus_parser = subparsers.add_parser(
        "focus",
        help="Activate a client workspace with live state and enriched agent context.",
    )
    focus_parser.add_argument(
        "client_id",
        nargs="?",
        default="",
        help="Existing client slug to focus on (e.g. 'personal').",
    )
    focus_parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-activate the last focus session from .focus.json.",
    )
    focus_parser.add_argument(
        "--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS,
    )
    focus_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(focus_parser)
    # --client accepted for CLI consistency with up/status/logs/doctor
    # (local_runtime_core_cutover WG-003). The positional client_id remains
    # the primary input; --client is honored when no positional is given.
    _add_client_arg(focus_parser)
    _add_service_arg(focus_parser)
    _add_context_dir_arg(focus_parser)

    stewardship_parser = subparsers.add_parser(
        "stewardship-report",
        help="Summarize current operator evidence, risks, and unassessed hardening gaps for a client.",
    )
    stewardship_parser.add_argument(
        "client_id",
        nargs="?",
        default="",
        help="Existing client slug to report on (e.g. 'personal').",
    )
    stewardship_parser.add_argument("--format", choices=("text", "json", "md"), default="text")
    stewardship_parser.add_argument(
        "--write",
        action="store_true",
        help="Write the report artifact into the client report directory.",
    )
    stewardship_parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for report artifacts. Defaults to the client reports/stewardship directory when --write is used.",
    )
    _add_profile_arg(stewardship_parser)
    _add_client_arg(stewardship_parser)

    parity_report_parser = subparsers.add_parser(
        "parity-report",
        help="Compare a client runtime graph against its production-stack parity contract.",
    )
    parity_report_parser.add_argument(
        "client_id",
        nargs="?",
        default="",
        help="Existing client slug to report on (e.g. 'personal').",
    )
    parity_report_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(parity_report_parser)
    _add_client_arg(parity_report_parser)
    _add_cwd_arg(parity_report_parser)

    session_start_parser = subparsers.add_parser(
        "session-start",
        help="Create a durable client-scoped session ledger with metadata and append-only events.",
    )
    session_start_parser.add_argument("client_id", help="Existing client slug to attach the session to.")
    session_start_parser.add_argument("--label", default="", help="Human-friendly session label.")
    session_start_parser.add_argument("--cwd", default="", help="Working directory for the session.")
    session_start_parser.add_argument("--goal", default="", help="Short statement of intent for the session.")
    session_start_parser.add_argument("--actor", default="", help="Optional actor or operator name.")
    session_start_parser.add_argument("--format", choices=("text", "json"), default="text")

    session_event_parser = subparsers.add_parser(
        "session-event",
        help="Append a structured event to an active durable session.",
    )
    session_event_parser.add_argument("client_id", help="Existing client slug that owns the session.")
    session_event_parser.add_argument("--session-id", required=True, help="Durable session id.")
    session_event_parser.add_argument("--event-type", required=True, help="Session event type, with or without session. prefix.")
    session_event_parser.add_argument("--message", default="", help="Optional event message.")
    session_event_parser.add_argument("--actor", default="", help="Optional actor or operator name.")
    session_event_parser.add_argument("--format", choices=("text", "json"), default="text")

    session_end_parser = subparsers.add_parser(
        "session-end",
        help="Close an active durable session and persist a handoff summary.",
    )
    session_end_parser.add_argument("client_id", help="Existing client slug that owns the session.")
    session_end_parser.add_argument("--session-id", required=True, help="Durable session id.")
    session_end_parser.add_argument(
        "--status",
        default="completed",
        choices=sorted(SESSION_TERMINAL_STATUSES),
        help="Terminal lifecycle state to persist.",
    )
    session_end_parser.add_argument("--summary", default="", help="Optional closeout summary.")
    session_end_parser.add_argument("--format", choices=("text", "json"), default="text")

    session_resume_parser = subparsers.add_parser(
        "session-resume",
        help="Resume a previously ended durable session.",
    )
    session_resume_parser.add_argument("client_id", help="Existing client slug that owns the session.")
    session_resume_parser.add_argument("--session-id", required=True, help="Durable session id.")
    session_resume_parser.add_argument("--actor", default="", help="Optional actor or operator name.")
    session_resume_parser.add_argument("--message", default="", help="Optional resume note.")
    session_resume_parser.add_argument("--format", choices=("text", "json"), default="text")

    session_status_parser = subparsers.add_parser(
        "session-status",
        help="Read one durable session or list recent sessions for a client.",
    )
    session_status_parser.add_argument("client_id", help="Existing client slug that owns the session.")
    session_status_parser.add_argument("--session-id", default=None, help="Specific durable session id to inspect.")
    session_status_parser.add_argument("--limit", type=int, default=10, help="Maximum sessions to return when listing.")
    session_status_parser.add_argument("--format", choices=("text", "json"), default="text")

    worker_submit_parser = subparsers.add_parser(
        "worker-submit",
        help="Create a broker-managed worker run without launching a runtime before context resolution.",
    )
    worker_submit_parser.add_argument("task_class", choices=WORKER_TASK_CLASSES)
    worker_submit_parser.add_argument("instruction")
    worker_submit_parser.add_argument("--client", default="", help="Overlay id for context resolution.")
    worker_submit_parser.add_argument("--cwd", default="", help="Working directory hint for context resolution.")
    worker_submit_parser.add_argument("--repo-hint", default="", help="Optional repo id or path hint.")
    worker_submit_parser.add_argument("--runtime", default="", help="Requested runtime id. Defaults to hermes.")
    worker_submit_parser.add_argument("--write-scope", choices=WORKER_WRITE_SCOPES, default=WORKER_DEFAULT_WRITE_SCOPE)
    worker_submit_parser.add_argument("--memory-scope", choices=WORKER_MEMORY_SCOPES, default=WORKER_DEFAULT_MEMORY_SCOPE)
    worker_submit_parser.add_argument("--artifact-policy", default=WORKER_DEFAULT_ARTIFACT_POLICY)
    worker_submit_parser.add_argument("--harness-session-ref", default="", help="Opaque caller correlation id.")
    worker_submit_parser.add_argument("--format", choices=("text", "json"), default="text")

    worker_status_parser = subparsers.add_parser(
        "worker-status",
        help="Read a broker-managed worker run by run id.",
    )
    worker_status_parser.add_argument("run_id")
    worker_status_parser.add_argument("--format", choices=("text", "json"), default="text")

    worker_artifacts_parser = subparsers.add_parser(
        "worker-artifacts",
        help="Read result artifacts for a terminal broker-managed worker run.",
    )
    worker_artifacts_parser.add_argument("run_id")
    worker_artifacts_parser.add_argument("--format", choices=("text", "json"), default="text")

    worker_promote_parser = subparsers.add_parser(
        "worker-promote-learning",
        help="Promote a reviewed worker learning proposal into its target.",
    )
    worker_promote_parser.add_argument("proposal_id")
    worker_promote_parser.add_argument("--approved-by", default="", help="Operator or reviewer approving promotion.")
    worker_promote_parser.add_argument("--target-kind", required=True)
    worker_promote_parser.add_argument("--target-location", required=True)
    worker_promote_parser.add_argument("--promotion-mode", default="promote")
    worker_promote_parser.add_argument("--format", choices=("text", "json"), default="text")

    acceptance_parser = subparsers.add_parser(
        "acceptance",
        help="Run the first-box readiness gate: doctor-pre, sync, focus, mcp-smoke, doctor-post.",
    )
    acceptance_parser.add_argument(
        "client_id",
        help="Existing client slug to validate for first-box acceptance (for example `personal`).",
    )
    acceptance_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS)
    acceptance_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(acceptance_parser)

    return parser


def _apply_default_command(args: argparse.Namespace) -> None:
    """When no subcommand is given, fall back to a compact `status` invocation."""
    if args.command is None:
        args.command = "status"
        args.format = "text"
        args.compact = False
        args.profile = []
        args.client = []


def _resolve_local_runtime_mode(args: argparse.Namespace) -> tuple[str, dict[str, Any] | None]:
    """Validate `--mode` and return the resolved mode plus an optional structured error.

    WG-003 requires unknown values to surface as a structured
    LOCAL_RUNTIME_MODE_UNSUPPORTED envelope (shared.md:457-469) rather than
    an argparse usage error. Only `up` accepts `--mode` today; other
    surfaces ignore it. Mode is orthogonal to --profile and --service
    (shared.md:518-520, Business Rule 2).
    """
    requested_mode_raw = getattr(args, "mode", None)
    default_mode = "reuse"
    if requested_mode_raw is None or str(requested_mode_raw).strip() == "":
        return default_mode, None

    resolved_mode = str(requested_mode_raw).strip()
    if resolved_mode in LOCAL_RUNTIME_START_MODES:
        return resolved_mode, None

    supported = ", ".join(LOCAL_RUNTIME_START_MODES)
    err = local_runtime_error(
        LOCAL_RUNTIME_MODE_UNSUPPORTED,
        f"Unsupported --mode value {resolved_mode!r}. Supported modes: {supported}.",
        recoverable=True,
        next_action=f"Re-run with --mode <{'|'.join(LOCAL_RUNTIME_START_MODES)}>.",
    )
    err["error"]["requested_mode"] = resolved_mode
    return resolved_mode, err


def _handle_client_init(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        if args.list_blueprints:
            blueprints = list_client_blueprints(root_dir)
            if args.format == "json":
                emit_json({"blueprints": blueprints})
            else:
                print_client_blueprints_text(blueprints)
            return EXIT_OK

        if not args.client_id:
            raise RuntimeError("client-init requires <client_id> unless --list-blueprints is used.")

        assignments = parse_key_value_assignments(args.set, "--set")
        actions, blueprint_metadata = scaffold_client_overlay(
            root_dir=root_dir,
            client_id=args.client_id,
            label=args.label,
            default_cwd=args.default_cwd,
            root_path=args.root_path,
            blueprint_name=args.blueprint,
            blueprint_assignments=assignments,
            dry_run=args.dry_run,
            force=args.force,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-init"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    cid = validate_client_id(args.client_id)
    payload: dict[str, Any] = {
        "client_id": cid,
        "dry_run": args.dry_run,
        "force": args.force,
        "actions": actions,
        "next_actions": next_actions_for_client_init(cid),
    }
    if blueprint_metadata is not None:
        payload["blueprint"] = blueprint_metadata
    if args.format == "json":
        emit_json(payload)
    else:
        if blueprint_metadata is not None:
            print(f"blueprint: {blueprint_metadata['id']}")
        print("\n".join(actions))
    return EXIT_OK


def _handle_onboard(args: argparse.Namespace, root_dir: Path) -> int:
    return run_onboard(
        root_dir=root_dir,
        client_id=args.client_id,
        label=args.label,
        default_cwd=args.default_cwd,
        root_path=args.root_path,
        blueprint_name=args.blueprint,
        set_args=args.set,
        dry_run=args.dry_run,
        force=args.force,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        fmt=args.format,
    )


def _handle_first_box(args: argparse.Namespace, root_dir: Path) -> int:
    return run_first_box(
        root_dir=root_dir,
        client_id=args.client_id,
        private_path_arg=args.private_path,
        profiles=args.profile,
        output_dir_arg=args.output_dir,
        label=args.label,
        default_cwd=args.default_cwd,
        root_path=args.root_path,
        blueprint_name=args.blueprint,
        set_args=args.set,
        force=args.force,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        fmt=args.format,
    )


def _handle_private_init(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = init_private_repo(
            root_dir=root_dir,
            target_dir_arg=args.path,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "private-init"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"target_dir: {payload['target_dir']}")
        print(f"clients_host_root: {payload['clients_host_root']}")
        print()
        print("\n".join(payload["actions"]))
    return EXIT_OK


def _handle_acceptance(args: argparse.Namespace, root_dir: Path) -> int:
    return run_acceptance(
        root_dir=root_dir,
        client_id=args.client_id,
        profiles=args.profile,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        fmt=args.format,
    )


def _handle_client_project(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = project_client_bundle(
            root_dir=root_dir,
            client_id=args.client_id,
            profiles=args.profile,
            output_dir_arg=args.output_dir,
            dry_run=args.dry_run,
            force=args.force,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-project"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"client: {payload['client_id']}")
        print(f"output_dir: {payload['output_dir']}")
        print(f"files: {payload['file_count']}")
        print(f"payload_tree_sha256: {payload['payload_tree_sha256']}")
        print()
        print("\n".join(payload["actions"]))
    return EXIT_OK


def _handle_client_open(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload, exit_code = open_client_surface(
            root_dir=root_dir,
            client_id=args.client_id,
            profiles=args.profile,
            output_dir_arg=args.output_dir,
            from_bundle_arg=args.from_bundle,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-open"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"client: {payload['client_id']}")
        print(f"output_dir: {payload['output_dir']}")
        print(f"profiles: {', '.join(payload['active_profiles'])}")
        print(f"mcp_servers: {', '.join(payload['mcp_servers'])}")
        print(f"focus: {payload['focus']['status']}")
        print()
        print("\n".join(payload["actions"]))
    return exit_code


def _handle_client_publish(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = publish_client_bundle(
            root_dir=root_dir,
            client_id=args.client_id,
            target_dir_arg=args.target_dir,
            from_bundle_arg=args.from_bundle,
            profiles=args.profile,
            require_acceptance=args.acceptance,
            write_deploy_artifact=args.deploy_artifact,
            commit=args.commit,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-publish"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"client: {payload['client_id']}")
        print(f"target_dir: {payload['target_dir']}")
        print(f"changed: {payload['changed']}")
        print(f"payload_tree_sha256: {payload['payload_tree_sha256']}")
        if payload["acceptance"]["present"]:
            print(f"accepted_at: {payload['acceptance']['accepted_at']}")
            print(f"acceptance_profiles: {', '.join(payload['acceptance']['active_profiles'])}")
        if payload["deploy"]["present"]:
            print(f"deploy_manifest: {payload['deploy']['manifest']}")
            print(f"deploy_archive: {payload['deploy']['archive']}")
        if payload["commit_hash"]:
            print(f"commit: {payload['commit_hash']}")
        print()
        print("\n".join(payload["actions"]))
    return EXIT_OK


def _handle_client_diff(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = diff_client_bundle(
            root_dir=root_dir,
            client_id=args.client_id,
            target_dir_arg=args.target_dir,
            from_bundle_arg=args.from_bundle,
            profiles=args.profile,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-diff"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print_client_diff_text(payload)
    return EXIT_OK


def _handle_distribution_publish(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = publish_skill_release(
            skill_path=host_path_to_absolute_path(root_dir, args.skill_path),
            version=args.version,
            manifest_path=host_path_to_absolute_path(root_dir, args.manifest_path),
            artifact_root=host_path_to_absolute_path(root_dir, args.artifact_root),
            signing_key_ref=args.signing_key,
            distributor_id=args.distributor_id,
            client_id=args.client_id,
            skill_name=args.skill_name,
            targets=args.target,
            capabilities=args.capability,
            changelog=args.changelog,
            min_version=args.min_version,
            min_version_reason=args.min_version_reason,
            download_prefix=args.download_prefix,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(RuntimeError(str(exc)), "distribution-publish"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"skill: {payload['skill']}")
        print(f"version: {payload['version']}")
        print(f"result: {payload['result']}")
        print(f"artifact: {payload['artifact_path']}")
        print(f"manifest: {payload['manifest_path']}")
        print(f"sha256: {payload['artifact_sha256']}")
    return EXIT_OK


def _handle_distribution_preview(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = preview_manifest(
            manifest_bytes=host_path_to_absolute_path(root_dir, args.manifest_path).read_bytes(),
            public_key_config=args.public_key,
            distributor_id=args.distributor_id,
            state_root=host_path_to_absolute_path(root_dir, args.state_root),
            pick=args.pick,
            pin=_parse_distribution_pin_args(args.pin),
            target_env=args.target_env,
            lockfile_path=(
                host_path_to_absolute_path(root_dir, args.lockfile)
                if args.lockfile else None
            ),
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(RuntimeError(str(exc)), "distribution-preview"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"distributor: {payload['distributor_id']}")
        print(f"manifest_version: {payload['manifest_version']}")
        print(f"ready: {payload['ready']}")
        for item in payload["items"]:
            print(
                f"{item['action']}: {item['skill']} v{item['selected_version']} "
                f"{item['pinned_by']} cache={item['cache_state']}"
            )
    return EXIT_OK if payload.get("ready") else EXIT_ERROR


def _handle_distribution_rollback(args: argparse.Namespace, root_dir: Path) -> int:
    state_root = host_path_to_absolute_path(root_dir, args.state_root)
    if args.list:
        payload = {
            "ok": True,
            "skill": args.skill,
            "cached_versions": cached_versions(
                state_root=state_root,
                skill_name=args.skill,
            ),
        }
        if args.format == "json":
            emit_json(payload)
        else:
            print(f"skill: {payload['skill']}")
            print("cached_versions: " + ", ".join(str(v) for v in payload["cached_versions"]))
        return EXIT_OK

    try:
        missing = [
            option
            for option, value in (
                ("--manifest-path", args.manifest_path),
                ("--public-key", args.public_key),
                ("--version", args.version),
                ("--lockfile", args.lockfile),
            )
            if value is None
        ]
        if missing:
            raise DistributionRollbackError(
                "distribution-rollback requires " + ", ".join(missing)
            )
        install_targets = [
            {"id": f"target-{index + 1}", "host_path": str(host_path_to_absolute_path(root_dir, target))}
            for index, target in enumerate(args.install_target)
        ]
        if not install_targets:
            raise DistributionRollbackError("distribution-rollback requires --install-target")
        payload = rollback_distributor_skill(
            manifest_path=host_path_to_absolute_path(root_dir, args.manifest_path),
            public_key_config=args.public_key,
            distributor_id=args.distributor_id,
            skill_name=args.skill,
            target_version=args.version,
            state_root=state_root,
            install_targets=install_targets,
            lockfile_path=host_path_to_absolute_path(root_dir, args.lockfile),
            reason=args.reason,
            emergency_override=args.emergency_override,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(RuntimeError(str(exc)), "distribution-rollback"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"skill: {payload['skill']}")
        print(f"to_version: {payload['to_version']}")
        print(f"source: {payload['source']}")
        print(f"lockfile_updated: {payload['lockfile_updated']}")
    return EXIT_OK


def _parse_distribution_pin_args(raw_items: list[str]) -> dict[str, int]:
    pins: dict[str, int] = {}
    for raw_item in raw_items or []:
        if "=" not in raw_item:
            raise DistributionPreviewError("--pin must use skill=version")
        skill, raw_version = raw_item.split("=", 1)
        skill = skill.strip()
        if not skill:
            raise DistributionPreviewError("--pin skill name must be non-empty")
        try:
            version = int(raw_version)
        except ValueError as exc:
            raise DistributionPreviewError("--pin version must be an integer") from exc
        pins[skill] = version
    return pins


def _handle_focus(args: argparse.Namespace, root_dir: Path) -> int:
    cid = args.client_id or ""
    # CLI-consistency (WG-003): fall back to --client for parity with
    # up/status/logs/doctor when no positional client_id was provided.
    if not cid:
        client_flags = getattr(args, "client", []) or []
        if client_flags:
            cid = str(client_flags[0]).strip()
    if not cid and not args.resume:
        print("focus requires a client_id or --resume.", file=sys.stderr)
        return EXIT_ERROR
    return run_focus(
        root_dir=root_dir,
        client_id=cid,
        profiles=args.profile,
        service_filter=getattr(args, "service", []),
        resume=args.resume,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        fmt=args.format,
        context_dir=resolve_context_dir(root_dir, getattr(args, "context_dir", None)),
    )


def _handle_stewardship_report(args: argparse.Namespace, root_dir: Path) -> int:
    cid = args.client_id or ""
    if not cid:
        client_flags = getattr(args, "client", []) or []
        if client_flags:
            cid = str(client_flags[0]).strip()
    if not cid:
        print("stewardship-report requires a client_id or --client.", file=sys.stderr)
        return EXIT_ERROR
    return run_stewardship_report(
        root_dir=root_dir,
        client_id=cid,
        profiles=args.profile,
        fmt=args.format,
        write=bool(args.write),
        output_dir_arg=args.output_dir,
    )


def _handle_session_start(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = start_client_session(
            root_dir,
            args.client_id,
            label=args.label,
            cwd=args.cwd,
            goal=args.goal,
            actor=args.actor,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-start"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        session = payload["session"]
        print(f"client: {payload['client_id']}")
        print(f"session: {session['session_id']}")
        print(f"status: {session['status']}")
        if session.get("label"):
            print(f"label: {session['label']}")
    return EXIT_OK


def _handle_session_event(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        detail = {"actor": args.actor} if args.actor else None
        payload = append_client_session_event(
            root_dir,
            args.client_id,
            args.session_id,
            event_type=args.event_type,
            message=args.message,
            detail=detail,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-event"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        session = payload["session"]
        print(f"client: {payload['client_id']}")
        print(f"session: {session['session_id']}")
        print(f"last_event: {session.get('last_event_type', '-')}")
        if session.get("last_message"):
            print(f"message: {session['last_message']}")
    return EXIT_OK


def _handle_session_end(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = end_client_session(
            root_dir,
            args.client_id,
            args.session_id,
            final_status=args.status,
            summary=args.summary,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-end"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        session = payload["session"]
        print(f"client: {payload['client_id']}")
        print(f"session: {session['session_id']}")
        print(f"status: {session['status']}")
    return EXIT_OK


def _handle_session_resume(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = resume_client_session(
            root_dir,
            args.client_id,
            args.session_id,
            actor=args.actor,
            message=args.message,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-resume"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        session = payload["session"]
        print(f"client: {payload['client_id']}")
        print(f"session: {session['session_id']}")
        print(f"status: {session['status']}")
    return EXIT_OK


def _handle_session_status(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = session_status_payload(
            root_dir,
            args.client_id,
            session_id=args.session_id,
            limit=max(0, int(args.limit)),
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-status"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        if payload.get("session"):
            session = payload["session"]
            print(f"client: {payload['client_id']}")
            print(f"session: {session['session_id']}")
            print(f"status: {session['status']}")
            print(f"events: {len(session.get('recent_events') or [])}")
        else:
            print(f"client: {payload['client_id']}")
            print(f"sessions: {payload['count']}")
            for session in payload.get("sessions") or []:
                label = session.get("label") or "-"
                print(f"  - {session['session_id']}: {session.get('status', 'unknown')} {label}")
    return EXIT_OK


def _handle_worker_submit(args: argparse.Namespace, root_dir: Path) -> int:
    exit_code, payload = _emit_worker_payload_or_error(
        args,
        lambda: create_worker_run(
            root_dir,
            task_class=args.task_class,
            instruction=args.instruction,
            client_id=args.client,
            cwd=args.cwd,
            repo_hint=args.repo_hint,
            runtime=args.runtime,
            artifact_policy=args.artifact_policy,
            write_scope=args.write_scope,
            memory_scope=args.memory_scope,
            harness_session_ref=args.harness_session_ref,
        ),
    )
    return _emit_worker_cli_payload(args, exit_code, payload, _worker_submit_text)


def _emit_worker_payload_or_error(
    args: argparse.Namespace,
    operation: Callable[[], dict[str, Any]],
) -> tuple[int, dict[str, Any]]:
    try:
        return EXIT_OK, operation()
    except WorkerRuntimeError as exc:
        return EXIT_ERROR, worker_runtime_error_payload(exc)
    except RuntimeError as exc:
        return EXIT_ERROR, classify_error(exc, str(getattr(args, "command", "worker")))


def _emit_worker_cli_payload(
    args: argparse.Namespace,
    exit_code: int,
    payload: dict[str, Any],
    text_lines: Callable[[dict[str, Any]], list[str]],
) -> int:
    if args.format == "json":
        emit_json(payload)
    elif exit_code != EXIT_OK:
        print(payload["error"]["message"], file=sys.stderr)
    else:
        print("\n".join(text_lines(payload)))
    return exit_code


def _worker_submit_text(payload: dict[str, Any]) -> list[str]:
    return [
        f"worker run: {payload['run_id']}",
        f"state: {payload['state']}",
        f"runtime: {payload['runtime']}",
        f"context: {payload['context_state']}",
    ]


def _worker_status_text(payload: dict[str, Any]) -> list[str]:
    return [
        f"worker run: {payload['run_id']}",
        f"state: {payload['state']}",
        f"runtime: {payload['runtime']}",
    ]


def _worker_artifacts_text(payload: dict[str, Any]) -> list[str]:
    result = payload.get("result") or {}
    lines = [
        f"worker run: {payload['run_id']}",
        f"artifacts: {len(payload.get('artifacts') or [])}",
    ]
    if result:
        lines.append(f"summary: {result.get('summary') or '-'}")
    return lines


def _worker_promote_text(payload: dict[str, Any]) -> list[str]:
    return [
        f"proposal: {payload['proposal_id']}",
        f"status: {payload['status']}",
        f"target: {payload['target_kind']} {payload['target_location']}",
    ]


def _handle_worker_status(args: argparse.Namespace, root_dir: Path) -> int:
    exit_code, payload = _emit_worker_payload_or_error(
        args,
        lambda: worker_status_payload(root_dir, args.run_id),
    )
    return _emit_worker_cli_payload(args, exit_code, payload, _worker_status_text)


def _handle_worker_artifacts(args: argparse.Namespace, root_dir: Path) -> int:
    exit_code, payload = _emit_worker_payload_or_error(
        args,
        lambda: worker_artifacts_payload(root_dir, args.run_id),
    )
    return _emit_worker_cli_payload(args, exit_code, payload, _worker_artifacts_text)


def _handle_worker_promote_learning(args: argparse.Namespace, root_dir: Path) -> int:
    exit_code, payload = _emit_worker_payload_or_error(
        args,
        lambda: promote_worker_learning(
            root_dir,
            proposal_id=args.proposal_id,
            approved_by=args.approved_by,
            target_kind=args.target_kind,
            target_location=args.target_location,
            promotion_mode=args.promotion_mode,
        ),
    )
    return _emit_worker_cli_payload(args, exit_code, payload, _worker_promote_text)


def _handle_swimmers_launch(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        exit_code, payload = launch_swimmers_batch(
            positional_dirs=list(getattr(args, "dirs", []) or []),
            dir_flags=list(getattr(args, "dir_flags", []) or []),
            cwd_flags=list(getattr(args, "cwd_flags", []) or []),
            dirs_file=getattr(args, "dirs_file", None),
            group=getattr(args, "group", None),
            group_path=getattr(args, "group_path", None),
            managed_only=bool(getattr(args, "managed_only", False)),
            request=getattr(args, "request", None),
            request_file=getattr(args, "request_file", None),
            tool=str(getattr(args, "tool", "codex") or "codex"),
            launch_target=getattr(args, "target", None),
            base_url=getattr(args, "base_url", None),
            auth_token_env=getattr(args, "auth_token_env", None),
            invoke_cwd=getattr(args, "invoke_cwd", None),
            timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "swimmers-launch"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print("\n".join(swimmers_launch_text_lines(payload)))
    return exit_code


def _handle_mmdx(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload, exit_code = mmdx_open_payload(
            root_dir=root_dir,
            cwd=Path(args.cwd) if args.cwd else None,
            query_parts=getattr(args, "query", []) or [],
            search_roots=getattr(args, "search_root", []) or [],
            open_file=bool(getattr(args, "open", True)),
            limit=max(1, int(getattr(args, "limit", MMDX_DEFAULT_LIMIT))),
            tmux=bool(getattr(args, "tmux", False)),
            tmux_submit=bool(getattr(args, "tmux_submit", False)),
            allow_parser_install=bool(getattr(args, "allow_parser_install", False)),
        )
    except RuntimeError as exc:
        payload = mmdx_error_payload(exc)
        if args.format == "json":
            emit_json(payload)
        else:
            print_mmdx_payload_text(payload)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print_mmdx_payload_text(payload)
    return exit_code


def _capabilities_payload(root_dir: Path) -> dict[str, Any]:
    commands = [
        {
            "name": name,
            "json": True,
            "safe_first_try": _safe_first_try_command(name),
        }
        for name in sorted(MANAGE_COMMAND_NAMES)
    ]
    return {
        "ok": True,
        "tool": "skillbox-manage",
        "contract_version": "2026-05-09",
        "root_dir": str(root_dir),
        "entrypoints": [
            "python3 .env-manager/manage.py",
            "python3 scripts/04-reconcile.py",
            "python3 scripts/box.py",
            "scripts/sbp",
            "scripts/sbo",
        ],
        "commands": commands,
        "agent_surfaces": {
            "capabilities": "python3 .env-manager/manage.py capabilities --json",
            "robot_docs": "python3 .env-manager/manage.py robot-docs guide",
            "robot_triage": "python3 .env-manager/manage.py --robot-triage",
            "json_aliases": sorted(JSON_FLAG_ALIASES),
            "outer_reconcile": "python3 scripts/04-reconcile.py capabilities --json",
            "box_lifecycle": "python3 scripts/box.py capabilities --json",
            "wrappers": ["scripts/sbp capabilities --json", "scripts/sbo capabilities --json"],
        },
        "safe_previews": [
            "python3 .env-manager/manage.py client-init --list-blueprints --format json",
            "python3 .env-manager/manage.py client-project <client> --dry-run --format json",
            "python3 .env-manager/manage.py client-diff <client> --target-dir <control-plane-repo> --format json",
            (
                "python3 .env-manager/manage.py distribution-preview --manifest-path <manifest.json> "
                "--public-key <public-key.pem> --format json"
            ),
            "python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync",
            "python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json",
        ],
        "exit_codes": {
            "0": "success",
            "1": "user input, runtime, or environment error",
            "2": "drift detected or argparse usage error, depending on command surface",
            "3": "operator input required",
        },
        "env": {
            "SKILLBOX_STATE_ROOT": "Persistent runtime state root.",
            "SKILLBOX_MONOSERVER_ROOT": "Host or container repo universe root.",
            "SKILLBOX_CLIENTS_ROOT": "Runtime client overlay root.",
            "NO_COLOR": "Suppress ANSI styling in supported surfaces.",
            "CI": "Prefer non-interactive behavior in supported surfaces.",
        },
        "next_actions": [
            "python3 .env-manager/manage.py status --format json",
            "python3 .env-manager/manage.py doctor --format json",
            "python3 .env-manager/manage.py robot-docs guide",
        ],
    }


def _safe_first_try_command(name: str) -> str:
    if name in {"capabilities", "robot-triage"}:
        return f"manage.py {name} --json"
    if name == "robot-docs":
        return "manage.py robot-docs guide"
    if name in {"client-init"}:
        return "manage.py client-init --list-blueprints --format json"
    if name in {"client-project"}:
        return "manage.py client-project <client> --dry-run --format json"
    if name in {"client-diff"}:
        return "manage.py client-diff <client> --target-dir <control-plane-repo> --format json"
    if name in {"client-publish"}:
        return "manage.py client-diff <client> --target-dir <control-plane-repo> --format json"
    if name in {"client-open"}:
        return "manage.py client-project <client> --dry-run --format json"
    if name in {"distribution-preview"}:
        return "manage.py distribution-preview --manifest-path <manifest.json> --public-key <public-key.pem> --format json"
    if name in {"distribution-publish"}:
        return "manage.py distribution-preview --manifest-path <manifest.json> --public-key <public-key.pem> --format json"
    if name in {"distribution-rollback"}:
        return "manage.py distribution-rollback --list --skill <skill> --format json"
    if name in {"status", "render", "doctor", "skills", "skill-audit", "mcp-audit"}:
        return f"manage.py {name} --format json"
    if name == "parity-report":
        return "manage.py parity-report <client> --format json"
    if name == "swimmers-launch":
        return "manage.py swimmers-launch <dir> <dir> --request '<prompt>' --dry-run --format json"
    if name in {"up", "down", "restart", "sync", "bootstrap", "context"}:
        return f"manage.py {name} --dry-run --format json"
    return f"manage.py {name} --help"


def _robot_docs_guide() -> str:
    return """Skillbox agent guide

Primary entrypoint:
  python3 .env-manager/manage.py <command> [options]

Start here:
  python3 .env-manager/manage.py capabilities --json
  python3 .env-manager/manage.py status --format json
  python3 .env-manager/manage.py doctor --format json
  python3 .env-manager/manage.py --robot-triage

Structured output:
  Most read-side and runtime commands accept --format json.
  Agent-friendly aliases are accepted: --json, --jason, --jsno, --jsson.
  Diagnostics and typo-alias notices are printed to stderr, not stdout.

Safe mutation pattern:
  Preview first when available: sync --dry-run, bootstrap --dry-run,
  up --dry-run, down --dry-run, restart --dry-run, context --dry-run.
  For skill lifecycle removals or pruning, use --dry-run before --yes.

Useful command families:
  status --format json          Compact runtime state.
  doctor --format json          Runtime validation checks.
  skills --issues-only          Skill visibility issues for the current cwd.
  mcp-audit --format json       Claude/Codex MCP parity audit.
  parity-report <client>        Dev/prod parity report for a client.
  swimmers-launch <dirs...>     Launch one Swimmers agent session per dir.
  focus <client> --format json  Sync, bootstrap, start, collect live state, context.
"""


def _handle_capabilities(args: argparse.Namespace, root_dir: Path) -> int:
    emit_json(_capabilities_payload(root_dir))
    return EXIT_OK


def _handle_robot_docs(args: argparse.Namespace, root_dir: Path) -> int:
    guide = _robot_docs_guide()
    if args.format == "json":
        emit_json({"ok": True, "topic": args.topic, "guide": guide})
    else:
        print(guide.rstrip())
    return EXIT_OK


def _handle_robot_triage(args: argparse.Namespace, root_dir: Path) -> int:
    payload: dict[str, Any] = {
        "ok": True,
        "tool": "skillbox-manage",
        "quick_ref": _capabilities_payload(root_dir)["next_actions"],
        "recommendations": [
            {
                "id": "start-status",
                "command": "python3 .env-manager/manage.py status --format json",
                "why": "Fastest non-mutating runtime inspection.",
            },
            {
                "id": "validate-runtime",
                "command": "python3 .env-manager/manage.py doctor --format json",
                "why": "Find graph, filesystem, and skill-integrity drift.",
            },
            {
                "id": "preview-before-mutation",
                "command": "python3 .env-manager/manage.py up --dry-run --format json",
                "why": "Preview service graph actions before starting services.",
            },
        ],
        "commands": {
            "capabilities": "python3 .env-manager/manage.py capabilities --json",
            "guide": "python3 .env-manager/manage.py robot-docs guide",
            "status": "python3 .env-manager/manage.py status --format json",
            "doctor": "python3 .env-manager/manage.py doctor --format json",
        },
    }
    try:
        model = build_runtime_model(root_dir)
        payload["health"] = {
            "model_loaded": True,
            "repos": len(model.get("repos") or []),
            "services": len(model.get("services") or []),
            "checks": len(model.get("checks") or []),
        }
    except Exception as exc:
        payload["ok"] = False
        payload["health"] = {
            "model_loaded": False,
            "error": str(exc),
            "next_action": "python3 .env-manager/manage.py doctor --format json",
        }
    emit_json(payload)
    return EXIT_OK if payload["ok"] else EXIT_ERROR


def _operator_booking_config_lines(payload: dict[str, Any]) -> list[str]:
    config = payload.get("operator_booking") or {}
    return [
        f"operator booking: {config.get('client_id') or '-'}",
        f"availability: {config.get('availability_url') or '-'}",
        f"book hold: {config.get('booking_hold_url') or '-'}",
        f"magic link: {config.get('magic_link_url') or '-'}",
        f"publishable key: {config.get('api_key_env') or '-'} configured={config.get('api_key_configured')}",
        f"access token: {config.get('access_token_env') or '-'} configured={config.get('access_token_configured')}",
    ]


def _operator_booking_availability_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"operator booking: {payload.get('client_id') or '-'}",
        f"booking url: {payload.get('booking_url') or '-'}",
        f"timezone: {payload.get('timezone') or '-'}",
        f"available slots: {payload.get('available', 0)}",
    ]
    for slot in payload.get("slots") or []:
        price = slot.get("price")
        price_text = f"${price}" if price is not None else "-"
        lines.append(f"  - {slot.get('date')} {slot.get('slot')} {price_text}")
    return lines


def _operator_booking_book_lines(payload: dict[str, Any]) -> list[str]:
    if payload.get("dry_run"):
        lines = ["operator booking dry run", f"book hold: {payload.get('booking_url')}"]
        if payload.get("magic_link_url"):
            lines.append(f"magic link: {payload.get('magic_link_url')}")
        return lines

    booking = payload.get("booking") or {}
    magic_link = payload.get("magic_link")
    lines = [
        f"booking id: {booking.get('bookingId') or '-'}",
        f"resource: {booking.get('resourceKey') or '-'}",
        f"action: {booking.get('actionKey') or '-'}",
        f"price: {booking.get('priceDisplay') or booking.get('price') or '-'}",
    ]
    if magic_link:
        lines.append(f"magic link sent: {magic_link.get('email') or '-'}")
    lines.extend(f"next: {next_action}" for next_action in payload.get("next_actions") or [])
    return lines


def _operator_booking_text_lines(payload: dict[str, Any]) -> list[str]:
    action = payload.get("action")
    if action == "config":
        return _operator_booking_config_lines(payload)
    if action == "availability":
        return _operator_booking_availability_lines(payload)
    if action == "book":
        return _operator_booking_book_lines(payload)
    return [str(payload)]


def _print_operator_booking_text(payload: dict[str, Any]) -> None:
    print("\n".join(_operator_booking_text_lines(payload)))


def _handle_operator_booking(
    args: argparse.Namespace,
    root_dir: Path,
    model: dict[str, Any],
    resolved_mode: str,
) -> int:
    try:
        payload, exit_code = operator_booking_payload(
            model,
            action=str(getattr(args, "action", "availability") or "availability"),
            client_id=_primary_client_id(args, model, default=""),
            date=getattr(args, "date", None),
            slot=getattr(args, "slot", None),
            email=getattr(args, "email", None),
            name=getattr(args, "name", None),
            redirect_url=getattr(args, "redirect_url", None),
            origin=getattr(args, "origin", None),
            send_magic_link=bool(getattr(args, "send_magic_link", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            limit=int(getattr(args, "limit", 8) or 8),
            access_token_env=getattr(args, "access_token_env", None),
        )
    except RuntimeError as exc:
        payload = operator_booking_error_payload(exc)
        if args.format == "json":
            emit_json(payload)
        else:
            print(payload["error"]["message"], file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        _print_operator_booking_text(payload)
    return exit_code


_EARLY_DISPATCH: dict[str, Callable[[argparse.Namespace, Path], int]] = {
    "capabilities": _handle_capabilities,
    "robot-docs": _handle_robot_docs,
    "robot-triage": _handle_robot_triage,
    "client-init": _handle_client_init,
    "onboard": _handle_onboard,
    "first-box": _handle_first_box,
    "private-init": _handle_private_init,
    "acceptance": _handle_acceptance,
    "client-project": _handle_client_project,
    "client-open": _handle_client_open,
    "client-publish": _handle_client_publish,
    "client-diff": _handle_client_diff,
    "distribution-publish": _handle_distribution_publish,
    "distribution-preview": _handle_distribution_preview,
    "distribution-rollback": _handle_distribution_rollback,
    "focus": _handle_focus,
    "stewardship-report": _handle_stewardship_report,
    "session-start": _handle_session_start,
    "session-event": _handle_session_event,
    "session-end": _handle_session_end,
    "session-resume": _handle_session_resume,
    "session-status": _handle_session_status,
    "worker-submit": _handle_worker_submit,
    "worker-status": _handle_worker_status,
    "worker-artifacts": _handle_worker_artifacts,
    "worker-promote-learning": _handle_worker_promote_learning,
    "swimmers-launch": _handle_swimmers_launch,
    "mmdx": _handle_mmdx,
}


def _handle_render(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    if args.format == "json":
        emit_json(model)
    else:
        print_render_text(model)
    return EXIT_OK


def _handle_sync(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    actions = sync_runtime(model, dry_run=args.dry_run)
    actions.extend(
        sync_context(
            model,
            root_dir,
            dry_run=args.dry_run,
            context_dir=resolve_context_dir(root_dir, getattr(args, "context_dir", None)),
        )
    )
    if args.format == "json":
        emit_json({"actions": actions, "dry_run": args.dry_run, "next_actions": next_actions_for_sync()})
    else:
        print("\n".join(actions))
    return EXIT_OK


def _handle_context(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    actions = sync_context(
        model,
        root_dir,
        dry_run=args.dry_run,
        context_dir=resolve_context_dir(root_dir, getattr(args, "context_dir", None)),
    )
    if args.format == "json":
        emit_json({"actions": actions, "dry_run": args.dry_run, "next_actions": next_actions_for_context()})
    else:
        print("\n".join(actions))
    return EXIT_OK


def _handle_doctor(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    results = doctor_results(model, root_dir)
    has_fail = any(result.status == "fail" for result in results)
    if args.format == "json":
        emit_json({
            "checks": [asdict(result) for result in results],
            "next_actions": next_actions_for_doctor(results),
        })
    else:
        print_doctor_text(results)
    if has_fail:
        return EXIT_DRIFT
    return EXIT_OK


def _handle_status(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    status_payload = runtime_status(model)
    if args.format == "json":
        status_payload["next_actions"] = next_actions_for_status(status_payload)
        emit_json(compact_runtime_status(status_payload) if args.compact else status_payload)
    else:
        print_status_text(status_payload)
    return EXIT_OK


def _handle_skills(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    payload = collect_skill_visibility(
        model,
        cwd=args.cwd,
        include_global=not args.no_global,
        include_project=not args.no_project,
        include_sources=args.show_sources,
    )
    if args.format == "json":
        emit_json(payload if args.full else compact_skill_visibility_payload(payload))
    else:
        print_skill_visibility_text(
            payload,
            full=args.full,
            show_shadowed=args.show_shadowed,
            issues_only=args.issues_only,
            limit=max(0, int(args.limit)),
        )
    return EXIT_OK


def _handle_skill_audit(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    payload = collect_skill_audit(
        model,
        cwd=args.cwd,
        scan_roots=getattr(args, "scan_root", None),
        max_depth=max(0, int(args.max_depth)),
        include_global=not bool(args.no_global),
        include_clean=bool(getattr(args, "all", False)),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print_skill_audit_text(payload, limit=max(0, int(args.limit)))
    return EXIT_OK


def _handle_mcp_audit(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    payload = collect_mcp_audit(
        root_dir,
        model,
        cwd=args.cwd,
        config_root=getattr(args, "config_root", None),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print_mcp_audit_text(payload, root_dir=root_dir)
    return EXIT_OK


def _handle_parity_report(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del root_dir, resolved_mode
    client_id = _primary_client_id(args, model, default=getattr(args, "client_id", "") or "")
    payload = collect_dev_prod_parity_report(model, client_id=client_id)
    return emit_dev_prod_parity_report(payload, fmt=args.format)


def _handle_skill(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    skill_action = str(args.skill_action)
    dry_run = bool(args.dry_run or skill_action == "plan")
    if (
        not dry_run
        and not bool(getattr(args, "yes", False))
        and (
            skill_action in {"move", "remove", "prune"}
            or (skill_action == "sync" and bool(getattr(args, "prune", False)))
        )
    ):
        raise RuntimeError(
            f"`skill {skill_action}` may unlink existing installs. "
            "Re-run with --dry-run to preview or --yes to apply."
        )
    payload = skill_lifecycle_plan(
        model,
        skill_action,
        skill_name=getattr(args, "skill_name", None),
        cwd=args.cwd,
        to=args.to,
        categories=getattr(args, "category", []) or [],
        source=args.source,
        from_scope=getattr(args, "from_scope", "all"),
        prune=bool(getattr(args, "prune", False)),
        force=bool(args.force),
    )
    payload = apply_skill_lifecycle_plan(
        payload,
        dry_run=dry_run,
        allow_directories=bool(args.allow_directories),
        force=bool(args.force),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print_skill_lifecycle_text(payload)
    if not dry_run:
        problematic = [
            item for item in payload.get("actions") or []
            if str(item.get("status") or "").startswith(("blocked", "conflict", "skipped"))
        ]
        if problematic:
            return EXIT_DRIFT
    return EXIT_OK


def _overlay_action_and_name(args: argparse.Namespace) -> tuple[str, str]:
    action = str(getattr(args, "action", "list"))
    name = str(getattr(args, "name", "") or "").strip()
    if action != "list" and not name:
        raise RuntimeError(
            f"overlay {action}: pass an overlay name, e.g. `overlay {action} marketing`."
        )
    return action, name


def _apply_persistent_overlay_action(action: str, name: str) -> tuple[bool, list[str], bool]:
    was_on = name in active_overlays()
    if action == "on":
        set_overlay(name, True)
    elif action == "off":
        set_overlay(name, False)
    elif action == "toggle":
        toggle_overlay(name)
    current = sorted(active_overlays())
    return was_on, current, name in current


def _overlay_cwd(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "cwd", None) or os.environ.get("PWD") or os.getcwd())


def _overlay_activations(
    args: argparse.Namespace,
    model: dict[str, Any],
    action: str,
    name: str,
    overlay_cwd: Path,
) -> list[dict[str, Any]]:
    if not name or action != "activate":
        return []
    return activate_overlay_scoped_skills(
        model,
        name,
        overlay_cwd,
        to=str(getattr(args, "to", "project")),
        categories=getattr(args, "category", []) or [],
        source=getattr(args, "source", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        force=bool(getattr(args, "force", False)),
    )


def _overlay_removed_links(
    args: argparse.Namespace,
    model: dict[str, Any],
    action: str,
    name: str,
    was_on: bool,
    now_on: bool,
    overlay_cwd: Path,
) -> list[str]:
    should_unlink = (
        name
        and (action == "off" or (action == "toggle" and was_on and not now_on))
        and not now_on
        and not bool(getattr(args, "keep", False))
    )
    if not should_unlink:
        return []
    return unlink_overlay_scoped_skills(
        model,
        name,
        overlay_cwd,
        scope=str(getattr(args, "scope", "project")),
    )


def _overlay_payload(
    args: argparse.Namespace,
    *,
    action: str,
    name: str,
    current: list[str],
    overlay_cwd: Path,
    removed: list[str],
    activations: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "overlays": current,
        "action": action,
        "name": name,
        "cwd": str(overlay_cwd),
        "to": getattr(args, "to", "project"),
        "scope": getattr(args, "scope", "project"),
        "dry_run": bool(getattr(args, "dry_run", False)),
        "persistent": action in {"on", "off", "toggle"},
        "unlinked": removed,
        "activations": activations,
    }


def _print_activation_packet_text(activation: dict[str, Any]) -> None:
    packet = activation.get("activation_packet")
    if not packet:
        print(f"activation packet: {activation.get('skill')} unavailable")
        return
    print(f"activation packet: {packet.get('name')}")
    print(f"source: {packet.get('source')}")
    print(f"skill_md_sha256: {packet.get('skill_md_sha256')}")
    print("skill_md:")
    print(str(packet.get("skill_md") or "").rstrip())


def _print_overlay_text(payload: dict[str, Any]) -> None:
    action = str(payload.get("action") or "list")
    current = list(payload.get("overlays") or [])
    if action == "list":
        print(f"overlays on: {', '.join(current)}" if current else "overlays: (none)")
        return

    name = str(payload.get("name") or "")
    now_on = name in current
    state = "activated" if action == "activate" else ("on" if now_on else "off")
    print(f"overlay {name}: {state}")
    if current:
        print("all on:", ", ".join(current))
    removed = payload.get("unlinked") or []
    activations = payload.get("activations") or []
    if removed:
        print(f"unlinked: {len(removed)} symlinks")
    if activations:
        print(f"activated: {len(activations)} skills")
        for activation in activations:
            _print_activation_packet_text(activation)


def _handle_overlay(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    action, name = _overlay_action_and_name(args)
    was_on, current, now_on = _apply_persistent_overlay_action(action, name)
    overlay_cwd = _overlay_cwd(args)
    activations = _overlay_activations(args, model, action, name, overlay_cwd)
    removed = _overlay_removed_links(args, model, action, name, was_on, now_on, overlay_cwd)
    payload = _overlay_payload(
        args,
        action=action,
        name=name,
        current=current,
        overlay_cwd=overlay_cwd,
        removed=removed,
        activations=activations,
    )
    if args.format == "json":
        emit_json(payload)
    else:
        _print_overlay_text(payload)
    return EXIT_OK


def _handle_bootstrap(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    sync_actions = sync_runtime(model, dry_run=args.dry_run)
    requested_tasks = select_tasks(model, getattr(args, "task", []))
    tasks = resolve_tasks_for_run(model, requested_tasks)
    if not args.dry_run:
        ensure_required_env_files_ready(select_env_files_for_tasks(model, tasks))
    task_results = run_tasks(
        model,
        tasks,
        dry_run=args.dry_run,
    )
    payload: dict[str, Any] = {
        "dry_run": args.dry_run,
        "sync_actions": sync_actions,
        "tasks": task_results,
        "next_actions": next_actions_for_bootstrap(task_results),
    }
    if args.format == "json":
        emit_json(payload)
    else:
        print_service_actions_text(payload)
    return EXIT_OK


def _check_logs_deferred_surfaces(args: argparse.Namespace, model: dict[str, Any], raw_service_ids: list[str]) -> int | None:
    """Return EXIT_ERROR after emitting a structured deferred-surface envelope, else None.

    WG-006: for `logs` we must consult the parity ledger before
    select_services so that unknown-but-declared-deferred surfaces emit
    LOCAL_RUNTIME_SERVICE_DEFERRED instead of "Unknown service id"
    (shared.md:174-177, flows.md Flow 5).
    """
    if args.command != "logs" or not raw_service_ids:
        return None
    logs_classification = classify_requested_surfaces(model, raw_service_ids)
    if not logs_classification["deferred"]:
        return None
    cid_for_logs = _primary_client_id(args, model)
    profile_for_logs = (
        args.profile[0]
        if args.profile
        else (local_runtime_active_profile(model) or "local-minimal")
    )
    surface_id, item = logs_classification["deferred"][0]
    err_payload = build_local_runtime_service_deferred_error(
        item,
        client_id=cid_for_logs,
        profile=profile_for_logs,
        surface_id=surface_id,
    )
    if args.format == "json":
        emit_json(err_payload)
    else:
        print_local_runtime_error_text(err_payload)
    return EXIT_ERROR


def _emit_up_payload(args: argparse.Namespace, payload: dict[str, Any], exit_code: int) -> int:
    if args.format == "json":
        emit_json(payload)
    elif exit_code != EXIT_OK and "error" in payload:
        print_local_runtime_error_text(payload)
    else:
        print_service_actions_text(payload)
    return exit_code


def _primary_client_id(args: argparse.Namespace, model: dict[str, Any], default: str = "personal") -> str:
    requested_clients = [
        str(value).strip()
        for value in (getattr(args, "client", []) or [])
        if str(value).strip()
    ]
    if requested_clients:
        return requested_clients[0]
    active_clients = [
        str(value).strip()
        for value in (model.get("active_clients") or [])
        if str(value).strip()
    ]
    if active_clients:
        return active_clients[0]
    return default


def _handle_local_profile_up(
    args: argparse.Namespace,
    model: dict[str, Any],
    resolved_mode: str,
    active_local_profile: str,
) -> int:
    client_id_for_up = _primary_client_id(args, model)
    service_filter = [s for s in (getattr(args, "service", []) or []) if s]
    up_exit, up_payload = run_up(
        model=model,
        client_id=client_id_for_up,
        profile=active_local_profile,
        requested_mode=resolved_mode,
        service_filter=service_filter,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
    )
    return _emit_up_payload(args, up_payload, up_exit)


def _bridge_missing_payload(args: argparse.Namespace, bridge: dict[str, Any]) -> dict[str, Any]:
    profile_hint = f" --profile {' --profile '.join(args.profile)}" if args.profile else " --profile local-minimal"
    return local_runtime_error(
        "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
        f"Bridge {bridge['id']} outputs missing. Run 'manage.py focus' first.",
        recoverable=True,
        next_action=f"manage.py focus {args.client[0] if args.client else 'personal'}{profile_hint}",
    )


def _emit_missing_bridge_if_needed(args: argparse.Namespace, model: dict[str, Any]) -> int | None:
    bridges = model.get("bridges") or []
    if not bridges or args.dry_run:
        return None
    for bridge in bridges:
        if bridge_outputs_state(bridge)["state"] != "missing":
            continue
        return _emit_up_payload(args, _bridge_missing_payload(args, bridge), EXIT_ERROR)
    return None


def _legacy_up_payload(
    args: argparse.Namespace,
    model: dict[str, Any],
    requested_services: list[dict[str, Any]],
    resolved_mode: str,
) -> dict[str, Any]:
    sync_actions = sync_runtime(model, dry_run=args.dry_run)
    services = resolve_services_for_start(model, requested_services)
    bootstrap_tasks = resolve_tasks_for_services(model, services)
    if not args.dry_run:
        ensure_required_env_files_ready(
            select_env_files_for_tasks(model, bootstrap_tasks) + select_env_files_for_services(model, services)
        )
    task_results = run_tasks(model, bootstrap_tasks, dry_run=args.dry_run, mode=resolved_mode)
    service_results = start_services(
        model,
        services,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        mode=resolved_mode,
    )
    return {
        "dry_run": args.dry_run,
        "requested_mode": resolved_mode,
        "effective_mode": resolved_mode,
        "sync_actions": sync_actions,
        "bootstrap_tasks": task_results,
        "services": service_results,
        "next_actions": next_actions_for_up(service_results),
    }


def _handle_up(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    # Local-runtime profiles use workflows.run_up; legacy profiles keep the
    # inline path for compatibility with existing lifecycle behavior.
    active_local_profile = local_runtime_active_profile(model)
    if active_local_profile:
        return _handle_local_profile_up(args, model, resolved_mode, active_local_profile)

    raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
    requested_services = select_services(model, raw_service_ids)
    deferred_exit = _emit_missing_bridge_if_needed(args, model)
    if deferred_exit is not None:
        return deferred_exit
    return _emit_up_payload(
        args,
        _legacy_up_payload(args, model, requested_services, resolved_mode),
        EXIT_OK,
    )


def _handle_down(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
    requested_services = select_services(model, raw_service_ids)
    services = resolve_services_for_stop(model, requested_services)
    service_results = stop_services(
        model,
        services,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
    )
    payload = {
        "dry_run": args.dry_run,
        "services": service_results,
        "next_actions": next_actions_for_down(),
    }
    if args.format == "json":
        emit_json(payload)
    else:
        print_service_actions_text(payload)
    return EXIT_OK


def _handle_restart(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
    requested_services = select_services(model, raw_service_ids)
    stop_targets = resolve_services_for_stop(model, requested_services)
    start_targets = resolve_services_for_start(model, stop_targets)
    bootstrap_tasks = resolve_tasks_for_services(model, start_targets)
    stop_results = stop_services(
        model,
        stop_targets,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
    )
    sync_actions = sync_runtime(model, dry_run=args.dry_run)
    if not args.dry_run:
        ensure_required_env_files_ready(
            select_env_files_for_tasks(model, bootstrap_tasks) + select_env_files_for_services(model, start_targets)
        )
    task_results = run_tasks(
        model,
        bootstrap_tasks,
        dry_run=args.dry_run,
    )
    start_results = start_services(
        model,
        start_targets,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
    )
    payload = {
        "dry_run": args.dry_run,
        "stop_services": stop_results,
        "sync_actions": sync_actions,
        "bootstrap_tasks": task_results,
        "start_services": start_results,
        "next_actions": next_actions_for_up(start_results),
    }
    if args.format == "json":
        emit_json(payload)
    else:
        print("stop:")
        print_service_actions_text({"services": stop_results})
        print()
        print_service_actions_text({"sync_actions": sync_actions, "tasks": task_results, "services": start_results})
    return EXIT_OK


def _handle_logs(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
    requested_services = select_services(model, raw_service_ids)
    logs_payload: dict[str, Any] = {
        "services": collect_service_logs(
            model,
            requested_services,
            line_count=max(0, int(args.lines)),
        ),
        "next_actions": ["status --format json"],
    }
    if args.format == "json":
        emit_json(logs_payload)
    else:
        print_service_logs_text(logs_payload)
    return EXIT_OK


_MODEL_DISPATCH: dict[str, Callable[[argparse.Namespace, Path, dict[str, Any], str], int]] = {
    "render": _handle_render,
    "sync": _handle_sync,
    "context": _handle_context,
    "doctor": _handle_doctor,
    "status": _handle_status,
    "skills": _handle_skills,
    "skill-audit": _handle_skill_audit,
    "mcp-audit": _handle_mcp_audit,
    "parity-report": _handle_parity_report,
    "skill": _handle_skill,
    "overlay": _handle_overlay,
    "operator-booking": _handle_operator_booking,
    "bootstrap": _handle_bootstrap,
    "up": _handle_up,
    "down": _handle_down,
    "restart": _handle_restart,
    "logs": _handle_logs,
}


def _emit_mode_error(args: argparse.Namespace, mode_error: dict[str, Any]) -> int:
    if getattr(args, "format", "text") == "json":
        emit_json(mode_error)
    else:
        print_local_runtime_error_text(mode_error)
    return EXIT_ERROR


def _emit_main_exception(args: argparse.Namespace, exc: Exception) -> int:
    if isinstance(exc, RuntimeError):
        payload_error = exc
    else:
        payload_error = RuntimeError(f"Unexpected error: {exc}")
    if args.format == "json":
        emit_json(classify_error(payload_error, args.command))
    elif isinstance(exc, RuntimeError):
        print(str(exc), file=sys.stderr)
    else:
        import traceback
        traceback.print_exc()
    return EXIT_ERROR


def _active_clients_for_args(args: argparse.Namespace, model: dict[str, Any]) -> list[str]:
    requested_clients = getattr(args, "client", [])
    positional_client = str(getattr(args, "client_id", "") or "").strip()
    if args.command == "parity-report" and positional_client and not requested_clients:
        requested_clients = [positional_client]
    active_clients = normalize_active_clients(model, requested_clients)
    cwd_inferred_commands = {
        "skills",
        "skill",
        "overlay",
        "mcp-audit",
        "parity-report",
        "bootstrap",
        "up",
        "down",
        "restart",
        "status",
        "logs",
        "operator-booking",
    }
    if args.command not in cwd_inferred_commands or requested_clients:
        return active_clients
    raw_cwd = getattr(args, "cwd", None)
    if args.command not in {"skills", "skill", "overlay", "mcp-audit"} and not raw_cwd:
        return active_clients
    skill_cwd = Path(raw_cwd or os.getcwd())
    matches = matched_skill_clients(model, skill_cwd)
    if not matches:
        return active_clients
    return normalize_active_clients(model, [matches[0]["id"]])


def _filtered_model_for_args(args: argparse.Namespace, root_dir: Path) -> dict[str, Any]:
    model = build_runtime_model(root_dir)
    active_profiles = normalize_active_profiles(getattr(args, "profile", []))
    return filter_model(model, active_profiles, _active_clients_for_args(args, model))


def _dispatch_model_command(
    args: argparse.Namespace,
    root_dir: Path,
    resolved_mode: str,
    model_handler: Callable[[argparse.Namespace, Path, dict[str, Any], str], int],
) -> int:
    try:
        model = _filtered_model_for_args(args, root_dir)
        raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
        deferred_exit = _check_logs_deferred_surfaces(args, model, raw_service_ids)
        if deferred_exit is not None:
            return deferred_exit
        return model_handler(args, root_dir, model, resolved_mode)
    except Exception as exc:
        return _emit_main_exception(args, exc)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    normalized_argv, diagnostics = _normalize_agent_argv(argv)
    args = parser.parse_args(normalized_argv)
    for diagnostic in diagnostics:
        print(diagnostic, file=sys.stderr)
    if getattr(args, "robot_triage", False):
        args.command = "robot-triage"
        args.format = "json"
    _apply_default_command(args)
    root_dir = resolve_root_dir(args.root_dir)

    resolved_mode, mode_error = _resolve_local_runtime_mode(args)
    if mode_error is not None:
        return _emit_mode_error(args, mode_error)

    early_handler = _EARLY_DISPATCH.get(args.command)
    if early_handler is not None:
        return early_handler(args, root_dir)

    model_handler = _MODEL_DISPATCH.get(args.command)
    if model_handler is None:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return EXIT_ERROR
    return _dispatch_model_command(args, root_dir, resolved_mode, model_handler)
