from __future__ import annotations

from .shared import *
from .runtime_ops import *
from .distribution.status import render_connected_distributors_section


def _context_clients_data(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(c.get("id", "")): c
        for c in model.get("clients") or []
    }


def _context_make_suffix(active_clients: list[str]) -> str:
    make_parts: list[str] = []
    if active_clients:
        make_parts.append(f"CLIENT={active_clients[0]}")
    return " " + " ".join(make_parts) if make_parts else ""


def _context_default_cwd(active_clients: list[str], clients_data: dict[str, dict[str, Any]]) -> str:
    for client_id in active_clients:
        client_data = clients_data.get(client_id, {})
        cwd = str(client_data.get("default_cwd", "")).strip()
        if cwd:
            return cwd
    return ""


def _context_header_lines(make_suffix: str) -> list[str]:
    regen_cmd = f"make context{make_suffix}"
    sync_cmd = f"make runtime-sync{make_suffix}"
    return [
        "# skillbox",
        "",
        "> Auto-generated from the runtime graph. Do not edit manually.",
        f"> Regenerate: `{regen_cmd}` or `{sync_cmd}`.",
        "",
        "You are inside a skillbox workspace container.",
        "",
    ]


def _context_environment_lines(
    model: dict[str, Any],
    active_clients: list[str],
    active_profiles: list[str],
    clients_data: dict[str, dict[str, Any]],
) -> list[str]:
    lines = ["## Environment", ""]
    default_cwd = _context_default_cwd(active_clients, clients_data)
    if active_clients:
        lines.append(f"- Client: **{', '.join(active_clients)}**")
    if default_cwd:
        lines.append(f"- Default CWD: `{default_cwd}`")
    if active_profiles:
        lines.append(f"- Profiles: {', '.join(active_profiles)}")

    runtime_env = model.get("env") or {}
    for cid_env in active_clients:
        client_data_env = clients_data.get(cid_env, {})
        if client_data_env.get("context"):
            ctx_path = client_config_runtime_dir(runtime_env, cid_env) / "context.yaml"
            lines.append(f"- Skill context: `$SKILLBOX_CLIENT_CONTEXT` → `{ctx_path}`")
            break
    lines.append("")
    return lines


def _context_tooling_lines() -> list[str]:
    return [
        "## Tooling Guidance",
        "",
        "- Start agent navigation with `python3 .env-manager/manage.py capabilities --json`, "
        "then `python3 .env-manager/manage.py next --format json`.",
        "- GitHub: use `gh-axi` for GitHub operations when available; "
        "fall back to `gh` only when `gh-axi` cannot satisfy the task.",
        "",
    ]


def _context_pressure_lines(model: dict[str, Any]) -> list[str]:
    root_dir = Path(str(model.get("root_dir") or DEFAULT_ROOT_DIR))
    advisory = runtime_pressure_advisory(root_dir)
    if not advisory.get("ok", True):
        return [
            "## Pressure And Offload Policy",
            "",
            "- Pressure advisory could not be collected; run `python3 .env-manager/manage.py pressure-report --format json`.",
            "- Do not delete files, install hooks, or mutate ballast from this context alone.",
            "",
        ]
    disk = advisory.get("local_disk") or {}
    target = advisory.get("target_worker") or {}
    rch = advisory.get("rch") or {}
    sbh = advisory.get("sbh") or {}
    protected = ", ".join(
        f"{entry.get('id')} `{entry.get('path')}`"
        for entry in advisory.get("protected_paths") or []
    ) or "none declared"
    excluded = ", ".join(str(item) for item in target.get("excluded_box_ids") or []) or "none"
    lines = [
        "## Pressure And Offload Policy",
        "",
        (
            f"- Local disk: {disk.get('free_gib')}GiB free "
            f"({disk.get('free_percent')}%, {disk.get('pressure_level')})."
        ),
        (
            f"- Build worker target: `{target.get('id')}` "
            f"state={target.get('state') or 'unknown'} "
            f"tailscale={target.get('tailscale_hostname') or target.get('tailscale_ip') or 'unknown'}; "
            f"excluded targets: {excluded}."
        ),
        (
            f"- RCH: state={rch.get('state')} worker={rch.get('worker_state')} "
            f"fail-open={rch.get('fail_open_expected')} hook-install-allowed={rch.get('hook_install_allowed')}."
        ),
        (
            f"- SBH: state={sbh.get('state')} daemon={sbh.get('daemon_state')} "
            f"auto-delete={sbh.get('auto_delete_allowed')} ballast-mutation={sbh.get('ballast_mutation_allowed')}."
        ),
        f"- Protected no-touch paths: {protected}.",
        "- Safe first commands: `python3 .env-manager/manage.py pressure-report --format json`; "
        "`python3 .env-manager/manage.py rch-report --format json`; "
        "`python3 .env-manager/manage.py sbh-report --format json`.",
        "- Do not run cleanup, hook installation, service installation, protect writes, or ballast mutation without explicit approval.",
        "",
    ]
    warnings = advisory.get("warnings") or []
    if warnings:
        lines.insert(-1, "- Current warnings: " + " ".join(str(warning) for warning in warnings))
    return lines


