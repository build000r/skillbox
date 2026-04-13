from __future__ import annotations

from .shared import *
from .runtime_ops import *

def generate_context_markdown(model: dict[str, Any]) -> str:
    """Generate a CLAUDE.md / AGENTS.md from the resolved runtime model."""
    lines: list[str] = []

    active_clients = model.get("active_clients") or []
    active_profiles = [p for p in (model.get("active_profiles") or []) if p != "core"]
    clients_data = {
        str(c.get("id", "")): c
        for c in model.get("clients") or []
    }

    # Build make suffix for commands
    make_parts: list[str] = []
    if active_clients:
        make_parts.append(f"CLIENT={active_clients[0]}")

    make_suffix = " " + " ".join(make_parts) if make_parts else ""

    # Determine default CWD from active client
    default_cwd = ""
    for client_id in active_clients:
        client_data = clients_data.get(client_id, {})
        cwd = str(client_data.get("default_cwd", "")).strip()
        if cwd:
            default_cwd = cwd
            break

    # Regenerate hint
    regen_cmd = f"make context{make_suffix}"
    sync_cmd = f"make runtime-sync{make_suffix}"

    # Header
    lines.append("# skillbox")
    lines.append("")
    lines.append(f"> Auto-generated from the runtime graph. Do not edit manually.")
    lines.append(f"> Regenerate: `{regen_cmd}` or `{sync_cmd}`.")
    lines.append("")
    lines.append("You are inside a skillbox workspace container.")
    lines.append("")

    # Environment
    lines.append("## Environment")
    lines.append("")
    if active_clients:
        lines.append(f"- Client: **{', '.join(active_clients)}**")
    if default_cwd:
        lines.append(f"- Default CWD: `{default_cwd}`")
    if active_profiles:
        lines.append(f"- Profiles: {', '.join(active_profiles)}")

    # Skill context pointer
    runtime_env = model.get("env") or {}
    for cid_env in active_clients:
        client_data_env = clients_data.get(cid_env, {})
        if client_data_env.get("context"):
            ctx_path = client_config_runtime_dir(runtime_env, cid_env) / "context.yaml"
            lines.append(f"- Skill context: `$SKILLBOX_CLIENT_CONTEXT` → `{ctx_path}`")
            break
    lines.append("")

    lines.append("## Tooling Guidance")
    lines.append("")
    lines.append(
        "- GitHub: use `gh-axi` for GitHub operations when available; "
        "fall back to `gh` only when `gh-axi` cannot satisfy the task."
    )
    lines.append("")

    # Repos
    repos = model.get("repos") or []
    if repos:
        lines.append("## Repos")
        lines.append("")
        lines.append("| ID | Path | Kind |")
        lines.append("|----|------|------|")
        for repo in repos:
            lines.append(
                f"| {repo['id']} | `{repo['path']}` | {repo.get('kind', 'repo')} |"
            )
        lines.append("")

    # Services
    services = model.get("services") or []
    if services:
        lines.append("## Services")
        lines.append("")
        for service in services:
            sid = service["id"]
            kind = service.get("kind", "service")
            profiles = service.get("profiles") or []
            profile_label = ", ".join(profiles) or "core"
            manageable, reason = service_supports_lifecycle(service)

            if manageable:
                svc_parts = list(make_parts)
                non_core = [p for p in profiles if p != "core"]
                if non_core:
                    svc_parts.append(f"PROFILE={non_core[0]}")
                svc_parts.append(f"SERVICE={sid}")
                svc_suffix = " " + " ".join(svc_parts)

                deps = service_dependency_ids(service)
                dep_note = f" (depends on: {', '.join(deps)})" if deps else ""

                lines.append(f"- **{sid}** ({kind}, {profile_label}){dep_note}")
                lines.append(f"  - Start: `make runtime-up{svc_suffix}`")
                lines.append(f"  - Stop: `make runtime-down{svc_suffix}`")
                lines.append(f"  - Logs: `make runtime-logs{svc_suffix}`")
            else:
                lines.append(
                    f"- **{sid}** ({kind}, {profile_label})"
                    f" — {reason or 'not manageable'}"
                )
        lines.append("")

    # Tasks
    tasks = model.get("tasks") or []
    if tasks:
        lines.append("## Tasks")
        lines.append("")
        for task in tasks:
            tid = task["id"]
            deps = task_dependency_ids(task)
            dep_note = f" (depends on: {', '.join(deps)})" if deps else ""

            task_parts = list(make_parts)
            task_parts.append(f"TASK={tid}")
            task_suffix = " " + " ".join(task_parts)

            lines.append(
                f"- **{tid}**{dep_note}: `make runtime-bootstrap{task_suffix}`"
            )
        lines.append("")

    # Installed skills
    skills = model.get("skills") or []
    if skills:
        lines.append("## Installed Skills")
        lines.append("")
        for skillset in skills:
            sid = skillset["id"]
            skill_names: list[str] = []
            kind = str(skillset.get("kind") or "").strip()
            if kind == "skill-repo-set":
                lock_host_path = Path(str(skillset.get("lock_path_host_path", "")))
                if lock_host_path.is_file():
                    try:
                        import json as _json
                        lock_data = _json.loads(lock_host_path.read_text(encoding="utf-8"))
                        raw_skills = lock_data.get("skills") or []
                        if isinstance(raw_skills, list):
                            skill_names = sorted(s.get("name", "") for s in raw_skills if s.get("name"))
                        elif isinstance(raw_skills, dict):
                            skill_names = sorted(raw_skills.keys())
                    except Exception:
                        pass
            else:
                manifest_host_path = Path(
                    str(skillset.get("manifest_host_path", ""))
                )
                if manifest_host_path.is_file():
                    try:
                        skill_names = read_manifest_skills(manifest_host_path)
                    except Exception:
                        pass

            if skill_names:
                lines.append(f"- **{sid}**: {', '.join(skill_names)}")
            else:
                lines.append(f"- **{sid}**: (empty)")
        lines.append("")

    # Logs
    logs = model.get("logs") or []
    if logs:
        lines.append("## Logs")
        lines.append("")
        lines.append("| ID | Path |")
        lines.append("|----|------|")
        for log_item in logs:
            lines.append(f"| {log_item['id']} | `{log_item['path']}` |")
        lines.append("")

    # Quick reference
    lines.append("## Quick Reference")
    lines.append("")
    lines.append("```bash")
    lines.append(f"make dev-sanity{make_suffix}")
    lines.append(f"make runtime-status{make_suffix}")
    lines.append(f"make runtime-sync{make_suffix}")
    lines.append(f"make runtime-up{make_suffix} SERVICE=<id>")
    lines.append(f"make runtime-down{make_suffix} SERVICE=<id>")
    lines.append(f"make runtime-logs{make_suffix} SERVICE=<id>")
    if tasks:
        lines.append(f"make runtime-bootstrap{make_suffix} TASK=<id>")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def generate_live_context_markdown(
    model: dict[str, Any], live_state: dict[str, Any],
    root_dir: Path = DEFAULT_ROOT_DIR,
) -> str:
    """Generate enriched CLAUDE.md / AGENTS.md with live runtime state."""
    base = generate_context_markdown(model)
    lines: list[str] = [base.rstrip()]

    # --- Live Service Status ---
    svc_states = live_state.get("services") or []
    if svc_states:
        lines.append("")
        lines.append("## Live Status")
        lines.append("")
        lines.append("| Service | State | PID | Healthy |")
        lines.append("|---------|-------|-----|---------|")
        for svc in svc_states:
            pid = str(svc.get("pid") or "-")
            healthy = "yes" if svc.get("healthy") else "no"
            state = svc.get("state", "unknown")
            lines.append(f"| {svc['id']} | {state} | {pid} | {healthy} |")
        lines.append("")

    # --- Repo State ---
    repo_states = live_state.get("repos") or []
    git_repos = [r for r in repo_states if r.get("git")]
    if git_repos:
        lines.append("## Repo State")
        lines.append("")
        lines.append("| Repo | Branch | Dirty | Untracked | Last Commit |")
        lines.append("|------|--------|-------|-----------|-------------|")
        for repo in git_repos:
            branch = repo.get("branch", "-")
            dirty = str(repo.get("dirty", 0))
            untracked = str(repo.get("untracked", 0))
            last_commit = repo.get("last_commit", "-")
            lines.append(
                f"| {repo['id']} | `{branch}` | {dirty} | {untracked} | {last_commit} |"
            )
        lines.append("")

    session_states = live_state.get("sessions") or []
    if session_states:
        lines.append("## Sessions")
        lines.append("")
        lines.append("| Client | Session | Status | Updated | Label | Last Event |")
        lines.append("|--------|---------|--------|---------|-------|------------|")
        for session in session_states:
            updated_at = float(session.get("updated_at") or 0)
            updated_str = time.strftime("%H:%M", time.localtime(updated_at)) if updated_at else "-"
            label = str(session.get("label") or session.get("goal") or "-").replace("|", "\\|")
            last_bits = [str(session.get("last_event_type") or "").strip()]
            last_message = str(session.get("last_message") or "").strip()
            if last_message:
                last_bits.append(last_message)
            last_event = " ".join(bit for bit in last_bits if bit).strip() or "-"
            last_event = last_event.replace("|", "\\|")
            lines.append(
                f"| {session['client_id']} | `{session['session_id']}` | {session.get('status', '-')} | {updated_str} | {label} | {last_event} |"
            )
        lines.append("")

    # --- Attention ---
    attention: list[str] = []

    # Failing checks
    for check in live_state.get("checks") or []:
        if not check.get("ok"):
            attention.append(f"CHECK FAIL: **{check['id']}** ({check['type']})")

    # Non-running services
    for svc in svc_states:
        if svc.get("state") in ("stopped", "not-running", "declared"):
            attention.append(
                f"SERVICE DOWN: **{svc['id']}** (state: {svc['state']})"
            )
        elif svc.get("state") == "starting":
            attention.append(
                f"SERVICE STARTING: **{svc['id']}** — may not be healthy yet"
            )

    # Recent errors from logs
    for log_item in live_state.get("logs") or []:
        errors = log_item.get("recent_errors") or []
        if errors:
            scanned = log_item.get("scanned_file", "")
            file_note = f" ({scanned})" if scanned else ""
            attention.append(
                f"RECENT ERRORS in **{log_item['id']}**{file_note}:"
            )
            for err_line in errors[-3:]:
                attention.append(f"  `{err_line.strip()[:120]}`")

    if attention:
        lines.append("## Attention")
        lines.append("")
        for item in attention:
            if item.startswith("  "):
                lines.append(item)
            else:
                lines.append(f"- {item}")
        lines.append("")

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


def _resolve_context_paths(
    context: dict[str, Any], client_dir: Path,
) -> dict[str, Any]:
    """Resolve relative paths in a context dict to absolute paths under client_dir.

    A value is treated as a relative path if it doesn't start with ``/`` and
    contains no spaces (heuristic: avoids mangling descriptions or list items).
    """
    resolved: dict[str, Any] = {}
    for key, value in context.items():
        if isinstance(value, dict):
            resolved[key] = _resolve_context_paths(value, client_dir)
        elif isinstance(value, str) and not value.startswith("/") and " " not in value and "/" in value:
            resolved[key] = str(client_dir / value)
        elif isinstance(value, list):
            resolved[key] = [
                str(client_dir / v)
                if isinstance(v, str) and not v.startswith("/") and " " not in v and "/" in v
                else v
                for v in value
            ]
        else:
            resolved[key] = value
    return resolved


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
            f"# Source: {client_runtime_dir / 'overlay.yaml'}\n"
            f"# Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n"
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
