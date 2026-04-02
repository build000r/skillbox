from __future__ import annotations

from .shared import *
from .validation import *
from .publish import *
from .runtime_ops import *
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
    subparsers = parser.add_subparsers(dest="command", required=True)

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
    add_profile_arg(status_parser)
    add_client_arg(status_parser)

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
        help="Runtime path for the client root. Defaults to ${SKILLBOX_MONOSERVER_ROOT}/<client-id>.",
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
        help="Runtime path for the client root. Defaults to ${SKILLBOX_MONOSERVER_ROOT}/<client-id>.",
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
        help="Runtime path for the client root when scaffolding. Defaults to ${SKILLBOX_MONOSERVER_ROOT}/<client-id>.",
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
    add_service_arg(focus_parser)
    add_context_dir_arg(focus_parser)

    acceptance_parser = subparsers.add_parser(
        "acceptance",
        help="Run the first-box readiness gate: doctor-pre, sync, focus, mcp-smoke, doctor-post.",
    )
    acceptance_parser.add_argument(
        "client_id",
        help="Existing client slug to validate for first-box acceptance (for example `personal`).",
    )
    acceptance_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(acceptance_parser)

    ack_parser = subparsers.add_parser(
        "ack",
        help="Acknowledge journal events to remove them from active context.",
    )
    ack_parser.add_argument(
        "--type", default=None, dest="event_type",
        help="Ack events of this type (e.g. pulse.service_restarted).",
    )
    ack_parser.add_argument(
        "--subject", default=None,
        help="Ack events with this subject (e.g. a service or client ID).",
    )
    ack_parser.add_argument(
        "--ts", type=float, default=None,
        help="Ack a specific event by its exact timestamp.",
    )
    ack_parser.add_argument(
        "--all", action="store_true", dest="ack_all",
        help="Ack all unacked events.",
    )
    ack_parser.add_argument("--reason", default="", help="Why this was acknowledged.")
    ack_parser.add_argument(
        "--list", action="store_true", dest="list_acks",
        help="List current acks instead of creating new ones.",
    )
    ack_parser.add_argument(
        "--prune", action="store_true",
        help="Remove expired acks from the store.",
    )
    ack_parser.add_argument("--format", choices=("text", "json"), default="text")

    args = parser.parse_args()
    root_dir = resolve_root_dir(args.root_dir)

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

    if args.command == "ack":
        if args.list_acks:
            ack_data = read_acks(root_dir)
            if args.format == "json":
                emit_json({"acks": ack_data, "count": len(ack_data)})
            else:
                if not ack_data:
                    print("No active acks.")
                else:
                    for key, entry in ack_data.items():
                        age = time.time() - entry.get("at", 0)
                        age_str = f"{int(age / 3600)}h ago" if age >= 3600 else f"{int(age / 60)}m ago"
                        reason = entry.get("reason", "")
                        reason_str = f" — {reason}" if reason else ""
                        print(f"  ts={key} acked {age_str}{reason_str}")
            return EXIT_OK

        if args.prune:
            pruned = prune_expired_acks(root_dir)
            if args.format == "json":
                emit_json({"pruned": pruned})
            else:
                print(f"Pruned {pruned} expired acks.")
            return EXIT_OK

        if not args.event_type and not args.subject and args.ts is None and not args.ack_all:
            print("ack requires --type, --subject, --ts, or --all.", file=sys.stderr)
            return EXIT_ERROR

        acked_items = ack_events(
            root_dir,
            event_type=args.event_type,
            subject=args.subject,
            ts=args.ts,
            ack_all=args.ack_all,
            reason=args.reason,
        )
        if args.format == "json":
            emit_json({"acked": acked_items, "count": len(acked_items), "next_actions": ["status --format json"]})
        else:
            if acked_items:
                for item in acked_items:
                    print(f"  acked: {item['type']} {item['subject']}")
                print(f"\n{len(acked_items)} events acknowledged.")
            else:
                print("No matching events to ack.")
        return EXIT_OK

    model = build_runtime_model(root_dir)
    active_profiles = normalize_active_profiles(getattr(args, "profile", []))
    active_clients = normalize_active_clients(model, getattr(args, "client", []))
    model = filter_model(model, active_profiles, active_clients)

    try:
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
                emit_json(status_payload)
            else:
                print_status_text(status_payload)
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

        requested_services = select_services(model, getattr(args, "service", []))

        if args.command == "up":
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
            )
            service_results = start_services(
                model,
                services,
                dry_run=args.dry_run,
                wait_seconds=max(0.0, float(args.wait_seconds)),
            )
            payload = {
                "dry_run": args.dry_run,
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