def _context_repo_lines(model: dict[str, Any]) -> list[str]:
    repos = model.get("repos") or []
    if not repos:
        return []
    lines = [
        "## Repos",
        "",
        "| ID | Path | Kind | Project | Command Lanes |",
        "|----|------|------|---------|---------------|",
    ]
    for repo in repos:
        project_kind = str(repo.get("project_kind") or "-")
        command_lanes = repo.get("command_lanes") or {}
        lane_names = ", ".join(str(lane_id) for lane_id in command_lanes) if isinstance(command_lanes, dict) else "-"
        lines.append(
            f"| {repo['id']} | `{repo['path']}` | {repo.get('kind', 'repo')} | "
            f"{project_kind} | {lane_names or '-'} |"
        )
    lines.append("")
    return lines


def _context_service_make_suffix(make_suffix: str, profiles: list[str], sid: str) -> str:
    svc_parts = [part for part in make_suffix.strip().split(" ") if part]
    non_core = [profile for profile in profiles if profile != "core"]
    if non_core:
        svc_parts.append(f"PROFILE={non_core[0]}")
    svc_parts.append(f"SERVICE={sid}")
    return " " + " ".join(svc_parts)


def _context_service_lines(model: dict[str, Any], make_suffix: str) -> list[str]:
    services = model.get("services") or []
    if not services:
        return []
    lines = ["## Services", ""]
    for service in services:
        sid = service["id"]
        kind = service.get("kind", "service")
        profiles = service.get("profiles") or []
        profile_label = ", ".join(profiles) or "core"
        manageable, reason = service_supports_lifecycle(service)
        if not manageable:
            lines.append(f"- **{sid}** ({kind}, {profile_label})" f" — {reason or 'not manageable'}")
            continue
        deps = service_dependency_ids(service)
        dep_note = f" (depends on: {', '.join(deps)})" if deps else ""
        svc_suffix = _context_service_make_suffix(make_suffix, profiles, sid)
        lines.append(f"- **{sid}** ({kind}, {profile_label}){dep_note}")
        lines.append(f"  - Start: `make runtime-up{svc_suffix}`")
        lines.append(f"  - Stop: `make runtime-down{svc_suffix}`")
        lines.append(f"  - Logs: `make runtime-logs{svc_suffix}`")
    lines.append("")
    return lines


def _context_task_lines(model: dict[str, Any], make_suffix: str) -> list[str]:
    tasks = model.get("tasks") or []
    if not tasks:
        return []
    lines = ["## Tasks", ""]
    for task in tasks:
        tid = task["id"]
        deps = task_dependency_ids(task)
        dep_note = f" (depends on: {', '.join(deps)})" if deps else ""
        task_suffix = f"{make_suffix} TASK={tid}" if make_suffix else f" TASK={tid}"
        lines.append(f"- **{tid}**{dep_note}: `make runtime-bootstrap{task_suffix}`")
    lines.append("")
    return lines


def _context_skill_names(skillset: dict[str, Any]) -> list[str]:
    kind = str(skillset.get("kind") or "").strip()
    if kind == "skill-repo-set":
        return _context_skill_repo_names(skillset)
    manifest_host_path = Path(str(skillset.get("manifest_host_path", "")))
    if not manifest_host_path.is_file():
        return []
    try:
        return read_manifest_skills(manifest_host_path)
    except Exception:
        return []


