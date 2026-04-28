from __future__ import annotations

import os
from pathlib import Path

from .shared import *
from .validation import *
from .publish import *
from .runtime_ops import *
from .skill_visibility import *
from .context_rendering import *
from .text_renderers import *
from .workflows import *

def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the internal skillbox runtime graph.")
    parser.add_argument(
        "--root-dir",
        default=None,
        help="Override the repo root for testing or embedding.",
    )
    subparsers = parser.add_subparsers(dest="command")

    def add_profile_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--profile",
            action="append",
            default=[],
            help="Activate a runtime profile. Can be repeated. Selecting any profile also includes `core`.",
        )

    def add_client_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--client",
            action="append",
            default=[],
            help="Activate a runtime client overlay. Can be repeated.",
        )

    def add_service_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--service",
            action="append",
            default=[],
            help="Limit the command to one or more declared service ids. Can be repeated.",
        )

    def add_task_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--task",
            action="append",
            default=[],
            help="Limit the command to one or more declared task ids. Can be repeated.",
        )

    def add_context_dir_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--context-dir",
            default=None,
            help=(
                "Write CLAUDE.md and AGENTS.md into this directory instead of the mounted "
                "home/.claude and home/.codex roots. Path is resolved relative to the repo root."
            ),
        )

    render_parser = subparsers.add_parser("render", help="Print the resolved runtime graph.")
    render_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(render_parser)
    add_client_arg(render_parser)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Create managed runtime directories, repos, artifacts, and installed skill state.",
    )
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(sync_parser)
    add_client_arg(sync_parser)
    add_context_dir_arg(sync_parser)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate runtime graph, filesystem readiness, and installed skill integrity.",
    )
    doctor_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(doctor_parser)
    add_client_arg(doctor_parser)

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
    add_profile_arg(status_parser)
    add_client_arg(status_parser)

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
    add_profile_arg(skills_parser)
    add_client_arg(skills_parser)

    skill_parser = subparsers.add_parser(
        "skill",
        help="Plan and apply skill installs, moves, removals, and policy syncs.",
    )
    skill_subparsers = skill_parser.add_subparsers(dest="skill_action", required=True)

    def add_skill_lifecycle_common(command_parser: argparse.ArgumentParser) -> None:
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
        add_profile_arg(command_parser)
        add_client_arg(command_parser)

    for action_name, help_text in (
        ("plan", "Preview where a skill would be installed."),
        ("add", "Install/link a skill into the selected global, project, or category scope."),
        ("move", "Install/link a skill into a new scope and remove old installs for that skill."),
    ):
        action_parser = skill_subparsers.add_parser(action_name, help=help_text)
        action_parser.add_argument("skill_name")
        add_skill_lifecycle_common(action_parser)

    remove_parser = skill_subparsers.add_parser("remove", help="Remove installed links/files for a skill.")
    remove_parser.add_argument("skill_name")
    add_skill_lifecycle_common(remove_parser)
    remove_parser.add_argument(
        "--from",
        dest="from_scope",
        choices=("global", "project", "all"),
        default="all",
        help="Installed scope to remove from.",
    )

    prune_parser = skill_subparsers.add_parser("prune", help="Remove installed skills that violate skill-scope policy.")
    add_skill_lifecycle_common(prune_parser)

    sync_skills_parser = skill_subparsers.add_parser(
        "sync",
        help="Install/link a named skill or all literal skills missing for the current cwd policy.",
    )
    sync_skills_parser.add_argument("skill_name", nargs="?")
    add_skill_lifecycle_common(sync_skills_parser)
    sync_skills_parser.add_argument(
        "--prune",
        action="store_true",
        help="Also unlink existing policy violations after installing missing skills.",
    )

    overlay_parser = subparsers.add_parser(
        "overlay",
        help="List, enable, disable, or toggle skill scope overlays (e.g. marketing).",
    )
    overlay_parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=("list", "on", "off", "toggle"),
        help="list (default), on, off, or toggle.",
    )
    overlay_parser.add_argument("name", nargs="?", help="Overlay name, e.g. marketing.")
    overlay_parser.add_argument("--cwd", default=None, help="Target cwd for scoped unlinks. Defaults to $PWD.")
    overlay_parser.add_argument(
        "--keep",
        action="store_true",
        help="When turning an overlay off, keep existing symlinks. Default is to unlink overlay-scoped symlinks from the cwd and agent homes.",
    )
    overlay_parser.add_argument("--format", choices=("text", "json"), default="text")

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Sync runtime state and run one-shot bootstrap tasks for the active scope.",
    )
    bootstrap_parser.add_argument("--dry-run", action="store_true")
    bootstrap_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(bootstrap_parser)
    add_client_arg(bootstrap_parser)
    add_task_arg(bootstrap_parser)

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
    add_profile_arg(up_parser)
    add_client_arg(up_parser)
    add_service_arg(up_parser)

    down_parser = subparsers.add_parser(
        "down",
        help="Stop manageable services for the active scope.",
    )
    down_parser.add_argument("--dry-run", action="store_true")
    down_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_STOP_WAIT_SECONDS)
    down_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(down_parser)
    add_client_arg(down_parser)
    add_service_arg(down_parser)

    restart_parser = subparsers.add_parser(
        "restart",
        help="Restart manageable services for the active scope.",
    )
    restart_parser.add_argument("--dry-run", action="store_true")
    restart_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS)
    restart_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(restart_parser)
    add_client_arg(restart_parser)
    add_service_arg(restart_parser)

    logs_parser = subparsers.add_parser(
        "logs",
        help="Show recent logs for declared services in the active scope.",
    )
    logs_parser.add_argument("--lines", type=int, default=DEFAULT_LOG_TAIL_LINES)
    logs_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(logs_parser)
    add_client_arg(logs_parser)
    add_service_arg(logs_parser)

    context_parser = subparsers.add_parser(
        "context",
        help="Generate CLAUDE.md and AGENTS.md from the resolved runtime graph.",
    )
    context_parser.add_argument("--dry-run", action="store_true")
    context_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(context_parser)
    add_client_arg(context_parser)
    add_context_dir_arg(context_parser)

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
    add_profile_arg(client_project_parser)

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
    add_profile_arg(client_open_parser)

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
    add_profile_arg(client_publish_parser)

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
    add_profile_arg(client_diff_parser)

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
    add_profile_arg(first_box_parser)

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
    add_profile_arg(focus_parser)
    # --client accepted for CLI consistency with up/status/logs/doctor
    # (local_runtime_core_cutover WG-003). The positional client_id remains
    # the primary input; --client is honored when no positional is given.
    add_client_arg(focus_parser)
    add_service_arg(focus_parser)
    add_context_dir_arg(focus_parser)

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
    add_profile_arg(acceptance_parser)

    args = parser.parse_args()
    if args.command is None:
        args.command = "status"
        args.format = "text"
        args.compact = False
        args.profile = []
        args.client = []
    root_dir = resolve_root_dir(args.root_dir)

    # --- WG-003: --mode selector validation ---------------------------------
    # Validate the local-runtime startup mode BEFORE any mutation runs.
    # Unknown values must emit a structured LOCAL_RUNTIME_MODE_UNSUPPORTED
    # envelope (shared.md:457-469) and exit 1 during CLI arg validation,
    # not during lifecycle execution. Only the `up` surface accepts --mode
    # today; other surfaces ignore it. Mode is orthogonal to --profile and
    # --service (shared.md:518-520, Business Rule 2).
    requested_mode_raw = getattr(args, "mode", None)
    default_mode = "reuse"
    if requested_mode_raw is None or str(requested_mode_raw).strip() == "":
        resolved_mode = default_mode
        mode_was_explicit = False
    else:
        resolved_mode = str(requested_mode_raw).strip()
        mode_was_explicit = True

    if mode_was_explicit and resolved_mode not in LOCAL_RUNTIME_START_MODES:
        supported = ", ".join(LOCAL_RUNTIME_START_MODES)
        err = local_runtime_error(
            LOCAL_RUNTIME_MODE_UNSUPPORTED,
            f"Unsupported --mode value {resolved_mode!r}. Supported modes: {supported}.",
            recoverable=True,
            next_action=f"Re-run with --mode <{'|'.join(LOCAL_RUNTIME_START_MODES)}>.",
        )
        err["error"]["requested_mode"] = resolved_mode
        fmt = getattr(args, "format", "text")
        if fmt == "json":
            emit_json(err)
        else:
            print_local_runtime_error_text(err)
        return EXIT_ERROR

    if args.command == "client-init":
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

    if args.command == "onboard":
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

    if args.command == "first-box":
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

    if args.command == "private-init":
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

    if args.command == "acceptance":
        return run_acceptance(
            root_dir=root_dir,
            client_id=args.client_id,
            profiles=args.profile,
            wait_seconds=max(0.0, float(args.wait_seconds)),
            fmt=args.format,
        )

    if args.command == "client-project":
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

    if args.command == "client-open":
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

    if args.command == "client-publish":
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

    if args.command == "client-diff":
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

    if args.command == "focus":
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

    if args.command == "session-start":
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

    if args.command == "session-event":
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

    if args.command == "session-end":
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

    if args.command == "session-resume":
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

    if args.command == "session-status":
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

    try:
        model = build_runtime_model(root_dir)
        active_profiles = normalize_active_profiles(getattr(args, "profile", []))
        requested_clients = getattr(args, "client", [])
        active_clients = normalize_active_clients(model, requested_clients)
        if args.command in {"skills", "skill"} and not requested_clients:
            skill_cwd = Path(getattr(args, "cwd", None) or os.getcwd())
            matches = matched_skill_clients(model, skill_cwd)
            if matches:
                active_clients = normalize_active_clients(model, [matches[0]["id"]])
        model = filter_model(model, active_profiles, active_clients)

        if args.command == "render":
            if args.format == "json":
                emit_json(model)
            else:
                print_render_text(model)
            return EXIT_OK

        if args.command == "sync":
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

        if args.command == "context":
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

        if args.command == "doctor":
            results = doctor_results(model, root_dir)
            has_fail = any(result.status == "fail" for result in results)
            has_warn = any(result.status == "warn" for result in results)
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

        if args.command == "status":
            status_payload = runtime_status(model)
            if args.format == "json":
                status_payload["next_actions"] = next_actions_for_status(status_payload)
                emit_json(compact_runtime_status(status_payload) if args.compact else status_payload)
            else:
                print_status_text(status_payload)
            return EXIT_OK

        if args.command == "skills":
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

        if args.command == "skill":
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

        if args.command == "overlay":
            action = str(getattr(args, "action", "list"))
            name = str(getattr(args, "name", "") or "").strip()
            if action != "list" and not name:
                raise RuntimeError(
                    f"overlay {action}: pass an overlay name, e.g. `overlay {action} marketing`."
                )
            was_on = name in active_overlays()
            if action == "on":
                set_overlay(name, True)
            elif action == "off":
                set_overlay(name, False)
            elif action == "toggle":
                toggle_overlay(name)
            current = sorted(active_overlays())
            now_on = name in current
            removed: list[str] = []
            if (
                name
                and was_on
                and not now_on
                and not bool(getattr(args, "keep", False))
            ):
                overlay_cwd = Path(
                    getattr(args, "cwd", None) or os.environ.get("PWD") or os.getcwd()
                )
                removed = unlink_overlay_scoped_skills(model, name, overlay_cwd)
            if args.format == "json":
                emit_json({
                    "overlays": current,
                    "action": action,
                    "name": name,
                    "unlinked": removed,
                })
            else:
                if action == "list":
                    if current:
                        print("overlays on:", ", ".join(current))
                    else:
                        print("overlays: (none)")
                else:
                    state = "on" if now_on else "off"
                    print(f"overlay {name}: {state}")
                    if current:
                        print("all on:", ", ".join(current))
                    if removed:
                        print(f"unlinked: {len(removed)} symlinks")
            return EXIT_OK

        if args.command == "bootstrap":
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

        # WG-006: for logs we must consult the parity ledger BEFORE
        # select_services -- unknown-but-declared-deferred surfaces would
        # otherwise raise "Unknown service id" and miss the
        # LOCAL_RUNTIME_SERVICE_DEFERRED contract (shared.md:174-177,
        # flows.md Flow 5).  We split requested ids into (covered, deferred,
        # unknown); if any are deferred we short-circuit.
        raw_service_ids = [
            s for s in (getattr(args, "service", []) or []) if s
        ]
        if args.command == "logs" and raw_service_ids:
            logs_classification = classify_requested_surfaces(
                model, raw_service_ids
            )
            if logs_classification["deferred"]:
                cid_for_logs = args.client[0] if args.client else "personal"
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

        requested_services = select_services(model, raw_service_ids)

        if args.command == "up":
            # WG-006: route local-runtime profiles through workflows.run_up
            # so the parity ledger, bridge reconciliation, mode validation,
            # bootstrap tasks, and service start all run through a single
            # contract-aware path.  Non-local profiles keep using the legacy
            # inline flow to preserve existing lifecycle test coverage.
            active_local_profile = local_runtime_active_profile(model)
            if active_local_profile:
                client_id_for_up = args.client[0] if args.client else "personal"
                service_filter = [
                    s for s in (getattr(args, "service", []) or []) if s
                ]
                up_exit, up_payload = run_up(
                    model=model,
                    client_id=client_id_for_up,
                    profile=active_local_profile,
                    requested_mode=resolved_mode,
                    service_filter=service_filter,
                    dry_run=args.dry_run,
                    wait_seconds=max(0.0, float(args.wait_seconds)),
                )
                if args.format == "json":
                    emit_json(up_payload)
                else:
                    if up_exit != EXIT_OK and "error" in up_payload:
                        print_local_runtime_error_text(up_payload)
                    else:
                        print_service_actions_text(up_payload)
                return up_exit

            # --- Legacy (non local-runtime) path ---------------------------
            # WG-006 keeps the pre-existing inline up pipeline alive for
            # profiles that are not part of the local_runtime_core_cutover
            # contract.  Parity-ledger classification still runs above via
            # the shared intercept.
            bridges = model.get("bridges") or []
            if bridges and not args.dry_run:
                for bridge in bridges:
                    state = bridge_outputs_state(bridge)
                    if state["state"] == "missing":
                        err = local_runtime_error(
                            "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
                            f"Bridge {bridge['id']} outputs missing. Run 'manage.py focus' first.",
                            recoverable=True,
                            next_action=f"manage.py focus {args.client[0] if args.client else 'personal'} --profile {' --profile '.join(args.profile) if args.profile else 'local-minimal'}",
                        )
                        if args.format == "json":
                            emit_json(err)
                        else:
                            print_local_runtime_error_text(err)
                        return EXIT_ERROR

            sync_actions = sync_runtime(model, dry_run=args.dry_run)
            services = resolve_services_for_start(model, requested_services)
            bootstrap_tasks = resolve_tasks_for_services(model, services)
            if not args.dry_run:
                ensure_required_env_files_ready(
                    select_env_files_for_tasks(model, bootstrap_tasks) + select_env_files_for_services(model, services)
                )
            task_results = run_tasks(
                model,
                bootstrap_tasks,
                dry_run=args.dry_run,
                mode=resolved_mode,
            )
            service_results = start_services(
                model,
                services,
                dry_run=args.dry_run,
                wait_seconds=max(0.0, float(args.wait_seconds)),
                mode=resolved_mode,
            )
            effective_mode = resolved_mode
            payload = {
                "dry_run": args.dry_run,
                "requested_mode": resolved_mode,
                "effective_mode": effective_mode,
                "sync_actions": sync_actions,
                "bootstrap_tasks": task_results,
                "services": service_results,
                "next_actions": next_actions_for_up(service_results),
            }
            if args.format == "json":
                emit_json(payload)
            else:
                print_service_actions_text(payload)
            return EXIT_OK

        if args.command == "down":
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

        if args.command == "restart":
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
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, args.command))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        if args.format == "json":
            emit_json(classify_error(RuntimeError(f"Unexpected error: {exc}"), args.command))
        else:
            import traceback
            traceback.print_exc()
        return EXIT_ERROR