def _context_skill_repo_names(skillset: dict[str, Any]) -> list[str]:
    lock_host_path = Path(str(skillset.get("lock_path_host_path", "")))
    if not lock_host_path.is_file():
        return []
    try:
        import json as _json
        lock_data = _json.loads(lock_host_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_skills = lock_data.get("skills") or []
    if isinstance(raw_skills, list):
        return sorted(skill.get("name", "") for skill in raw_skills if skill.get("name"))
    if isinstance(raw_skills, dict):
        return sorted(raw_skills.keys())
    return []


def _context_skill_lines(model: dict[str, Any]) -> list[str]:
    skills = model.get("skills") or []
    if not skills:
        return []
    lines = ["## Installed Skills", ""]
    for skillset in skills:
        sid = skillset["id"]
        skill_names = _context_skill_names(skillset)
        if skill_names:
            lines.append(f"- **{sid}**: {', '.join(skill_names)}")
        else:
            lines.append(f"- **{sid}**: (empty)")
    lines.append("")
    return lines


def _context_log_lines(model: dict[str, Any]) -> list[str]:
    logs = model.get("logs") or []
    if not logs:
        return []
    lines = ["## Logs", "", "| ID | Path |", "|----|------|"]
    for log_item in logs:
        lines.append(f"| {log_item['id']} | `{log_item['path']}` |")
    lines.append("")
    return lines


def _context_quick_reference_lines(model: dict[str, Any], make_suffix: str) -> list[str]:
    lines = [
        "## Quick Reference",
        "",
        "```bash",
        "python3 .env-manager/manage.py capabilities --json",
        "python3 .env-manager/manage.py next --format json",
        f"make dev-sanity{make_suffix}",
        f"make runtime-status{make_suffix}",
        f"make runtime-sync{make_suffix}",
        f"make runtime-up{make_suffix} SERVICE=<id>",
        f"make runtime-down{make_suffix} SERVICE=<id>",
        f"make runtime-logs{make_suffix} SERVICE=<id>",
    ]
    if model.get("tasks") or []:
        lines.append(f"make runtime-bootstrap{make_suffix} TASK=<id>")
    lines.extend(["```", ""])
    return lines


def generate_context_markdown(model: dict[str, Any]) -> str:
    """Generate a CLAUDE.md / AGENTS.md from the resolved runtime model."""
    active_clients = model.get("active_clients") or []
    active_profiles = [profile for profile in (model.get("active_profiles") or []) if profile != "core"]
    clients_data = _context_clients_data(model)
    make_suffix = _context_make_suffix(active_clients)
    lines: list[str] = []
    lines.extend(_context_header_lines(make_suffix))
    lines.extend(_context_environment_lines(model, active_clients, active_profiles, clients_data))
    lines.extend(_context_tooling_lines())
    lines.extend(_context_pressure_lines(model))
    lines.extend(_context_repo_lines(model))
    lines.extend(_context_service_lines(model, make_suffix))
    lines.extend(_context_task_lines(model, make_suffix))
    lines.extend(_context_skill_lines(model))
    distributor_lines = render_connected_distributors_section(model)
    if distributor_lines:
        lines.extend(distributor_lines)
    lines.extend(_context_log_lines(model))
    lines.extend(_context_quick_reference_lines(model, make_suffix))
    return "\n".join(lines)


def _live_status_lines(svc_states: list[dict[str, Any]]) -> list[str]:
    if not svc_states:
        return []
    lines = ["", "## Live Status", "", "| Service | State | PID | Healthy |", "|---------|-------|-----|---------|"]
    for svc in svc_states:
        pid = str(svc.get("pid") or "-")
        healthy = "yes" if svc.get("healthy") else "no"
        lines.append(f"| {svc['id']} | {svc.get('state', 'unknown')} | {pid} | {healthy} |")
    lines.append("")
    return lines


def _repo_state_lines(repo_states: list[dict[str, Any]]) -> list[str]:
    git_repos = [r for r in repo_states if r.get("git")]
    if not git_repos:
        return []
    lines = ["## Repo State", "", "| Repo | Branch | Dirty | Untracked | Last Commit |", "|------|--------|-------|-----------|-------------|"]
    for repo in git_repos:
        lines.append(
            f"| {repo['id']} | `{repo.get('branch', '-')}` | "
            f"{repo.get('dirty', 0)} | {repo.get('untracked', 0)} | "
            f"{repo.get('last_commit', '-')} |"
        )
    lines.append("")
    return lines


def _session_state_lines(session_states: list[dict[str, Any]]) -> list[str]:
    if not session_states:
        return []
    lines = [
        "## Sessions", "",
        "| Client | Session | Status | Updated | Label | Last Event |",
        "|--------|---------|--------|---------|-------|------------|",
    ]
    for session in session_states:
        updated_at = float(session.get("updated_at") or 0)
        updated_str = time.strftime("%H:%M", time.localtime(updated_at)) if updated_at else "-"
        label = str(session.get("label") or session.get("goal") or "-").replace("|", "\\|")
        last_bits = [str(session.get("last_event_type") or "").strip()]
        last_message = str(session.get("last_message") or "").strip()
        if last_message:
            last_bits.append(last_message)
        last_event = (" ".join(bit for bit in last_bits if bit).strip() or "-").replace("|", "\\|")
        lines.append(
            f"| {session['client_id']} | `{session['session_id']}` | "
            f"{session.get('status', '-')} | {updated_str} | {label} | {last_event} |"
        )
    lines.append("")
    return lines


def _collect_attention_items(live_state: dict[str, Any], svc_states: list[dict[str, Any]]) -> list[str]:
    attention: list[str] = []
    for warning in (live_state.get("pressure_advisory") or {}).get("warnings") or []:
        attention.append(f"PRESSURE: {warning}")
    for check in live_state.get("checks") or []:
        if not check.get("ok"):
            attention.append(f"CHECK FAIL: **{check['id']}** ({check['type']})")
    for svc in svc_states:
        if svc.get("state") in ("stopped", "not-running", "declared"):
            attention.append(f"SERVICE DOWN: **{svc['id']}** (state: {svc['state']})")
        elif svc.get("state") == "starting":
            attention.append(f"SERVICE STARTING: **{svc['id']}** — may not be healthy yet")
    for log_item in live_state.get("logs") or []:
        errors = log_item.get("recent_errors") or []
        if not errors:
            continue
        scanned = log_item.get("scanned_file", "")
        file_note = f" ({scanned})" if scanned else ""
        attention.append(f"RECENT ERRORS in **{log_item['id']}**{file_note}:")
        for err_line in errors[-3:]:
            attention.append(f"  `{err_line.strip()[:120]}`")
    return attention


def _attention_lines(attention: list[str]) -> list[str]:
    if not attention:
        return []
    lines = ["## Attention", ""]
    for item in attention:
        lines.append(item if item.startswith("  ") else f"- {item}")
    lines.append("")
    return lines


def generate_live_context_markdown(
    model: dict[str, Any], live_state: dict[str, Any],
    root_dir: Path = DEFAULT_ROOT_DIR,
) -> str:
    """Generate enriched CLAUDE.md / AGENTS.md with live runtime state."""
    svc_states = live_state.get("services") or []
    lines: list[str] = [generate_context_markdown(model).rstrip()]
    lines.extend(_live_status_lines(svc_states))
    lines.extend(_repo_state_lines(live_state.get("repos") or []))
    lines.extend(_session_state_lines(live_state.get("sessions") or []))
    lines.extend(_attention_lines(_collect_attention_items(live_state, svc_states)))
    return "\n".join(lines)


def write_agent_context_files(
    content: str,
    *,
    root_dir: Path,
    dry_run: bool,
    context_dir: Path | None,
    action_prefix: str,
    event_subject: str | None = None,
) -> list[str]:
    actions: list[str] = []
    claude_path, codex_path, symlink_target = context_output_paths(root_dir, context_dir)

    ensure_directory(claude_path.parent, dry_run)
    if not dry_run:
        claude_path.write_text(content, encoding="utf-8")
    actions.append(f"{action_prefix}: {repo_rel(root_dir, claude_path)}")

    ensure_directory(codex_path.parent, dry_run)
    if codex_path.is_symlink():
        current_target = os.readlink(str(codex_path))
        if current_target == symlink_target:
            actions.append(
                f"exists: {repo_rel(root_dir, codex_path)}"
                f" -> {symlink_target}"
            )
            return actions
        if not dry_run:
            codex_path.unlink()
    elif codex_path.exists():
        if not dry_run:
            codex_path.unlink()

    if not dry_run:
        codex_path.symlink_to(symlink_target)
    actions.append(
        f"symlink-context: {repo_rel(root_dir, codex_path)}"
        f" -> {symlink_target}"
    )

    if not dry_run and event_subject:
        detail = {"output_dir": repo_rel(root_dir, claude_path.parent)}
        log_runtime_event("context.generated", event_subject, detail, root_dir=root_dir)

    return actions


def sync_live_context(
    model: dict[str, Any],
    live_state: dict[str, Any],
    root_dir: Path,
    context_dir: Path | None = None,
) -> list[str]:
    """Write live-enriched CLAUDE.md and create the AGENTS.md symlink."""
    content = generate_live_context_markdown(model, live_state, root_dir)
    return write_agent_context_files(
        content,
        root_dir=root_dir,
        dry_run=False,
        context_dir=context_dir,
        action_prefix="write-live-context",
        event_subject="live-context",
    )


CONTEXT_PATH_KEYS = {
    "cwd_match",
    "scan_roots",
    "default_cwd",
    "plan_root",
    "plan_draft",
    "plan_index",
    "session_plans",
    "workflow_root",
    "workflow_index",
    "evaluation_root",
    "evaluation_notes",
    "invocation_root",
    "invocation_notes",
    "observability_root",
    "observability_notes",
    "extraction_rule",
    "source_docs",
    "strategy_pages",
    "acquisition_pages",
    "hydration_sources",
    "overlay_path",
    "client_dir",
}


def _should_resolve_context_path(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text.startswith(("/", "~", "$")):
        return False
    if "${" in text:
        return False
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme and parsed.netloc:
        return False
    return True


def _resolve_context_paths(
    context: dict[str, Any], client_dir: Path,
) -> dict[str, Any]:
    """Resolve only explicit path-like context keys under client_dir."""

    def _resolve_value(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {
                child_key: _resolve_value(child_value, child_key)
                for child_key, child_value in value.items()
            }
        if isinstance(value, list):
            return [_resolve_value(item, key) for item in value]
        if isinstance(value, str) and key in CONTEXT_PATH_KEYS:
            if _should_resolve_context_path(value):
                return str(client_dir / value)
        return value

    return {
        key: _resolve_value(value, key)
        for key, value in context.items()
    }


def generate_skill_context(
    model: dict[str, Any],
    root_dir: Path,
    dry_run: bool,
    *,
    output_dir: Path | None = None,
) -> list[str]:
    """Write a resolved context.yaml for each active client that declares context."""
    yaml_mod = require_yaml("generate skill context")
    actions: list[str] = []
    active_ids = set(model.get("active_clients") or [])
    runtime_env = model.get("env") or load_runtime_env(root_dir)

    for client in model.get("clients") or []:
        cid = client.get("id", "")
        if cid not in active_ids:
            continue
        raw_context = client.get("context")
        if not raw_context or not isinstance(raw_context, dict):
            continue

        client_runtime_dir = client_config_runtime_dir(runtime_env, cid)
        if output_dir is None:
            client_dir = client_config_host_dir(root_dir, runtime_env, cid)
        else:
            client_dir = output_dir / runtime_path_to_projection_rel_path(runtime_env, str(client_runtime_dir))
        resolved = _resolve_context_paths(raw_context, client_dir)
        resolved["client_id"] = cid
        resolved["client_dir"] = str(client_dir)

        header = (
            f"# AUTO-GENERATED by focus. Do not edit.\n"
            f"# Source: {client_runtime_dir / 'overlay.yaml'}\n\n"
        )
        body = yaml_mod.safe_dump(resolved, sort_keys=False, default_flow_style=False)
        out_path = client_dir / "context.yaml"

        if not dry_run:
            ensure_directory(client_dir, dry_run=False)
            out_path.write_text(header + body, encoding="utf-8")

        actions.append(f"write-skill-context: {repo_rel(root_dir, out_path)}")

    if actions:
        log_runtime_event("skill-context.generated", "focus", root_dir=root_dir)
    return actions


def sync_context(
    model: dict[str, Any],
    root_dir: Path,
    dry_run: bool,
    context_dir: Path | None = None,
) -> list[str]:
    """Write the generated CLAUDE.md and create the AGENTS.md symlink."""
    content = generate_context_markdown(model)
    return write_agent_context_files(
        content,
        root_dir=root_dir,
        dry_run=dry_run,
        context_dir=context_dir,
        action_prefix="write-context",
        event_subject="context",
    )
