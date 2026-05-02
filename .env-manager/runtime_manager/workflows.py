from __future__ import annotations

from .shared import *
from .validation import *
from .runtime_ops import *
from .context_rendering import *
from .text_renderers import print_local_runtime_error_text
from lib.runtime_model import resolve_placeholders

def run_onboard(
    *,
    root_dir: Path,
    client_id: str,
    label: str | None,
    default_cwd: str | None,
    root_path: str | None,
    blueprint_name: str | None,
    set_args: list[str],
    dry_run: bool,
    force: bool,
    wait_seconds: float,
    fmt: str,
) -> int:
    """Macro: client-init → sync → bootstrap → up → context → doctor."""
    steps: list[dict[str, Any]] = []
    is_json = fmt == "json"

    def step(name: str, status: str, detail: Any = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"step": name, "status": status}
        if detail is not None:
            entry["detail"] = detail
        steps.append(entry)
        if not is_json:
            marker = "ok" if status == "ok" else ("skip" if status == "skip" else "FAIL")
            print(f"[{marker}] {name}")
        return entry

    # -- 1. Scaffold -----------------------------------------------------------
    try:
        cid = validate_client_id(client_id)
        assignments = parse_key_value_assignments(set_args, "--set")
        scaffold_actions, blueprint_metadata = scaffold_client_overlay(
            root_dir=root_dir,
            client_id=cid,
            label=label,
            default_cwd=default_cwd,
            root_path=root_path,
            blueprint_name=blueprint_name,
            blueprint_assignments=assignments,
            dry_run=dry_run,
            force=force,
        )
        scaffold_detail: dict[str, Any] = {"actions": scaffold_actions}
        if blueprint_metadata is not None:
            scaffold_detail["blueprint"] = blueprint_metadata
        step("scaffold", "ok", scaffold_detail)
    except RuntimeError as exc:
        step("scaffold", "fail", {"error": str(exc)})
        payload: dict[str, Any] = {
            "client_id": client_id,
            "dry_run": dry_run,
            "steps": steps,
        }
        payload.update(classify_error(exc, "onboard"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # In dry-run mode, the scaffold didn't write files, so the client won't
    # exist in the runtime model.  Report what *would* happen and stop early.
    if dry_run:
        for skip_name in ("sync", "bootstrap", "up", "context", "verify"):
            step(skip_name, "skip", {"reason": "dry-run"})
        payload = {
            "client_id": cid,
            "dry_run": True,
            "steps": steps,
            "next_actions": [f"onboard {cid} --format json"],
        }
        if is_json:
            emit_json(payload)
        return EXIT_OK

    # -- 2. Sync ---------------------------------------------------------------
    try:
        model = build_runtime_model(root_dir)
        active_profiles = normalize_active_profiles([])
        active_clients = normalize_active_clients(model, [cid])
        model = filter_model(model, active_profiles, active_clients)
        sync_actions = sync_runtime(model, dry_run=False)
        step("sync", "ok", {"actions": sync_actions})
    except RuntimeError as exc:
        step("sync", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "dry_run": False, "steps": steps}
        payload.update(classify_error(exc, "onboard"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # -- 3. Bootstrap ----------------------------------------------------------
    try:
        requested_tasks = select_tasks(model, [])
        tasks = resolve_tasks_for_run(model, requested_tasks)
        if tasks:
            ensure_required_env_files_ready(select_env_files_for_tasks(model, tasks))
            task_results = run_tasks(model, tasks, dry_run=False)
            step("bootstrap", "ok", {"tasks": task_results})
        else:
            step("bootstrap", "skip", {"reason": "no tasks declared"})
    except RuntimeError as exc:
        step("bootstrap", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "dry_run": False, "steps": steps}
        payload.update(classify_error(exc, "onboard"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # -- 4. Up -----------------------------------------------------------------
    try:
        requested_services = select_services(model, [])
        services = resolve_services_for_start(model, requested_services)
        if services:
            ensure_required_env_files_ready(select_env_files_for_services(model, services))
            service_results = start_services(
                model, services, dry_run=False, wait_seconds=wait_seconds,
            )
            step("up", "ok", {"services": service_results})
        else:
            step("up", "skip", {"reason": "no services declared"})
    except RuntimeError as exc:
        step("up", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "dry_run": False, "steps": steps}
        payload.update(classify_error(exc, "onboard"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # -- 5. Context ------------------------------------------------------------
    try:
        context_actions = sync_context(model, root_dir, dry_run=False)
        step("context", "ok", {"actions": context_actions})
    except RuntimeError as exc:
        step("context", "fail", {"error": str(exc)})

    # -- 6. Doctor (verify) ----------------------------------------------------
    doctor = doctor_results(model, root_dir)
    has_fail = any(r.status == "fail" for r in doctor)
    has_warn = any(r.status == "warn" for r in doctor)
    step(
        "verify",
        "fail" if has_fail else ("warn" if has_warn else "ok"),
        {"checks": [asdict(r) for r in doctor]},
    )

    payload = {
        "client_id": cid,
        "dry_run": False,
        "steps": steps,
        "next_actions": (
            [f"doctor --client {cid} --format json", f"status --client {cid} --format json"]
            if has_fail
            else [f"status --client {cid} --format json"]
        ),
    }
    log_runtime_event("onboard.completed", cid, {
        "steps_ok": sum(1 for s in steps if s.get("status") == "ok"),
    }, root_dir)
    if is_json:
        emit_json(payload)
    return EXIT_DRIFT if has_fail else EXIT_OK


def run_first_box(
    *,
    root_dir: Path,
    client_id: str,
    private_path_arg: str | None,
    profiles: list[str],
    output_dir_arg: str | None,
    label: str | None,
    default_cwd: str | None,
    root_path: str | None,
    blueprint_name: str | None,
    set_args: list[str],
    force: bool,
    wait_seconds: float,
    fmt: str,
) -> int:
    """Canonical first-box flow: private-init -> onboard (if needed) -> acceptance -> client-open."""
    steps: list[dict[str, Any]] = []
    is_json = fmt == "json"

    def step(name: str, status: str, detail: Any = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"step": name, "status": status}
        if detail is not None:
            entry["detail"] = detail
        steps.append(entry)
        if not is_json:
            marker = "ok" if status == "ok" else ("skip" if status == "skip" else ("warn" if status == "warn" else "FAIL"))
            print(f"[{marker}] {name}")
        return entry

    def emit_first_box(payload: dict[str, Any]) -> int:
        if is_json:
            emit_json(payload)
        else:
            print(f"client: {payload['client_id']}")
            print(f"private_repo: {payload['private_repo']['target_dir']}")
            print(f"output_dir: {payload.get('output_dir', '')}")
            print(f"profiles: {', '.join(payload.get('active_profiles') or ['core'])}")
            print(f"created_client: {payload.get('created_client', False)}")
            if payload.get("mcp_servers"):
                print(f"mcp_servers: {', '.join(payload['mcp_servers'])}")
            print()
            for item in steps:
                marker = item["status"]
                print(f"{item['step']}: {marker}")
        return payload.get("exit_code", EXIT_OK)

    def failure_payload(
        *,
        client_id: str,
        private_repo: dict[str, Any],
        created_client: bool,
        nested_payload: dict[str, Any] | None = None,
        command: str,
        default_message: str,
        exit_code: int = EXIT_ERROR,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "client_id": client_id,
            "private_repo": private_repo,
            "created_client": created_client,
            "steps": steps,
            "exit_code": exit_code,
        }
        nested_error = (nested_payload or {}).get("error")
        if isinstance(nested_error, dict):
            payload["error"] = nested_error
        else:
            payload.update(classify_error(RuntimeError(default_message), command))
        if nested_payload and isinstance(nested_payload.get("next_actions"), list):
            payload["next_actions"] = nested_payload["next_actions"]
        elif "next_actions" not in payload:
            payload["next_actions"] = next_actions_for_first_box(client_id, profiles)
        return payload

    def required_open_surface_mcp_servers(active_profiles: list[str]) -> list[str]:
        model = build_runtime_model(root_dir)
        filtered_model = filter_model(
            model,
            normalize_active_profiles(active_profiles),
            normalize_active_clients(model, [cid]),
        )
        return [str(request["name"]) for request in requested_mcp_servers(filtered_model)]

    try:
        cid = validate_client_id(client_id)
    except RuntimeError as exc:
        payload: dict[str, Any] = {"client_id": client_id, "steps": steps, "exit_code": EXIT_ERROR}
        payload.update(classify_error(exc, "first-box"))
        return emit_first_box(payload)

    try:
        private_init_payload = init_private_repo(root_dir, target_dir_arg=private_path_arg)
        private_repo = {
            "target_dir": private_init_payload["target_dir"],
            "clients_host_root": private_init_payload["clients_host_root"],
        }
        step(
            "private-init",
            "ok",
            {
                "target_dir": private_init_payload["target_dir"],
                "clients_host_root": private_init_payload["clients_host_root"],
                "actions": private_init_payload.get("actions") or [],
            },
        )
    except RuntimeError as exc:
        step("private-init", "fail", {"error": str(exc)})
        payload = {
            "client_id": cid,
            "steps": steps,
            "exit_code": EXIT_ERROR,
        }
        payload.update(classify_error(exc, "first-box"))
        return emit_first_box(payload)

    _, overlay_path, overlay_runtime_path = client_overlay_location(root_dir, cid)
    overlay_exists = overlay_path.is_file()
    created_client = not overlay_exists
    scaffold_inputs_present = any(
        value is not None and value != []
        for value in (label, default_cwd, root_path, blueprint_name, set_args)
    ) or force
    # Box lifecycle callers pass scaffold defaults defensively.  When the
    # private repo already owns an overlay, first-box must not reinterpret those
    # defaults as permission to overwrite it; use onboard/client-init --force
    # for an intentional replacement.
    onboard_needed = created_client or force

    if onboard_needed:
        onboard_args = ["onboard", cid, "--wait-seconds", str(wait_seconds), "--format", "json"]
        if label is not None:
            onboard_args.extend(["--label", label])
        if default_cwd is not None:
            onboard_args.extend(["--default-cwd", default_cwd])
        if root_path is not None:
            onboard_args.extend(["--root-path", root_path])
        if blueprint_name is not None:
            onboard_args.extend(["--blueprint", blueprint_name])
        for assignment in set_args:
            onboard_args.extend(["--set", assignment])
        if force:
            onboard_args.append("--force")

        onboard_code, onboard_payload = run_manage_json_command(root_dir, onboard_args)
        if onboard_code == EXIT_ERROR:
            step("onboard", "fail", onboard_payload)
            step("acceptance", "skip", {"reason": "onboard failed"})
            step("open", "skip", {"reason": "onboard failed"})
            payload = failure_payload(
                client_id=cid,
                private_repo=private_repo,
                created_client=created_client,
                nested_payload=onboard_payload,
                command="first-box",
                default_message=f"first-box onboard failed for {cid}",
            )
            return emit_first_box(payload)

        onboard_status = "warn" if onboard_code == EXIT_DRIFT else "ok"
        step("onboard", onboard_status, onboard_payload)
    else:
        ignored_scaffold_inputs: list[str] = []
        if scaffold_inputs_present:
            if label is not None:
                ignored_scaffold_inputs.append("label")
            if default_cwd is not None:
                ignored_scaffold_inputs.append("default_cwd")
            if root_path is not None:
                ignored_scaffold_inputs.append("root_path")
            if blueprint_name is not None:
                ignored_scaffold_inputs.append("blueprint")
            if set_args:
                ignored_scaffold_inputs.append("set")
        skip_detail: dict[str, Any] = {
            "reason": f"client overlay already present at {overlay_runtime_path}",
        }
        if ignored_scaffold_inputs:
            skip_detail["ignored_scaffold_inputs"] = ignored_scaffold_inputs
            skip_detail["next_actions"] = [
                f"onboard {cid} --force --format json",
                f"client-init {cid} --force --format json",
            ]
        step(
            "onboard",
            "skip",
            skip_detail,
        )

    profile_args = [arg for profile in profiles for arg in ("--profile", profile)]
    acceptance_code, acceptance_payload = run_manage_json_command(
        root_dir,
        ["acceptance", cid, *profile_args, "--wait-seconds", str(wait_seconds), "--format", "json"],
    )
    if acceptance_code != EXIT_OK or not acceptance_payload.get("ready"):
        step("acceptance", "fail", acceptance_payload)
        step("open", "skip", {"reason": "acceptance failed"})
        payload = failure_payload(
            client_id=cid,
            private_repo=private_repo,
            created_client=created_client,
            nested_payload=acceptance_payload,
            command="first-box",
            default_message=f"first-box acceptance failed for {cid}",
        )
        return emit_first_box(payload)

    step("acceptance", "ok", acceptance_payload)

    open_args = ["client-open", cid, *profile_args]
    if output_dir_arg is not None:
        open_args.extend(["--output-dir", output_dir_arg])
    open_args.extend(["--format", "json"])
    open_code, open_payload = run_manage_json_command(root_dir, open_args)
    if open_code not in (EXIT_OK, EXIT_DRIFT):
        step("open", "fail", open_payload)
        payload = failure_payload(
            client_id=cid,
            private_repo=private_repo,
            created_client=created_client,
            nested_payload=open_payload,
            command="first-box",
            default_message=f"first-box client-open failed for {cid}",
        )
        return emit_first_box(payload)

    open_active_profiles = [
        str(value).strip()
        for value in (open_payload.get("active_profiles") or profiles or [])
        if str(value).strip()
    ]
    expected_mcp_servers = required_open_surface_mcp_servers(open_active_profiles)
    actual_mcp_servers = [
        str(value).strip()
        for value in (open_payload.get("mcp_servers") or [])
        if str(value).strip()
    ]
    missing_mcp_servers = [
        server_name
        for server_name in expected_mcp_servers
        if server_name not in set(actual_mcp_servers)
    ]
    if missing_mcp_servers:
        control_plane_error = {
            "type": "missing_mcp_surface",
            "message": (
                "opened client surface is missing required inner MCP servers: "
                + ", ".join(missing_mcp_servers)
            ),
            "recoverable": True,
            "recovery_hint": (
                "Check the root .mcp.json, confirm the active runtime MCP services are declared, "
                "then rerun client-open or first-box."
            ),
        }
        step(
            "open",
            "fail",
            dict(open_payload) | {
                "expected_mcp_servers": expected_mcp_servers,
                "actual_mcp_servers": actual_mcp_servers,
                "missing_mcp_servers": missing_mcp_servers,
                "error": control_plane_error,
            },
        )
        payload = failure_payload(
            client_id=cid,
            private_repo=private_repo,
            created_client=created_client,
            nested_payload={"error": control_plane_error},
            command="first-box",
            default_message=f"first-box open surface missing required MCP servers for {cid}",
        )
        return emit_first_box(payload)

    step("open", "warn" if open_code == EXIT_DRIFT else "ok", open_payload)
    payload = {
        "client_id": cid,
        "private_repo": private_repo,
        "created_client": created_client,
        "output_dir": open_payload.get("output_dir"),
        "active_profiles": open_payload.get("active_profiles") or acceptance_payload.get("active_profiles") or ["core"],
        "mcp_servers": open_payload.get("mcp_servers") or [],
        "steps": steps,
        "next_actions": next_actions_for_first_box(cid, profiles),
        "exit_code": open_code,
    }
    return emit_first_box(payload)


COMPOSE_OVERRIDES_DIR_REL = Path("workspace") / ".compose-overrides"


def generate_client_compose_override(
    root_dir: Path,
    model: dict[str, Any],
    client_id: str,
) -> Path:
    """Generate a docker-compose.client-{id}.yml with per-repo bind mounts."""
    env_values = model.get("env") or {}

    # Collect bind mounts from all repos in the filtered model.
    mounts: dict[str, str] = {}  # runtime_path -> host_path
    for repo in model.get("repos", []):
        host_path = repo.get("host_path")
        runtime_path = repo.get("path")
        if not host_path or not runtime_path:
            continue
        # Skip workspace-internal paths (they're already mounted via /workspace).
        if runtime_path.startswith(env_values.get("SKILLBOX_WORKSPACE_ROOT", "/workspace")):
            continue
        mounts[runtime_path] = host_path

    # Always include the swimmers repo so the binary install path works.
    swimmers_repo = env_values.get("SKILLBOX_SWIMMERS_REPO", "")
    if swimmers_repo and swimmers_repo not in mounts:
        from lib.runtime_model import runtime_path_to_host_path as _rp2hp
        swimmers_host = str(_rp2hp(root_dir, env_values, swimmers_repo))
        if Path(swimmers_host).exists():
            mounts[swimmers_repo] = swimmers_host

    # Remove child paths when a parent is already mounted (avoids redundant mounts).
    sorted_paths = sorted(mounts.keys())
    pruned: dict[str, str] = {}
    for rpath in sorted_paths:
        if any(rpath != parent and rpath.startswith(parent + "/") for parent in pruned):
            continue
        pruned[rpath] = mounts[rpath]

    # Build volume entries.
    volume_entries = [f"{host}:{container}" for container, host in sorted(pruned.items())]

    # Build compose override document.
    lines = [f"# Auto-generated by skillbox for client '{client_id}'. Do not edit."]
    lines.append("services:")
    for svc in ("workspace", "api", "web"):
        lines.append(f"  {svc}:")
        lines.append("    volumes:")
        for entry in volume_entries:
            lines.append(f"      - {entry}")

    out_dir = root_dir / COMPOSE_OVERRIDES_DIR_REL
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"docker-compose.client-{client_id}.yml"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


MANAGE_JSON_COMMAND_TIMEOUT_SECONDS = 300.0


def run_manage_json_command(root_dir: Path, args: list[str]) -> tuple[int, dict[str, Any]]:
    cmd = [sys.executable, str(SCRIPT_DIR / "manage.py"), "--root-dir", str(root_dir), *args]
    # Run in a new session so that on timeout we can SIGKILL the whole group —
    # manage.py itself spawns subprocesses (git, docker compose, etc.) and
    # killing only the immediate child would leave them orphaned.
    with subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=MANAGE_JSON_COMMAND_TIMEOUT_SECONDS)
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return 124, {
                "error": (
                    f"manage.py {' '.join(args)} timed out after "
                    f"{MANAGE_JSON_COMMAND_TIMEOUT_SECONDS:.0f}s"
                ),
                "_stderr": stderr or "",
            }

    stdout = (stdout or "").strip()
    if not stdout:
        payload: dict[str, Any] = {}
    else:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = {"stdout": stdout}
        payload = parsed if isinstance(parsed, dict) else {"payload": parsed}
    stderr = (stderr or "").strip()
    if stderr:
        payload["_stderr"] = stderr
    return returncode, payload


def doctor_step_status(payload: dict[str, Any], exit_code: int) -> str:
    if exit_code not in (EXIT_OK, EXIT_DRIFT):
        return "fail"
    checks = payload.get("checks") or []
    has_fail = any(str(item.get("status")) == "fail" for item in checks)
    has_warn = any(str(item.get("status")) == "warn" for item in checks)
    if has_fail:
        return "fail"
    if has_warn:
        return "warn"
    return "ok"


def focus_step_detail(
    focus_payload: dict[str, Any],
    active_profiles: list[str],
) -> dict[str, Any]:
    services = [
        str(service.get("id"))
        for service in (focus_payload.get("live_state") or {}).get("services") or []
        if str(service.get("id", "")).strip()
    ]
    if not services:
        for item in focus_payload.get("steps") or []:
            if item.get("step") != "up":
                continue
            services = [
                str(service.get("id"))
                for service in (item.get("detail") or {}).get("services") or []
                if str(service.get("id", "")).strip()
            ]
            break
    return {
        "active_profiles": active_profiles,
        "services": services,
        "step_names": [str(item.get("step")) for item in focus_payload.get("steps") or []],
    }

def load_mcp_server_configs(root_dir: Path) -> dict[str, Any]:
    config_path = root_dir / MCP_CONFIG_REL
    if not config_path.is_file():
        return {
            "skillbox": {
                "command": "python3",
                "args": ["/workspace/.env-manager/mcp_server.py"],
            }
        }
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Failed to read {repo_rel(root_dir, config_path)}: {exc}") from exc
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        raise RuntimeError(f"Invalid {repo_rel(root_dir, config_path)}: mcpServers must be an object.")
    return servers


def absolutize_local_path_argument(root_dir: Path, raw_value: str) -> str:
    value = str(raw_value or "").strip()
    if not value or value.startswith("-"):
        return raw_value

    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve()) if candidate.exists() else raw_value

    if "/" not in value and not value.startswith("."):
        return raw_value

    resolved = (root_dir / candidate).resolve()
    if resolved.exists():
        return str(resolved)
    return raw_value


def translate_mcp_server_config(root_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    runtime_env = load_runtime_env(root_dir)
    translated_env = translated_runtime_env(root_dir, runtime_env)
    translated = copy.deepcopy(config)

    command = str(translated.get("command") or "").strip()
    if command:
        command = translate_runtime_paths(command, runtime_env, translated_env)
        translated["command"] = absolutize_local_path_argument(root_dir, command)

    translated["args"] = [
        absolutize_local_path_argument(
            root_dir,
            translate_runtime_paths(str(raw_arg), runtime_env, translated_env),
        )
        for raw_arg in translated.get("args") or []
    ]
    return translated


def selected_mcp_server_configs(root_dir: Path, model: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    server_configs = load_mcp_server_configs(root_dir)
    selected: dict[str, Any] = {}
    server_names: list[str] = []
    services_by_id = {str(s.get("id", "")).strip(): s for s in model.get("services") or []}
    for request in requested_mcp_servers(model):
        server_name = str(request["name"])
        service_id = request.get("service_id")
        config = server_configs.get(server_name)
        if not isinstance(config, dict):
            if isinstance(service_id, str) and service_id:
                backing = services_by_id.get(service_id)
                if backing and not backing.get("required", True):
                    manageable, _reason = service_supports_lifecycle(backing, model)
                    if not manageable:
                        continue
            raise RuntimeError(f"MCP server '{server_name}' is not configured in {MCP_CONFIG_REL}.")
        selected[server_name] = translate_mcp_server_config(root_dir, config)
        server_names.append(server_name)
    return selected, server_names


def mcp_server_name_for_service(service: dict[str, Any]) -> str:
    raw_name = str(service.get("mcp_server") or service.get("id") or "").strip()
    if raw_name.endswith("-mcp"):
        raw_name = raw_name[:-4]
    return raw_name


def requested_mcp_servers(model: dict[str, Any]) -> list[dict[str, Any]]:
    requested: list[dict[str, Any]] = [{"name": "skillbox", "service_id": None}]
    seen = {"skillbox"}
    for service in model.get("services") or []:
        if str(service.get("kind") or "").strip() != "mcp":
            continue
        server_name = mcp_server_name_for_service(service)
        if not server_name or server_name in seen:
            continue
        requested.append({"name": server_name, "service_id": str(service.get("id") or "").strip() or None})
        seen.add(server_name)
    return requested


def send_mcp_message(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("MCP process stdin is unavailable.")
    try:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
    except BrokenPipeError as exc:
        raise RuntimeError("MCP process closed stdin before the request completed.") from exc


def read_mcp_response(
    proc: subprocess.Popen[str],
    request_id: int,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, Any], list[str]]:
    if proc.stdout is None:
        raise RuntimeError("MCP process stdout is unavailable.")

    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    stray_lines: list[str] = []
    deadline = time.monotonic() + timeout_seconds

    try:
        while time.monotonic() < deadline:
            timeout = max(0.0, deadline - time.monotonic())
            events = selector.select(min(0.2, timeout))
            if not events:
                if proc.poll() is not None:
                    break
                continue

            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue

            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                stray_lines.append(text)
                continue

            if message.get("id") != request_id:
                stray_lines.append(text)
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    raise RuntimeError(str(error.get("message") or error))
                raise RuntimeError(str(error))

            result = message.get("result") or {}
            if not isinstance(result, dict):
                raise RuntimeError(f"MCP request {request_id} returned a non-object result.")
            return result, stray_lines
    finally:
        selector.close()

    if proc.poll() is not None:
        raise RuntimeError(f"MCP process exited with code {proc.returncode} before responding.")
    raise RuntimeError(f"Timed out waiting for MCP response to request {request_id}.")


def finalize_mcp_process(proc: subprocess.Popen[str]) -> tuple[list[str], list[str], int | None]:
    if proc.poll() is None:
        proc.terminate()
    try:
        stdout_text, stderr_text = proc.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout_text, stderr_text = proc.communicate(timeout=0.5)
    stdout_lines = [line for line in stdout_text.splitlines() if line.strip()]
    stderr_lines = [line for line in stderr_text.splitlines() if line.strip()]
    return stdout_lines[-10:], stderr_lines[-10:], proc.returncode


def smoke_mcp_server(root_dir: Path, server_name: str, config: dict[str, Any]) -> dict[str, Any]:
    command = str(config.get("command") or "").strip()
    args = [str(arg) for arg in config.get("args") or []]
    detail: dict[str, Any] = {"command": command, "args": args}
    if not command:
        return detail | {"status": "fail", "error": f"MCP server '{server_name}' has no command configured."}

    try:
        proc = subprocess.Popen(
            [command, *args],
            cwd=root_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return detail | {"status": "fail", "error": str(exc)}

    stray_stdout: list[str] = []
    try:
        send_mcp_message(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "skillbox-acceptance", "version": "1.0.0"},
                },
            },
        )
        init_result, init_noise = read_mcp_response(proc, 1, timeout_seconds=MCP_SMOKE_TIMEOUT_SECONDS)
        stray_stdout.extend(init_noise)
        detail["server_info"] = init_result.get("serverInfo") or {}

        send_mcp_message(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        send_mcp_message(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_result, tools_noise = read_mcp_response(proc, 2, timeout_seconds=MCP_SMOKE_TIMEOUT_SECONDS)
        stray_stdout.extend(tools_noise)
        tools = tools_result.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("tools/list did not return a tools array.")
        detail["tool_names"] = [
            str(tool.get("name"))
            for tool in tools
            if isinstance(tool, dict) and str(tool.get("name", "")).strip()
        ]
        detail["status"] = "ok"
    except RuntimeError as exc:
        detail["status"] = "fail"
        detail["error"] = str(exc)
    finally:
        stdout_tail, stderr_tail, exit_code = finalize_mcp_process(proc)
        merged_stdout = stray_stdout + stdout_tail
        if merged_stdout:
            detail["stdout_tail"] = merged_stdout[-10:]
        if stderr_tail:
            detail["stderr_tail"] = stderr_tail
        if exit_code is not None:
            detail["exit_code"] = exit_code

    return detail


def smoke_requested_mcp_servers(
    root_dir: Path,
    model: dict[str, Any],
) -> tuple[bool, dict[str, Any], list[str]]:
    detail: dict[str, Any] = {"servers": {}, "servers_ok": [], "servers_failed": []}
    try:
        server_configs = load_mcp_server_configs(root_dir)
    except RuntimeError as exc:
        detail["error"] = str(exc)
        detail["servers_failed"] = ["skillbox"]
        return False, detail, []

    failed_services: list[str] = []
    for request in requested_mcp_servers(model):
        server_name = str(request["name"])
        service_id = request.get("service_id")
        raw_config = server_configs.get(server_name)
        config = (
            translate_mcp_server_config(root_dir, raw_config)
            if isinstance(raw_config, dict)
            else raw_config
        )
        if not isinstance(config, dict):
            # Skip non-required MCP servers whose artifact is unavailable
            if isinstance(service_id, str) and service_id:
                services_by_id = {str(s.get("id", "")).strip(): s for s in model.get("services") or []}
                backing = services_by_id.get(service_id)
                if backing and not backing.get("required", True):
                    manageable, reason = service_supports_lifecycle(backing, model)
                    if not manageable:
                        detail["servers"][server_name] = {
                            "status": "skip",
                            "reason": reason or "backing service unavailable",
                        }
                        continue
            detail["servers"][server_name] = {
                "status": "fail",
                "error": f"MCP server '{server_name}' is not configured in {MCP_CONFIG_REL}.",
            }
            detail["servers_failed"].append(server_name)
            if isinstance(service_id, str) and service_id:
                failed_services.append(service_id)
            continue

        server_detail = smoke_mcp_server(root_dir, server_name, config)
        detail["servers"][server_name] = server_detail
        if server_detail.get("status") == "ok":
            detail["servers_ok"].append(server_name)
        else:
            detail["servers_failed"].append(server_name)
            if isinstance(service_id, str) and service_id:
                failed_services.append(service_id)

    return not detail["servers_failed"], detail, failed_services


ACCEPTANCE_PROBE_DEFAULT_TIMEOUT_SECONDS = 300.0


def load_client_acceptance_probe(
    *,
    root_dir: Path,
    overlay_path: Path,
) -> dict[str, Any] | None:
    overlay_doc = load_yaml(overlay_path)
    if not isinstance(overlay_doc, dict):
        raise RuntimeError(f"Expected overlay document in {overlay_path} to be a mapping.")

    resolved_overlay = resolve_placeholders(overlay_doc, load_runtime_env(root_dir))
    client_doc = resolved_overlay.get("client")
    if client_doc is None:
        raise RuntimeError(f"Expected top-level `client` mapping in {overlay_path}.")
    if not isinstance(client_doc, dict):
        raise RuntimeError(f"Expected `client` to be a mapping in {overlay_path}.")

    probe = client_doc.get("acceptance_probe")
    if probe is None:
        return None
    if not isinstance(probe, dict):
        raise RuntimeError(f"Expected client.acceptance_probe in {overlay_path} to be a mapping.")

    raw_command = probe.get("command")
    if not isinstance(raw_command, list) or not raw_command:
        raise RuntimeError(
            f"Expected client.acceptance_probe.command in {overlay_path} to be a non-empty list."
        )
    command = [str(arg).strip() for arg in raw_command]
    if any(not arg for arg in command):
        raise RuntimeError(
            f"Expected client.acceptance_probe.command in {overlay_path} to contain only non-empty values."
        )

    raw_cwd = probe.get("cwd")
    cwd: str | None = None
    if raw_cwd is not None:
        cwd = str(raw_cwd).strip()
        if not cwd:
            raise RuntimeError(
                f"Expected client.acceptance_probe.cwd in {overlay_path} to be a non-empty string when provided."
            )

    raw_profiles = probe.get("profiles") or []
    if raw_profiles:
        if not isinstance(raw_profiles, list):
            raise RuntimeError(
                f"Expected client.acceptance_probe.profiles in {overlay_path} to be a list when provided."
            )
        profiles = [str(item).strip() for item in raw_profiles if str(item).strip()]
        if not profiles:
            raise RuntimeError(
                f"Expected client.acceptance_probe.profiles in {overlay_path} to contain at least one profile."
            )
    else:
        profiles = []

    raw_env = probe.get("env") or {}
    if not isinstance(raw_env, dict):
        raise RuntimeError(f"Expected client.acceptance_probe.env in {overlay_path} to be a mapping.")
    env = {str(key).strip(): str(value) for key, value in raw_env.items() if str(key).strip()}

    raw_timeout = probe.get("timeout_seconds", ACCEPTANCE_PROBE_DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Expected client.acceptance_probe.timeout_seconds in {overlay_path} to be numeric."
        ) from exc
    if timeout_seconds <= 0:
        raise RuntimeError(
            f"Expected client.acceptance_probe.timeout_seconds in {overlay_path} to be greater than zero."
        )

    return {
        "command": command,
        "cwd": cwd,
        "profiles": profiles,
        "env": env,
        "timeout_seconds": timeout_seconds,
    }


def run_client_acceptance_probe(
    *,
    root_dir: Path,
    client_id: str,
    profiles: list[str],
    probe: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    runtime_env = load_runtime_env(root_dir)
    translated_env = translated_runtime_env(root_dir, runtime_env)

    def translate_probe_value(raw_value: Any) -> str:
        value = str(raw_value)
        if value.startswith("/"):
            return str(runtime_path_to_host_path(root_dir, runtime_env, value))
        return translate_runtime_paths(value, runtime_env, translated_env)

    raw_cwd = str(probe.get("cwd") or "").strip()
    translated_cwd = translate_probe_value(raw_cwd) if raw_cwd else str(root_dir)
    cwd = root_dir if not raw_cwd else Path(translated_cwd)
    if raw_cwd and not cwd.is_absolute():
        cwd = (root_dir / cwd).resolve()
    translated_command = [
        absolutize_local_path_argument(
            root_dir,
            translate_probe_value(arg),
        )
        for arg in probe["command"]
    ]
    probe_env = {
        str(key): translate_probe_value(value)
        for key, value in (probe.get("env") or {}).items()
    }
    runtime_probe_env = {
        key: str(runtime_env.get(key) or "")
        for key in (
            "SKILLBOX_INGRESS_PUBLIC_BASE_URL",
            "SKILLBOX_INGRESS_PUBLIC_HOST",
            "SKILLBOX_INGRESS_PUBLIC_PORT",
            "SKILLBOX_INGRESS_PRIVATE_BASE_URL",
            "SKILLBOX_INGRESS_PRIVATE_HOST",
            "SKILLBOX_INGRESS_PRIVATE_PORT",
        )
        if str(runtime_env.get(key) or "").strip()
    }

    detail: dict[str, Any] = {
        "command": translated_command,
        "cwd": str(cwd),
        "env_keys": sorted({*probe_env.keys(), *runtime_probe_env.keys()}),
        "timeout_seconds": probe["timeout_seconds"],
    }

    if not cwd.is_dir():
        detail["error"] = f"Probe cwd does not exist: {cwd}"
        return False, detail

    env = os.environ.copy()
    env.update(runtime_probe_env)
    env.update(probe_env)
    env.setdefault("SKILLBOX_ACCEPTANCE_CLIENT_ID", client_id)
    env.setdefault("SKILLBOX_ACCEPTANCE_PROFILES", ",".join(sorted(normalize_active_profiles(profiles))))
    env.setdefault("SKILLBOX_ACCEPTANCE_ROOT_DIR", str(root_dir))

    try:
        result = subprocess.run(
            translated_command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=float(probe["timeout_seconds"]),
        )
    except subprocess.TimeoutExpired:
        detail["error"] = f"Acceptance probe timed out after {probe['timeout_seconds']} seconds."
        return False, detail
    except OSError as exc:
        detail["error"] = str(exc)
        return False, detail

    detail["exit_code"] = result.returncode
    stdout_lines = [line for line in result.stdout.splitlines() if line.strip()]
    stderr_lines = [line for line in result.stderr.splitlines() if line.strip()]
    if stdout_lines:
        detail["stdout_tail"] = stdout_lines[-10:]
    if stderr_lines:
        detail["stderr_tail"] = stderr_lines[-10:]
    return result.returncode == 0, detail


def run_acceptance(
    *,
    root_dir: Path,
    client_id: str,
    profiles: list[str],
    wait_seconds: float,
    fmt: str,
) -> int:
    steps: list[dict[str, Any]] = []
    is_json = fmt == "json"
    active_profiles = sorted(normalize_active_profiles(profiles))

    def step(name: str, status: str, detail: Any = None) -> None:
        entry: dict[str, Any] = {"step": name, "status": status}
        if detail is not None:
            entry["detail"] = detail
        steps.append(entry)
        if not is_json:
            marker = {
                "ok": "ok",
                "warn": "warn",
                "skip": "skip",
            }.get(status, "FAIL")
            print(f"[{marker}] {name}")

    def emit_acceptance(payload: dict[str, Any]) -> int:
        if is_json:
            emit_json(payload)
        else:
            print()
            print(f"  Client:  {payload['client_id']}")
            print(f"  Ready:   {'yes' if payload.get('ready') else 'no'}")
            print(f"  Profiles: {', '.join(payload.get('active_profiles') or ['core'])}")
            if payload.get("error"):
                print(f"  Error:   {payload['error']['message']}")
        return EXIT_OK if payload.get("ready") else EXIT_ERROR

    try:
        cid = validate_client_id(client_id)
    except RuntimeError as exc:
        payload = {"client_id": client_id, "active_profiles": active_profiles, "steps": steps, "ready": False}
        payload.update(classify_error(exc, "acceptance"))
        return emit_acceptance(payload)

    _, overlay_path, overlay_runtime_path = client_overlay_location(root_dir, cid)
    if not overlay_path.is_file():
        payload = {
            "client_id": cid,
            "active_profiles": active_profiles,
            "steps": steps,
            "ready": False,
        }
        payload.update(
            structured_error(
                (
                    f"Client '{cid}' has no overlay at {overlay_runtime_path}. "
                    f"Run onboard {cid} before acceptance."
                ),
                error_type="client_not_onboarded",
                recovery_hint=f"Run onboard {cid} to scaffold the client overlay.",
                next_actions=[f"onboard {cid} --format json"],
            )
        )
        return emit_acceptance(payload)

    try:
        acceptance_probe = load_client_acceptance_probe(root_dir=root_dir, overlay_path=overlay_path)
    except RuntimeError as exc:
        payload = {
            "client_id": cid,
            "active_profiles": active_profiles,
            "steps": steps,
            "ready": False,
        }
        payload.update(
            structured_error(
                str(exc),
                error_type="acceptance_probe_invalid",
                recovery_hint=f"Fix client.acceptance_probe in {overlay_runtime_path} or remove it.",
                next_actions=[f"doctor --client {cid} --format json"],
            )
        )
        return emit_acceptance(payload)

    profile_args = [arg for profile in profiles for arg in ("--profile", profile)]
    doctor_args = ["doctor", "--client", cid, *profile_args, "--format", "json"]
    sync_args = ["sync", "--client", cid, *profile_args, "--format", "json"]
    focus_args = ["focus", cid, *profile_args, "--wait-seconds", str(wait_seconds), "--format", "json"]
    if acceptance_probe is not None and acceptance_probe.get("profiles"):
        probe_profiles = normalize_active_profiles(acceptance_probe["profiles"])
        if not probe_profiles.issubset(set(active_profiles)):
            acceptance_probe = None

    # Doctor pre-flight: only block on config-level failures that sync can't fix
    # (e.g. connector-contract, runtime-manifest). Path/artifact/lock checks are
    # sync-resolvable and should not abort before sync has run.
    PRE_FLIGHT_BLOCKING_CODES = {"runtime-manifest", "connector-contract"}
    doctor_pre_code, doctor_pre_payload = run_manage_json_command(root_dir, doctor_args)
    doctor_pre_checks = doctor_pre_payload.get("checks") or []
    has_blocking_failure = any(
        str(item.get("status")) == "fail" and str(item.get("code", "")) in PRE_FLIGHT_BLOCKING_CODES
        for item in doctor_pre_checks
    )
    if has_blocking_failure:
        doctor_pre_status = "fail"
    elif any(str(item.get("status")) in ("fail", "warn") for item in doctor_pre_checks):
        doctor_pre_status = "warn"
    else:
        doctor_pre_status = "ok"
    step("doctor-pre", doctor_pre_status, {"checks": doctor_pre_checks})
    if doctor_pre_status == "fail":
        step("sync", "skip", {"reason": "doctor-pre failed"})
        step("focus", "skip", {"reason": "doctor-pre failed"})
        step("mcp-smoke", "skip", {"reason": "doctor-pre failed"})
        step("doctor-post", "skip", {"reason": "doctor-pre failed"})
        payload = {
            "client_id": cid,
            "active_profiles": active_profiles,
            "steps": steps,
            "ready": False,
        }
        payload.update(
            structured_error(
                "Pre-flight doctor checks failed.",
                error_type="doctor_pre_failed",
                next_actions=doctor_pre_payload.get("next_actions") or ["doctor --format json"],
            )
        )
        return emit_acceptance(payload)

    sync_code, sync_payload = run_manage_json_command(root_dir, sync_args)
    sync_status = "ok" if sync_code == EXIT_OK else "fail"
    step("sync", sync_status, {"actions": sync_payload.get("actions") or []})
    if sync_status != "ok":
        step("focus", "skip", {"reason": "sync failed"})
        step("mcp-smoke", "skip", {"reason": "sync failed"})
        step("doctor-post", "skip", {"reason": "sync failed"})
        payload = {
            "client_id": cid,
            "active_profiles": active_profiles,
            "steps": steps,
            "ready": False,
        }
        payload["error"] = sync_payload.get("error") or {
            "type": "sync_failed",
            "message": "Sync failed during acceptance.",
            "recoverable": True,
        }
        payload["next_actions"] = sync_payload.get("next_actions") or [f"sync{format_profile_args(profiles)} --format json"]
        return emit_acceptance(payload)

    focus_code, focus_payload = run_manage_json_command(root_dir, focus_args)
    focus_status = "ok" if focus_code == EXIT_OK else "fail"
    step("focus", focus_status, focus_step_detail(focus_payload, active_profiles))
    if focus_status != "ok":
        step("mcp-smoke", "skip", {"reason": "focus failed"})
        step("doctor-post", "skip", {"reason": "focus failed"})
        payload = {
            "client_id": cid,
            "active_profiles": active_profiles,
            "steps": steps,
            "ready": False,
        }
        payload["error"] = focus_payload.get("error") or {
            "type": "focus_failed",
            "message": "Focus failed during acceptance.",
            "recoverable": True,
        }
        payload["next_actions"] = focus_payload.get("next_actions") or [f"focus {cid}{format_profile_args(profiles)} --format json"]
        return emit_acceptance(payload)

    model = build_runtime_model(root_dir)
    filtered_model = filter_model(model, normalize_active_profiles(profiles), normalize_active_clients(model, [cid]))
    mcp_ok, mcp_detail, failed_services = smoke_requested_mcp_servers(root_dir, filtered_model)
    step("mcp-smoke", "ok" if mcp_ok else "fail", mcp_detail)

    probe_ok = True
    probe_detail: dict[str, Any] | None = None
    if mcp_ok and acceptance_probe is not None:
        probe_ok, probe_detail = run_client_acceptance_probe(
            root_dir=root_dir,
            client_id=cid,
            profiles=profiles,
            probe=acceptance_probe,
        )
        step("workflow-probe", "ok" if probe_ok else "fail", probe_detail)

    doctor_post_code, doctor_post_payload = run_manage_json_command(root_dir, doctor_args)
    doctor_post_status = doctor_step_status(doctor_post_payload, doctor_post_code)
    step("doctor-post", doctor_post_status, {"checks": doctor_post_payload.get("checks") or []})

    ready = mcp_ok and probe_ok and doctor_post_status != "fail"
    payload = {
        "client_id": cid,
        "active_profiles": active_profiles,
        "steps": steps,
        "ready": ready,
        "next_actions": (
            next_actions_for_acceptance_success(cid, profiles)
            if ready
            else next_actions_for_acceptance_mcp_failure(profiles, failed_services)
            if not mcp_ok
            else [
                f"status --client {cid}{format_profile_args(profiles)} --format json",
                f"logs --client {cid}{format_profile_args(profiles)} --format json",
            ]
            if not probe_ok
            else doctor_post_payload.get("next_actions") or ["doctor --format json"]
        ),
    }
    if not ready:
        payload["error"] = (
            {
                "type": "mcp_smoke_failed",
                "message": "MCP smoke failed for: " + ", ".join(mcp_detail.get("servers_failed") or ["unknown"]),
                "recoverable": True,
            }
            if not mcp_ok
            else {
                "type": "acceptance_probe_failed",
                "message": "Acceptance probe failed.",
                "recoverable": True,
                "detail": probe_detail or {},
            }
            if not probe_ok
            else {
                "type": "doctor_post_failed",
                "message": "Post-focus doctor checks failed.",
                "recoverable": True,
            }
        )
    return emit_acceptance(payload)


STEWARDSHIP_REPORT_VERSION = 1
STEWARDSHIP_STALE_EVIDENCE_SECONDS = 24 * 60 * 60
STEWARDSHIP_PULSE_STATE_RELS = (
    Path("logs") / "runtime" / "pulse.state.json",
    Path(".skillbox-state") / "logs" / "runtime" / "pulse.state.json",
)


def _stewardship_utc_now() -> tuple[float, str, str]:
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    slug = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return now, stamp, slug


def _load_optional_json_object(path: Path) -> tuple[str, dict[str, Any] | None, str | None]:
    if not path.is_file():
        return "missing", None, None
    try:
        return "present", load_json_file(path), None
    except RuntimeError as exc:
        return "invalid", None, str(exc)


def _state_age_seconds(payload: dict[str, Any] | None, key: str, now: float) -> float | None:
    if not payload:
        return None
    try:
        value = float(payload.get(key) or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(0.0, now - value)


def _stewardship_focus_evidence(root_dir: Path, cid: str, now: float) -> dict[str, Any]:
    focus_path = root_dir / FOCUS_STATE_REL
    status, payload, error = _load_optional_json_object(focus_path)
    evidence: dict[str, Any] = {
        "status": status,
        "path": repo_rel(root_dir, focus_path),
    }
    if error:
        evidence["error"] = error
    if payload:
        focus_client = str(payload.get("client_id") or "").strip()
        age_seconds = _state_age_seconds(payload, "focused_at", now)
        evidence.update(
            {
                "client_id": focus_client,
                "matches_client": focus_client == cid,
                "active_profiles": payload.get("active_profiles") or [],
                "focused_at": payload.get("focused_at"),
                "age_seconds": age_seconds,
                "stale": age_seconds is not None and age_seconds > STEWARDSHIP_STALE_EVIDENCE_SECONDS,
                "skill_context_path": payload.get("skill_context_path"),
            }
        )
        if focus_client and focus_client != cid:
            evidence["status"] = "other_client"
    return evidence


def _stewardship_pulse_candidates(root_dir: Path, model: dict[str, Any]) -> list[Path]:
    candidates = [root_dir / rel for rel in STEWARDSHIP_PULSE_STATE_RELS]
    for log_item in model.get("logs") or []:
        if str(log_item.get("id") or "").strip() == "runtime":
            host_path = str(log_item.get("host_path") or "").strip()
            if host_path:
                candidates.append(Path(host_path) / "pulse.state.json")
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        marker = str(candidate)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(candidate)
    return unique


def _stewardship_pulse_evidence(root_dir: Path, model: dict[str, Any], now: float) -> dict[str, Any]:
    candidates = _stewardship_pulse_candidates(root_dir, model)
    existing = next((path for path in candidates if path.is_file()), None)
    target = existing or candidates[0]
    status, payload, error = _load_optional_json_object(target)
    evidence: dict[str, Any] = {
        "status": status,
        "path": repo_rel(root_dir, target),
        "candidate_paths": [repo_rel(root_dir, path) for path in candidates],
    }
    if error:
        evidence["error"] = error
    if payload:
        age_seconds = _state_age_seconds(payload, "updated_at", now)
        active_clients = payload.get("active_clients") or []
        active_profiles = payload.get("active_profiles") or []
        evidence.update(
            {
                "pid": payload.get("pid"),
                "updated_at": payload.get("updated_at"),
                "age_seconds": age_seconds,
                "stale": age_seconds is not None and age_seconds > STEWARDSHIP_STALE_EVIDENCE_SECONDS,
                "cycle_count": payload.get("cycle_count"),
                "heals": payload.get("heals"),
                "events_emitted": payload.get("events_emitted"),
                "active_clients": active_clients,
                "active_profiles": active_profiles,
            }
        )
    return evidence


def _stewardship_recent_error_evidence(live: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for log_item in live.get("logs") or []:
        errors = [str(line) for line in log_item.get("recent_errors") or [] if str(line).strip()]
        if not errors:
            continue
        entries.append(
            {
                "id": log_item.get("id"),
                "path": log_item.get("path"),
                "present": bool(log_item.get("present")),
                "scanned_files": log_item.get("scanned_files") or [],
                "count": len(errors),
                "samples": errors[-3:],
            }
        )
    return entries


def _stewardship_health_evidence(status_payload: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    checks = live.get("checks") or []
    services = live.get("services") or []
    recent_errors = _stewardship_recent_error_evidence(live)
    return {
        "checks": {
            "passing": sum(1 for item in checks if item.get("ok")),
            "total": len(checks),
            "failing": _stewardship_failing_checks(checks),
        },
        "services": {
            "running": sum(1 for item in services if item.get("state") in {"running", "ok", "idle"}),
            "total": len(services),
            "down": _stewardship_down_services(services),
            "blocked": status_payload.get("blocked_services") or [],
        },
        "recent_errors": {
            "count": sum(int(item.get("count") or 0) for item in recent_errors),
            "logs": recent_errors,
        },
    }


def _stewardship_failing_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": item.get("id"), "type": item.get("type")}
        for item in checks
        if not item.get("ok")
    ]


def _stewardship_down_services(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "kind": item.get("kind"),
            "state": item.get("state"),
        }
        for item in services
        if item.get("state") not in {"running", "ok", "idle"}
    ]


def _stewardship_session_evidence(live: dict[str, Any]) -> dict[str, Any]:
    sessions = [
        {
            "client_id": item.get("client_id"),
            "session_id": item.get("session_id"),
            "status": item.get("status"),
            "label": item.get("label") or "",
            "goal": item.get("goal") or "",
            "updated_at": item.get("updated_at"),
            "last_event_type": item.get("last_event_type") or "",
            "last_message": item.get("last_message") or "",
        }
        for item in live.get("sessions") or []
    ]
    return {"count": len(sessions), "recent": sessions[:5]}


def _stewardship_parity_evidence(status_payload: dict[str, Any]) -> dict[str, Any]:
    parity = status_payload.get("parity_ledger") or {}
    covered = parity.get("covered_surfaces") or []
    deferred = parity.get("deferred_surfaces") or []
    return {
        "covered_surfaces": covered,
        "deferred_surfaces": deferred,
        "covered_count": len(covered),
        "deferred_count": len(deferred),
    }


def _stewardship_doctor_check_payload(result: CheckResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "code": result.code,
        "message": result.message,
        "details": result.details,
    }


def _stewardship_doctor_evidence(model: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    checks = [_stewardship_doctor_check_payload(result) for result in doctor_results(model, root_dir)]
    failures = [check for check in checks if check.get("status") == "fail"]
    warnings = [check for check in checks if check.get("status") == "warn"]
    status = "fail" if failures else ("warn" if warnings else "pass")
    return {
        "status": status,
        "failures": failures,
        "warnings": warnings,
        "checks": checks,
        "counts": {
            "fail": len(failures),
            "warn": len(warnings),
            "pass": sum(1 for check in checks if check.get("status") == "pass"),
            "total": len(checks),
        },
    }


def _stewardship_not_assessed() -> list[dict[str, str]]:
    return [
        {
            "id": "backup-recovery",
            "status": "not_assessed",
            "reason": "No first-class backup or restore drill evidence is declared in the public runtime graph yet.",
            "next_action": "Add a restore-drill check before claiming recovery readiness.",
        },
        {
            "id": "cost-review",
            "status": "not_assessed",
            "reason": "No cost telemetry or budget review evidence is declared in the public runtime graph yet.",
            "next_action": "Add a cost snapshot source before claiming spend stewardship.",
        },
    ]


def _stewardship_risk(
    risk_id: str,
    severity: str,
    title: str,
    evidence: dict[str, Any],
    recommendation: str,
    actions: list[str],
) -> dict[str, Any]:
    return {
        "id": risk_id,
        "severity": severity,
        "title": title,
        "evidence": evidence,
        "recommendation": recommendation,
        "next_actions": actions,
    }


def _stewardship_profile_args(active_profiles: list[str]) -> str:
    profiles = [profile for profile in active_profiles if profile != "core"]
    return format_profile_args(profiles)


def _stewardship_doctor_failure_codes(failures: list[dict[str, Any]]) -> set[str]:
    return {str(failure.get("code") or "").strip() for failure in failures if str(failure.get("code") or "").strip()}


def _stewardship_doctor_recommendation(failures: list[dict[str, Any]]) -> str:
    failure_codes = _stewardship_doctor_failure_codes(failures)
    if "skill-repo-lock" in failure_codes:
        return "Run sync to refresh skill-repo locks, then rerun doctor before treating the box as stewarded."
    if "skill-repo-install" in failure_codes:
        return "Run sync to repair managed skill installs, then rerun doctor before treating the box as stewarded."
    return "Repair the failing doctor checks before treating the box as stewarded."


def _stewardship_doctor_repair_actions(
    cid: str,
    profile_args: str,
    failures: list[dict[str, Any]],
) -> list[str]:
    failure_codes = _stewardship_doctor_failure_codes(failures)
    actions: list[str] = []
    if failure_codes & {"skill-repo-lock", "skill-repo-install"}:
        actions.append(f"sync --client {cid}{profile_args} --format json")
    actions.append(f"doctor --client {cid}{profile_args} --format json")
    return actions


def _stewardship_doctor_risks(
    cid: str,
    profile_args: str,
    doctor: dict[str, Any],
) -> list[dict[str, Any]]:
    failures = doctor.get("failures") or []
    if not failures:
        return []
    return [
        _stewardship_risk(
            "doctor-validation",
            "high",
            "Runtime doctor validation is failing",
            {"failures": failures},
            _stewardship_doctor_recommendation(failures),
            _stewardship_doctor_repair_actions(cid, profile_args, failures),
        )
    ]


def _stewardship_check_risks(
    cid: str,
    profile_args: str,
    health: dict[str, Any],
) -> list[dict[str, Any]]:
    failing_checks = (health.get("checks") or {}).get("failing") or []
    if not failing_checks:
        return []
    return [
        _stewardship_risk(
            "failing-checks",
            "high",
            "Runtime checks are failing",
            {"checks": failing_checks},
            "Run doctor for the scoped client and repair required path or env drift first.",
            [f"doctor --client {cid}{profile_args} --format json"],
        )
    ]


def _stewardship_log_risks(
    cid: str,
    profile_args: str,
    health: dict[str, Any],
) -> list[dict[str, Any]]:
    recent_errors = health.get("recent_errors") or {}
    if int(recent_errors.get("count") or 0) <= 0:
        return []
    return [
        _stewardship_risk(
            "recent-log-errors",
            "high",
            "Recent runtime logs contain error signatures",
            recent_errors,
            "Inspect the scoped logs before treating the box as healthy.",
            [f"logs --client {cid}{profile_args} --format json"],
        )
    ]


def _stewardship_service_risks(
    cid: str,
    profile_args: str,
    health: dict[str, Any],
) -> list[dict[str, Any]]:
    down_services = (health.get("services") or {}).get("down") or []
    if not down_services:
        return []
    return [
        _stewardship_risk(
            "services-not-running",
            "medium",
            "Declared services are not running",
            {"services": down_services},
            "Start or intentionally stop the affected services, then regenerate the stewardship report.",
            [f"up --client {cid}{profile_args} --format json"],
        )
    ]


def _stewardship_focus_risks(
    cid: str,
    profile_args: str,
    focus: dict[str, Any],
) -> list[dict[str, Any]]:
    if focus.get("status") not in {"missing", "invalid", "other_client"} and not focus.get("stale"):
        return []
    return [
        _stewardship_risk(
            "focus-not-current",
            "medium",
            "No current focus evidence exists for this client",
            focus,
            "Run focus so agent context, live state, and client selection are refreshed.",
            [f"focus {cid}{profile_args} --format json"],
        )
    ]


def _stewardship_pulse_risks(
    cid: str,
    pulse: dict[str, Any],
) -> list[dict[str, Any]]:
    pulse_clients = {str(client).strip() for client in pulse.get("active_clients") or [] if str(client).strip()}
    pulse_out_of_scope = bool(pulse_clients) and cid not in pulse_clients
    if pulse.get("status") not in {"missing", "invalid"} and not pulse.get("stale") and not pulse_out_of_scope:
        return []
    pulse_evidence = dict(pulse)
    if pulse_out_of_scope:
        pulse_evidence["matches_client"] = False
    return [
        _stewardship_risk(
            "pulse-not-observed",
            "low",
            "Pulse reconciliation evidence is absent, stale, or scoped elsewhere",
            pulse_evidence,
            "Start or refresh pulse for the scoped client before relying on autonomous drift detection.",
            ["make pulse-status"],
        )
    ]


def _stewardship_parity_risks(
    cid: str,
    profile_args: str,
    parity: dict[str, Any],
) -> list[dict[str, Any]]:
    deferred = parity.get("deferred_surfaces") or []
    if not deferred:
        return []
    return [
        _stewardship_risk(
            "deferred-runtime-surfaces",
            "low",
            "Some runtime surfaces are declared but deferred",
            {"deferred_surfaces": deferred},
            "Keep deferred surfaces out of readiness claims until they are covered or intentionally removed.",
            [f"status --client {cid}{profile_args} --format json"],
        )
    ]


def _stewardship_risks(
    *,
    cid: str,
    active_profiles: list[str],
    focus: dict[str, Any],
    pulse: dict[str, Any],
    health: dict[str, Any],
    parity: dict[str, Any],
    doctor: dict[str, Any],
) -> list[dict[str, Any]]:
    profile_args = _stewardship_profile_args(active_profiles)
    risks: list[dict[str, Any]] = []
    risks.extend(_stewardship_doctor_risks(cid, profile_args, doctor))
    risks.extend(_stewardship_check_risks(cid, profile_args, health))
    risks.extend(_stewardship_log_risks(cid, profile_args, health))
    risks.extend(_stewardship_service_risks(cid, profile_args, health))
    risks.extend(_stewardship_focus_risks(cid, profile_args, focus))
    risks.extend(_stewardship_pulse_risks(cid, pulse))
    risks.extend(_stewardship_parity_risks(cid, profile_args, parity))
    return risks


def _stewardship_next_actions(
    cid: str,
    active_profiles: list[str],
    risks: list[dict[str, Any]],
) -> list[str]:
    actions: list[str] = []
    for risk in risks:
        for action in risk.get("next_actions") or []:
            if action not in actions:
                actions.append(action)
    profile_args = _stewardship_profile_args(active_profiles)
    for action in (
        f"stewardship-report {cid}{profile_args} --format md --write",
        f"acceptance {cid}{profile_args} --format json",
    ):
        if action not in actions:
            actions.append(action)
    return actions


def _build_stewardship_report(root_dir: Path, cid: str, profiles: list[str]) -> dict[str, Any]:
    model = build_runtime_model(root_dir)
    active_profiles = normalize_active_profiles(profiles or [])
    active_clients = normalize_active_clients(model, [cid])
    filtered_model = filter_model(model, active_profiles, active_clients)
    status_payload = runtime_status(filtered_model)
    live = collect_live_state(filtered_model, root_dir)
    now, generated_at, report_slug = _stewardship_utc_now()
    focus = _stewardship_focus_evidence(root_dir, cid, now)
    pulse = _stewardship_pulse_evidence(root_dir, filtered_model, now)
    health = _stewardship_health_evidence(status_payload, live)
    sessions = _stewardship_session_evidence(live)
    parity = _stewardship_parity_evidence(status_payload)
    doctor = _stewardship_doctor_evidence(filtered_model, root_dir)
    risks = _stewardship_risks(
        cid=cid,
        active_profiles=sorted(filtered_model.get("active_profiles") or []),
        focus=focus,
        pulse=pulse,
        health=health,
        parity=parity,
        doctor=doctor,
    )
    next_recommendation = (
        str(risks[0].get("recommendation") or "")
        if risks
        else "No blocking runtime risk was found in the current local evidence; keep the packet current after focus or runtime changes."
    )
    return {
        "version": STEWARDSHIP_REPORT_VERSION,
        "client_id": cid,
        "active_profiles": sorted(filtered_model.get("active_profiles") or []),
        "generated_at": generated_at,
        "report_slug": report_slug,
        "focus": focus,
        "health": health,
        "evidence": {
            "live_collected_at": live.get("collected_at"),
            "pulse": pulse,
            "doctor": doctor,
            "sessions": sessions,
            "parity_ledger": parity,
            "repos": [
                {
                    "id": repo.get("id"),
                    "present": bool(repo.get("present")),
                    "branch": repo.get("branch"),
                    "dirty": repo.get("dirty"),
                    "untracked": repo.get("untracked"),
                    "last_commit": repo.get("last_commit"),
                }
                for repo in live.get("repos") or []
            ],
        },
        "risks": risks,
        "not_assessed": _stewardship_not_assessed(),
        "next_recommendation": next_recommendation,
        "next_actions": _stewardship_next_actions(
            cid,
            sorted(filtered_model.get("active_profiles") or []),
            risks,
        ),
    }


def _resolve_stewardship_output_dir(root_dir: Path, cid: str, output_dir_arg: str | None) -> Path:
    value = str(output_dir_arg or "").strip()
    if value:
        return resolve_optional_host_dir(root_dir, value, default_rel=Path("reports") / "stewardship")
    _env_values, overlay_path, _overlay_runtime_path = client_overlay_location(root_dir, cid)
    return overlay_path.parent / "reports" / "stewardship"


def render_stewardship_report_markdown(payload: dict[str, Any]) -> str:
    sections = [
        _stewardship_markdown_header(payload),
        _stewardship_markdown_risks(payload),
        _stewardship_markdown_evidence(payload),
        _stewardship_markdown_not_assessed(payload),
        _stewardship_markdown_next_actions(payload),
    ]
    return "\n\n".join(section.rstrip() for section in sections if section.strip()).rstrip() + "\n"


def _stewardship_markdown_header(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# Stewardship Report: {payload['client_id']}",
            "",
            f"- Generated: {payload['generated_at']}",
            f"- Profiles: {', '.join(payload.get('active_profiles') or ['core'])}",
            f"- Recommendation: {payload.get('next_recommendation') or '-'}",
        ]
    )


def _stewardship_markdown_risks(payload: dict[str, Any]) -> str:
    lines = [
        "## Risks",
        "",
    ]
    risks = payload.get("risks") or []
    if not risks:
        lines.append("- No blocking risks found in current local evidence.")
    else:
        for risk in risks:
            lines.append(
                f"- {str(risk.get('severity') or 'info').upper()}: "
                f"{risk.get('title')} (`{risk.get('id')}`)"
            )
            lines.append(f"  Recommendation: {risk.get('recommendation')}")
    return "\n".join(lines)


def _stewardship_markdown_evidence(payload: dict[str, Any]) -> str:
    health = payload.get("health") or {}
    checks = health.get("checks") or {}
    services = health.get("services") or {}
    recent_errors = health.get("recent_errors") or {}
    evidence = payload.get("evidence") or {}
    sessions = evidence.get("sessions") or {}
    parity = evidence.get("parity_ledger") or {}
    doctor = evidence.get("doctor") or {}
    doctor_counts = doctor.get("counts") or {}
    lines = [
        "## Evidence",
        "",
        f"- Focus: {(payload.get('focus') or {}).get('status')} at {(payload.get('focus') or {}).get('path')}",
        f"- Checks: {checks.get('passing', 0)}/{checks.get('total', 0)} passing",
        f"- Services: {services.get('running', 0)}/{services.get('total', 0)} running",
        f"- Recent log errors: {recent_errors.get('count', 0)}",
        f"- Doctor: {doctor.get('status', 'unknown')} ({doctor_counts.get('fail', 0)} failing)",
    ]
    lines.extend(_stewardship_markdown_doctor_findings(doctor))
    lines.extend(
        [
            f"- Sessions: {sessions.get('count', 0)} recent session(s)",
            f"- Parity deferred surfaces: {parity.get('deferred_count', 0)}",
        ]
    )
    return "\n".join(lines)


def _stewardship_markdown_doctor_findings(doctor: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for failure in doctor.get("failures") or []:
        code = failure.get("code") or "unknown"
        message = failure.get("message") or "doctor check failed"
        lines.append(f"  - Failure `{code}`: {message}")
        for issue in ((failure.get("details") or {}).get("issues") or [])[:3]:
            lines.append(f"    - {issue}")
    for warning in doctor.get("warnings") or []:
        code = warning.get("code") or "unknown"
        message = warning.get("message") or "doctor check warned"
        lines.append(f"  - Warning `{code}`: {message}")
    return lines


def _stewardship_markdown_not_assessed(payload: dict[str, Any]) -> str:
    lines = ["## Not Assessed", ""]
    for item in payload.get("not_assessed") or []:
        lines.append(f"- {item.get('id')}: {item.get('reason')}")
    return "\n".join(lines)


def _stewardship_markdown_next_actions(payload: dict[str, Any]) -> str:
    lines = ["## Next Actions", ""]
    for action in payload.get("next_actions") or []:
        lines.append(f"- `{action}`")
    return "\n".join(lines)


def _write_stewardship_artifact(
    root_dir: Path,
    cid: str,
    payload: dict[str, Any],
    *,
    fmt: str,
    output_dir_arg: str | None,
) -> dict[str, Any]:
    output_dir = _resolve_stewardship_output_dir(root_dir, cid, output_dir_arg)
    extension = "json" if fmt == "json" else "md"
    report_path = output_dir / f"{cid}-stewardship-{payload['report_slug']}.{extension}"
    payload["artifact"] = {
        "written": True,
        "path": repo_rel(root_dir, report_path),
        "format": fmt,
    }
    if fmt == "json":
        write_json_file(report_path, payload)
    else:
        write_text_file(report_path, render_stewardship_report_markdown(payload), dry_run=False)
    return payload["artifact"]


def run_stewardship_report(
    *,
    root_dir: Path,
    client_id: str,
    profiles: list[str],
    fmt: str,
    write: bool,
    output_dir_arg: str | None,
) -> int:
    try:
        cid = validate_client_id(client_id)
        _env_values, overlay_path, overlay_runtime_path = client_overlay_location(root_dir, cid)
        if not overlay_path.is_file():
            raise RuntimeError(
                f"Client '{cid}' has no overlay at {overlay_runtime_path}. "
                f"Use 'onboard {cid}' to scaffold it first."
            )
        payload = _build_stewardship_report(root_dir, cid, profiles)
        if write or output_dir_arg:
            _write_stewardship_artifact(
                root_dir,
                cid,
                payload,
                fmt=fmt,
                output_dir_arg=output_dir_arg,
            )
    except RuntimeError as exc:
        if fmt == "json":
            emit_json(classify_error(exc, "stewardship-report"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if fmt == "json":
        emit_json(payload)
    else:
        print(render_stewardship_report_markdown(payload), end="")
    return EXIT_OK


def _focus_step(
    steps: list[dict[str, Any]], is_json: bool, name: str, status: str, detail: Any = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {"step": name, "status": status}
    if detail is not None:
        entry["detail"] = detail
    steps.append(entry)
    if not is_json:
        marker = "ok" if status == "ok" else ("skip" if status == "skip" else "FAIL")
        print(f"[{marker}] {name}")
    return entry


def _focus_emit_simple_error(message: str, is_json: bool) -> int:
    if is_json:
        emit_json({"error": message})
    else:
        print(message, file=sys.stderr)
    return EXIT_ERROR


def _focus_emit_classify_error(
    exc: Exception,
    cid: str,
    steps: list[dict[str, Any]],
    is_json: bool,
    *,
    print_text: bool = True,
) -> int:
    payload: dict[str, Any] = {"client_id": cid, "steps": steps}
    payload.update(classify_error(exc, "focus"))
    if is_json:
        emit_json(payload)
    elif print_text:
        print(str(exc), file=sys.stderr)
    return EXIT_ERROR


def _focus_emit_local_runtime_payload(
    base: dict[str, Any], extra: dict[str, Any], is_json: bool
) -> int:
    payload = {**base, **extra}
    if is_json:
        emit_json(payload)
    elif (payload.get("error") or {}).get("type", "").startswith("LOCAL_RUNTIME_"):
        print_local_runtime_error_text(payload)
    return EXIT_ERROR


def _resolve_resume_focus_state(
    focus_path: Path, client_id: str, profiles: list[str], is_json: bool
) -> tuple[str, list[str], int | None]:
    """Apply .focus.json overrides for --resume; returns (client_id, profiles, exit_code)."""
    if not focus_path.is_file():
        return client_id, profiles, _focus_emit_simple_error(
            "No .focus.json found. Run focus with a client_id first.", is_json
        )
    try:
        saved = json.loads(focus_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return client_id, profiles, _focus_emit_simple_error(
            f"Failed to read .focus.json: {exc}", is_json
        )
    new_client_id = saved.get("client_id", client_id)
    new_profiles = profiles
    if not profiles:
        new_profiles = [
            str(profile)
            for profile in saved.get("active_profiles") or []
            if str(profile).strip() and str(profile).strip() != "core"
        ]
    return new_client_id, new_profiles, None


def _validate_focus_client(
    root_dir: Path, client_id: str, is_json: bool
) -> tuple[str, int | None]:
    """Validate the client id and overlay file; returns (cid, exit_code)."""
    try:
        cid = validate_client_id(client_id)
    except RuntimeError as exc:
        if is_json:
            emit_json(classify_error(exc, "focus"))
        else:
            print(str(exc), file=sys.stderr)
        return "", EXIT_ERROR
    _, overlay_path, overlay_runtime_path = client_overlay_location(root_dir, cid)
    if not overlay_path.is_file():
        err_msg = (
            f"Client '{cid}' has no overlay at {overlay_runtime_path}. "
            f"Use 'onboard {cid}' to scaffold it first."
        )
        if is_json:
            emit_json(classify_error(RuntimeError(err_msg), "focus"))
        else:
            print(err_msg, file=sys.stderr)
        return cid, EXIT_ERROR
    return cid, None


def _build_focus_model(
    root_dir: Path, cid: str, profiles: list[str], steps: list[dict[str, Any]], is_json: bool
) -> tuple[dict[str, Any] | None, int | None]:
    try:
        model = build_runtime_model(root_dir)
        active_profiles = normalize_active_profiles(profiles or [])
        active_clients = normalize_active_clients(model, [cid])
        return filter_model(model, active_profiles, active_clients), None
    except RuntimeError as exc:
        return None, _focus_emit_classify_error(exc, cid, steps, is_json)


def _focus_local_runtime_preflight(
    model: dict[str, Any],
    active_local_profile: str | None,
    cid: str,
    steps: list[dict[str, Any]],
    is_json: bool,
) -> int | None:
    if not active_local_profile:
        return None
    try:
        overlay_host_path = local_runtime_overlay_path(model, cid)
        pre_reconcile = reconcile_local_runtime_env(
            model,
            active_local_profile,
            overlay_path=overlay_host_path,
            dry_run=False,
        )
        if pre_reconcile.get("status") == "blocked":
            _focus_step(
                steps, is_json, "local-runtime-preflight", "fail",
                {"profile": active_local_profile, "error": pre_reconcile.get("error")},
            )
            extra: dict[str, Any] = {}
            if pre_reconcile.get("error"):
                extra["error"] = pre_reconcile["error"]
            return _focus_emit_local_runtime_payload(
                {"client_id": cid, "steps": steps}, extra, is_json
            )
        _focus_step(
            steps, is_json, "local-runtime-preflight", "ok",
            {"profile": active_local_profile, "actions": pre_reconcile.get("actions")},
        )
        return None
    except RuntimeError as exc:
        _focus_step(steps, is_json, "local-runtime-preflight", "fail", {"error": str(exc)})
        return _focus_emit_classify_error(exc, cid, steps, is_json, print_text=False)


def _focus_compose_override_step(
    root_dir: Path, model: dict[str, Any], cid: str,
    steps: list[dict[str, Any]], is_json: bool,
) -> None:
    try:
        override_path = generate_client_compose_override(root_dir, model, cid)
        _focus_step(steps, is_json, "compose-override", "ok", {"path": str(override_path)})
    except Exception as exc:
        _focus_step(steps, is_json, "compose-override", "fail", {"error": str(exc)})


def _focus_sync_step(
    model: dict[str, Any], cid: str, steps: list[dict[str, Any]], is_json: bool,
) -> int | None:
    try:
        sync_actions = sync_runtime(model, dry_run=False)
        _focus_step(steps, is_json, "sync", "ok", {"actions": sync_actions})
        return None
    except RuntimeError as exc:
        _focus_step(steps, is_json, "sync", "fail", {"error": str(exc)})
        return _focus_emit_classify_error(exc, cid, steps, is_json, print_text=False)


def _focus_bridge_freshness_step(
    model: dict[str, Any], cid: str, steps: list[dict[str, Any]], is_json: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bridges = model.get("bridges") or []
    bridge_detail: dict[str, Any] = {}
    if not bridges:
        return bridges, bridge_detail
    overlay_path = None
    for client in model.get("clients") or []:
        if client.get("id") == cid and client.get("_overlay_path"):
            overlay_path = client["_overlay_path"]
            break
    for bridge in bridges:
        bridge_detail[bridge["id"]] = bridge_freshness(bridge, overlay_path)
    all_fresh = all(f.get("fresh") for f in bridge_detail.values())
    action = "skip (fresh)" if all_fresh else "will re-run stale bridges"
    _focus_step(steps, is_json, "bridge-check", "ok", {"bridges": bridge_detail, "action": action})
    return bridges, bridge_detail


def _focus_bootstrap_step(
    model: dict[str, Any],
    root_dir: Path,
    cid: str,
    bridge_detail: dict[str, Any],
    steps: list[dict[str, Any]],
    is_json: bool,
) -> int | None:
    try:
        requested_tasks = select_tasks(model, [])
        tasks = resolve_tasks_for_run(model, requested_tasks)
        if not tasks:
            _focus_step(steps, is_json, "bootstrap", "skip", {"reason": "no tasks declared"})
            return None
        doctor = doctor_results(model, root_dir)
        doctor_failures = [asdict(result) for result in doctor if result.status == "fail"]
        if doctor_failures:
            _focus_step(
                steps, is_json, "bootstrap", "fail",
                {
                    "error": "post-sync doctor checks failed",
                    "checks": [asdict(result) for result in doctor],
                },
            )
            payload = {"client_id": cid, "steps": steps}
            payload.update(structured_error(
                "Pre-bootstrap doctor checks failed after sync.",
                error_type="pre_bootstrap_doctor_failed",
                recoverable=True,
                recovery_hint=(
                    "Run doctor to materialize or mount the remaining required runtime inputs "
                    "before retrying focus."
                ),
                next_actions=[
                    f"doctor --client {cid} --format json",
                    f"logs --client {cid} --format json",
                ],
            ))
            if is_json:
                emit_json(payload)
            else:
                print("Pre-bootstrap doctor checks failed after sync.", file=sys.stderr)
            return EXIT_ERROR
        tasks_to_run: list[dict[str, Any]] = []
        for task in tasks:
            bid = str(task.get("bridge_id", "")).strip()
            if bid and bridge_detail.get(bid, {}).get("fresh"):
                if not is_json:
                    print(f"  [skip] {task['id']} (bridge {bid} outputs are fresh)")
            else:
                tasks_to_run.append(task)
        if tasks_to_run:
            ensure_required_env_files_ready(select_env_files_for_tasks(model, tasks_to_run))
            task_results = run_tasks(model, tasks_to_run, dry_run=False)
            _focus_step(steps, is_json, "bootstrap", "ok", {"tasks": task_results})
        else:
            _focus_step(steps, is_json, "bootstrap", "skip", {"reason": "all bridge tasks fresh"})
        return None
    except RuntimeError as exc:
        _focus_step(steps, is_json, "bootstrap", "fail", {"error": str(exc)})
        err_str = str(exc)
        payload: dict[str, Any] = {"client_id": cid, "steps": steps}
        if any(bid in err_str for bid in bridge_detail):
            payload.update(local_runtime_error(
                "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
                err_str,
                recoverable=True,
                next_action="re-run sync.sh manually to diagnose",
            ))
        else:
            payload.update(classify_error(exc, "focus"))
        if is_json:
            emit_json(payload)
        elif (payload.get("error") or {}).get("type", "").startswith("LOCAL_RUNTIME_"):
            print_local_runtime_error_text(payload)
        return EXIT_ERROR


def _focus_bridge_verify_step(
    bridges: list[dict[str, Any]], cid: str, steps: list[dict[str, Any]], is_json: bool,
) -> int | None:
    if not bridges:
        return None
    missing_outputs: list[str] = []
    for bridge in bridges:
        state = bridge_outputs_state(bridge)
        if state["state"] == "missing":
            missing_outputs.extend(state.get("missing", []))
    if missing_outputs:
        _focus_step(steps, is_json, "bridge-verify", "fail", {"missing": missing_outputs})
        payload = {"client_id": cid, "steps": steps}
        payload.update(local_runtime_error(
            "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            f"Bridge outputs missing after bootstrap: {', '.join(missing_outputs)}",
            recoverable=True,
            next_action="re-run sync.sh manually to diagnose",
        ))
        if is_json:
            emit_json(payload)
        else:
            print_local_runtime_error_text(payload)
        return EXIT_ERROR
    _focus_step(steps, is_json, "bridge-verify", "ok", {"bridges": len(bridges)})
    return None


def _focus_up_step(
    model: dict[str, Any],
    service_filter: list[str],
    wait_seconds: float,
    cid: str,
    steps: list[dict[str, Any]],
    is_json: bool,
) -> int | None:
    try:
        requested_services = select_services(model, service_filter)
        services = resolve_services_for_start(model, requested_services)
        if services:
            ensure_required_env_files_ready(
                select_env_files_for_tasks(
                    model, resolve_tasks_for_services(model, services),
                ) + select_env_files_for_services(model, services)
            )
            service_results = start_services(
                model, services, dry_run=False, wait_seconds=wait_seconds,
            )
            _focus_step(steps, is_json, "up", "ok", {"services": service_results})
        else:
            _focus_step(steps, is_json, "up", "skip", {"reason": "no services in scope"})
        return None
    except RuntimeError as exc:
        _focus_step(steps, is_json, "up", "fail", {"error": str(exc)})
        return _focus_emit_classify_error(exc, cid, steps, is_json, print_text=False)


def _focus_collect_live_step(
    model: dict[str, Any], root_dir: Path, steps: list[dict[str, Any]], is_json: bool,
) -> dict[str, Any]:
    try:
        live = collect_live_state(model, root_dir)
        _focus_step(steps, is_json, "collect", "ok")
        return live
    except Exception as exc:
        _focus_step(steps, is_json, "collect", "fail", {"error": str(exc)})
        return {
            "collected_at": time.time(),
            "repos": [],
            "services": [],
            "checks": [],
            "logs": [],
            "sessions": [],
        }


def _focus_skill_context_step(
    model: dict[str, Any], root_dir: Path, steps: list[dict[str, Any]], is_json: bool,
) -> None:
    try:
        skill_ctx_actions = generate_skill_context(model, root_dir, dry_run=False)
        if skill_ctx_actions:
            _focus_step(steps, is_json, "skill-context", "ok", {"actions": skill_ctx_actions})
        else:
            _focus_step(steps, is_json, "skill-context", "skip", {"reason": "no client context declared"})
    except Exception as exc:
        _focus_step(steps, is_json, "skill-context", "fail", {"error": str(exc)})


def _focus_enriched_context_step(
    model: dict[str, Any], live: dict[str, Any], root_dir: Path,
    context_dir: Path | None, steps: list[dict[str, Any]], is_json: bool,
) -> None:
    try:
        context_actions = sync_live_context(model, live, root_dir, context_dir=context_dir)
        _focus_step(steps, is_json, "context", "ok", {"actions": context_actions})
    except RuntimeError as exc:
        _focus_step(steps, is_json, "context", "fail", {"error": str(exc)})


def _focus_persist_step(
    focus_path: Path,
    model: dict[str, Any],
    cid: str,
    service_filter: list[str],
    root_dir: Path,
    steps: list[dict[str, Any]],
    is_json: bool,
) -> None:
    _, ctx_yaml_path, ctx_runtime_path = client_context_location(root_dir, cid)
    focus_data: dict[str, Any] = {
        "version": 1,
        "client_id": cid,
        "active_profiles": sorted(model.get("active_profiles") or []),
        "focused_at": time.time(),
        "service_filter": service_filter or None,
    }
    if ctx_yaml_path.is_file():
        focus_data["skill_context_path"] = str(ctx_runtime_path)
    try:
        focus_path.write_text(json.dumps(focus_data, indent=2), encoding="utf-8")
        _focus_step(steps, is_json, "persist", "ok")
    except OSError as exc:
        _focus_step(steps, is_json, "persist", "fail", {"error": str(exc)})


def _focus_summary_counts(live: dict[str, Any]) -> dict[str, int]:
    return {
        "repos_present": sum(1 for r in live.get("repos", []) if r.get("present")),
        "repos_dirty": sum(1 for r in live.get("repos", []) if r.get("dirty", 0) > 0),
        "services_running": sum(1 for s in live.get("services", []) if s.get("healthy")),
        "services_down": sum(
            1 for s in live.get("services", [])
            if s.get("state") in ("stopped", "not-running", "declared")
        ),
        "checks_passing": sum(1 for c in live.get("checks", []) if c.get("ok")),
        "checks_total": len(live.get("checks", [])),
        "recent_errors": sum(
            len(lg.get("recent_errors", [])) for lg in live.get("logs", [])
        ),
    }


def _focus_local_runtime_section(
    model: dict[str, Any],
    active_local_profile: str | None,
    bridges: list[dict[str, Any]],
    cid: str,
    live: dict[str, Any],
    steps: list[dict[str, Any]],
    is_json: bool,
) -> dict[str, Any] | None:
    """Build the local_runtime section.

    WG-005 wires focus into the shared reconciliation surface
    (reconcile_local_runtime_env + local_runtime_focus_payload) so focus and
    up agree on the readiness decision and emit the same US-1 shape. When
    no local-* profile is active but bridges are declared, fall back to the
    legacy ad-hoc block for backwards compatibility.
    """
    if active_local_profile:
        try:
            overlay_host_path = local_runtime_overlay_path(model, cid)
            reconcile_result = reconcile_local_runtime_env(
                model,
                active_local_profile,
                overlay_path=overlay_host_path,
                dry_run=False,
            )
            focus_payload = local_runtime_focus_payload(
                model, reconcile_result, client_id=cid,
            )
            section = focus_payload.get("local_runtime")
            if reconcile_result.get("status") == "blocked":
                _focus_step(
                    steps, is_json, "local-runtime-reconcile", "fail",
                    {"profile": active_local_profile, "error": reconcile_result.get("error")},
                )
            else:
                _focus_step(
                    steps, is_json, "local-runtime-reconcile", "ok",
                    {"profile": active_local_profile, "actions": reconcile_result.get("actions")},
                )
            return section
        except Exception as exc:  # pragma: no cover - defensive
            _focus_step(steps, is_json, "local-runtime-reconcile", "fail", {"error": str(exc)})
            return None
    if not bridges:
        return None
    bridge_states_focus = []
    for bridge in bridges:
        state = bridge_outputs_state(bridge)
        bridge_states_focus.append({
            "id": bridge["id"],
            "status": "ready" if state["state"] == "ok" else state["state"],
        })
    return {
        "env_bridge": bridge_states_focus[0] if len(bridge_states_focus) == 1 else bridge_states_focus,
        "services": [
            {"id": s["id"], "state": s.get("state", "stopped")}
            for s in live.get("services", [])
        ],
    }


def _focus_emit_summary(payload: dict[str, Any], summary: dict[str, int], cid: str, is_json: bool) -> None:
    if is_json:
        emit_json(payload)
        return
    print()
    print(f"  Client:    {cid}")
    print(f"  Repos:     {summary['repos_present']} present, {summary['repos_dirty']} dirty")
    print(f"  Services:  {summary['services_running']} running, {summary['services_down']} down")
    print(f"  Checks:    {summary['checks_passing']}/{summary['checks_total']} passing")
    if summary["recent_errors"]:
        print(f"  Errors:    {summary['recent_errors']} recent error(s) in logs")


def run_focus(
    *,
    root_dir: Path,
    client_id: str,
    profiles: list[str],
    service_filter: list[str],
    resume: bool,
    wait_seconds: float,
    fmt: str,
    context_dir: Path | None = None,
) -> int:
    """Focus macro: sync → bootstrap → up → collect live state → generate enriched context."""
    steps: list[dict[str, Any]] = []
    is_json = fmt == "json"
    focus_path = root_dir / FOCUS_STATE_REL

    if resume:
        client_id, profiles, exit_code = _resolve_resume_focus_state(
            focus_path, client_id, profiles, is_json,
        )
        if exit_code is not None:
            return exit_code

    cid, exit_code = _validate_focus_client(root_dir, client_id, is_json)
    if exit_code is not None:
        return exit_code

    model, exit_code = _build_focus_model(root_dir, cid, profiles, steps, is_json)
    if exit_code is not None or model is None:
        return exit_code if exit_code is not None else EXIT_ERROR

    profile_errors = validate_local_runtime_profiles(model)
    if profile_errors:
        return _focus_emit_local_runtime_payload(
            {"client_id": cid, "steps": steps}, profile_errors[0], is_json,
        )

    active_local_profile = local_runtime_active_profile(model)
    exit_code = _focus_local_runtime_preflight(model, active_local_profile, cid, steps, is_json)
    if exit_code is not None:
        return exit_code

    _focus_compose_override_step(root_dir, model, cid, steps, is_json)

    exit_code = _focus_sync_step(model, cid, steps, is_json)
    if exit_code is not None:
        return exit_code

    bridges, bridge_detail = _focus_bridge_freshness_step(model, cid, steps, is_json)

    exit_code = _focus_bootstrap_step(model, root_dir, cid, bridge_detail, steps, is_json)
    if exit_code is not None:
        return exit_code

    exit_code = _focus_bridge_verify_step(bridges, cid, steps, is_json)
    if exit_code is not None:
        return exit_code

    exit_code = _focus_up_step(model, service_filter, wait_seconds, cid, steps, is_json)
    if exit_code is not None:
        return exit_code

    live = _focus_collect_live_step(model, root_dir, steps, is_json)
    _focus_skill_context_step(model, root_dir, steps, is_json)
    _focus_enriched_context_step(model, live, root_dir, context_dir, steps, is_json)
    _focus_persist_step(focus_path, model, cid, service_filter, root_dir, steps, is_json)

    summary = _focus_summary_counts(live)
    local_runtime_section = _focus_local_runtime_section(
        model, active_local_profile, bridges, cid, live, steps, is_json,
    )
    has_fail = any(s.get("status") == "fail" for s in steps)
    payload: dict[str, Any] = {
        "client_id": cid,
        "active_profiles": sorted(model.get("active_profiles") or []),
        "steps": steps,
        "live_state": live,
        "summary": summary,
        "next_actions": next_actions_for_focus(cid, has_fail, live.get("services") or []),
    }
    if local_runtime_section:
        payload["local_runtime"] = local_runtime_section

    log_runtime_event("focus.activated", cid, payload.get("summary", {}), root_dir)
    _focus_emit_summary(payload, summary, cid, is_json)
    return EXIT_DRIFT if has_fail else EXIT_OK


def run_up(
    *,
    model: dict[str, Any],
    client_id: str,
    profile: str,
    requested_mode: str,
    service_filter: list[str] | None = None,
    dry_run: bool = False,
    wait_seconds: float = 0.0,
) -> tuple[int, dict[str, Any]]:
    """Mode-aware up orchestration for local_runtime_core_cutover (WG-005).

    Implements the contract from shared.md:428-469 and backend.md:25-35:

      1. Reconcile bridge/env readiness via the WG-004 helper.  Any blocked
         result is returned as-is with the LOCAL_RUNTIME_* code that
         reconciliation produced -- NO service mutation happens.
      2. Resolve the requested service graph (topologically sorted, with
         every declared dependency included).
      3. Validate the effective mode against every requested service BEFORE
         any mutation.  Mixed support rejects the whole request with
         LOCAL_RUNTIME_MODE_UNSUPPORTED (backend.md:33-35, Rule 2).
      4. Run bootstrap tasks in declared dependency order.
      5. Start services via their mode-specific declared commands, gating
         each downstream service on the upstream health check.  Any failure
         returns LOCAL_RUNTIME_START_BLOCKED with the full list of services
         that never started.

    Returns a ``(exit_code, payload)`` tuple.  The payload shape matches
    shared.md:435-469 on both happy and blocked paths.
    """
    effective_mode = (requested_mode or "").strip() or "reuse"
    if effective_mode not in LOCAL_RUNTIME_START_MODES:
        supported = ", ".join(LOCAL_RUNTIME_START_MODES)
        payload: dict[str, Any] = {
            "client_id": client_id,
            "profile": profile,
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "bootstrap_tasks": [],
            "services": [],
        }
        payload.update(
            local_runtime_error(
                LOCAL_RUNTIME_MODE_UNSUPPORTED,
                (
                    f"Unsupported --mode value {effective_mode!r}. "
                    f"Supported modes: {supported}."
                ),
                recoverable=True,
                next_action=f"Re-run with --mode <{'|'.join(LOCAL_RUNTIME_START_MODES)}>.",
            )
        )
        payload["error"]["requested_mode"] = requested_mode
        return EXIT_ERROR, payload

    # (0) WG-006: parity-ledger enforcement.  Before ANY mutation -- even
    # bridge reconciliation -- reject direct --service requests that the
    # parity ledger classifies as deferred/bridge-only/external.  This
    # closes US-4 so operators cannot silently fall into an inconsistent
    # state when asking for an uncovered surface (flows.md Flow 5,
    # backend.md:159-169).
    if service_filter:
        classification = classify_requested_surfaces(model, service_filter)
        if classification["deferred"]:
            surface_id, item = classification["deferred"][0]
            deferred_payload: dict[str, Any] = {
                "client_id": client_id,
                "profile": profile,
                "requested_mode": requested_mode,
                "effective_mode": effective_mode,
                "bootstrap_tasks": [],
                "services": [],
            }
            deferred_payload.update(
                build_local_runtime_service_deferred_error(
                    item,
                    client_id=client_id,
                    profile=profile,
                    requested_mode=requested_mode,
                    surface_id=surface_id,
                )
            )
            return EXIT_ERROR, deferred_payload
        if classification["unknown"]:
            unknown_ids = list(classification["unknown"])
            available_services = sorted(
                str(service.get("id", "")).strip()
                for service in model.get("services") or []
                if str(service.get("id", "")).strip()
            )
            message = f"Unknown service id(s): {', '.join(unknown_ids)}."
            if available_services:
                message += f" Available services: {', '.join(available_services)}."
            payload = {
                "client_id": client_id,
                "profile": profile,
                "requested_mode": requested_mode,
                "effective_mode": effective_mode,
                "bootstrap_tasks": [],
                "services": [],
            }
            payload.update(
                structured_error(
                    message,
                    error_type="unknown_service",
                    recoverable=True,
                    recovery_hint=(
                        "Use a declared runtime service id or inspect the "
                        "parity ledger for deferred legacy surfaces."
                    ),
                    next_actions=[
                        (
                            f"manage.py render --client {client_id} "
                            f"--profile {profile} --format json"
                        ),
                    ],
                )
            )
            payload["error"]["requested_mode"] = requested_mode
            payload["error"]["blocked_services"] = unknown_ids
            payload["error"]["available_services"] = available_services
            return EXIT_ERROR, payload

    # (1) Reconcile bridge/env before any mutation (backend.md Rule 3 + 4).
    overlay_host_path = local_runtime_overlay_path(model, client_id)
    reconcile_result = reconcile_local_runtime_env(
        model,
        profile,
        overlay_path=overlay_host_path,
        dry_run=dry_run,
    )
    if reconcile_result.get("status") == "blocked":
        payload: dict[str, Any] = {
            "client_id": client_id,
            "profile": profile,
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "bootstrap_tasks": [],
            "services": [],
        }
        err = reconcile_result.get("error") or {}
        payload["error"] = dict(err)
        payload["error"].setdefault("requested_mode", requested_mode)
        payload["error"].setdefault("blocked_services", [])
        return EXIT_ERROR, payload

    # (2) Resolve the requested service graph.
    filter_ids = [s for s in (service_filter or []) if s]
    if filter_ids:
        requested = select_services(model, filter_ids)
    else:
        requested = select_local_runtime_services(model, profile)
    ordered_services = resolve_services_for_start(
        model, requested, mode=effective_mode,
    )

    if not ordered_services:
        payload = {
            "client_id": client_id,
            "profile": profile,
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "bootstrap_tasks": [],
            "services": [],
        }
        payload.update(local_runtime_error(
            LOCAL_RUNTIME_PROFILE_UNKNOWN,
            f"Profile {profile!r} has no declared local-runtime services.",
            recoverable=False,
        ))
        payload["error"]["requested_mode"] = requested_mode
        return EXIT_ERROR, payload

    # (3) Pre-mutation mode validation (backend.md Rule 2 / shared.md US-2).
    unsupported = validate_services_support_mode(ordered_services, effective_mode)
    if unsupported:
        payload = {
            "client_id": client_id,
            "profile": profile,
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "bootstrap_tasks": [],
            "services": [],
        }
        payload.update(local_runtime_error(
            LOCAL_RUNTIME_MODE_UNSUPPORTED,
            (
                f"Mode {effective_mode!r} is not supported by all requested "
                f"services: {', '.join(unsupported)}"
            ),
            recoverable=True,
            blocked_services=unsupported,
            next_action=(
                f"Re-run with a mode declared by every service in {profile}."
            ),
        ))
        payload["error"]["requested_mode"] = requested_mode
        return EXIT_ERROR, payload

    # (4) Bootstrap tasks (env bridge + repo-specific DB bootstraps, etc.) in declared
    #     dependency order (shared.md:116-137, flows.md:69-89).
    bootstrap_task_specs = resolve_tasks_for_services(model, ordered_services)
    bootstrap_results: list[dict[str, Any]] = []
    if bootstrap_task_specs and not dry_run:
        try:
            ensure_required_env_files_ready(
                select_env_files_for_tasks(model, bootstrap_task_specs)
                + select_env_files_for_services(model, ordered_services)
            )
        except RuntimeError as exc:
            payload = {
                "client_id": client_id,
                "profile": profile,
                "requested_mode": requested_mode,
                "effective_mode": effective_mode,
                "bootstrap_tasks": [],
                "services": [],
            }
            payload.update(local_runtime_error(
                LOCAL_RUNTIME_ENV_OUTPUT_MISSING,
                str(exc),
                recoverable=True,
                blocked_services=[str(s.get("id", "")) for s in ordered_services],
                next_action=(
                    f"manage.py focus --client {client_id} --profile {profile}"
                ),
            ))
            payload["error"]["requested_mode"] = requested_mode
            return EXIT_ERROR, payload

    try:
        bootstrap_results = run_tasks(
            model,
            bootstrap_task_specs,
            dry_run=dry_run,
            mode=effective_mode,
        )
    except RuntimeError as exc:
        payload = {
            "client_id": client_id,
            "profile": profile,
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "bootstrap_tasks": [
                {"id": str(t.get("id", "")), "status": "planned"}
                for t in bootstrap_task_specs
            ],
            "services": [],
        }
        payload.update(local_runtime_error(
            LOCAL_RUNTIME_START_BLOCKED,
            f"Bootstrap task failed: {exc}",
            recoverable=True,
            blocked_services=[str(s.get("id", "")) for s in ordered_services],
            next_action=(
                f"manage.py status --client {client_id} --profile {profile}"
            ),
        ))
        payload["error"]["requested_mode"] = requested_mode
        return EXIT_ERROR, payload

    bootstrap_summary = [
        {
            "id": str(entry.get("id", "")),
            "status": "ok"
            if entry.get("result") in {"ready", "completed", "dry-run"}
            else "pending",
        }
        for entry in bootstrap_results
    ]

    # (5) Start the services in topological order.  Dry-run short-circuits
    #     before any mutation and returns the planned launch order.
    if dry_run:
        planned_services = []
        for service in ordered_services:
            svc_id = str(service.get("id", ""))
            mode_command = resolve_service_mode_command(service, effective_mode)
            planned_services.append({
                "id": svc_id,
                "state": "planned",
                "mode": effective_mode,
                "command": mode_command,
            })
        payload = {
            "client_id": client_id,
            "profile": profile,
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "dry_run": True,
            "bootstrap_tasks": bootstrap_summary
            or [
                {"id": str(t.get("id", "")), "status": "planned"}
                for t in bootstrap_task_specs
            ],
            "services": planned_services,
        }
        return EXIT_OK, payload

    started = start_services(
        model,
        ordered_services,
        dry_run=False,
        wait_seconds=wait_seconds,
        mode=effective_mode,
    )

    # Merge the shared.md:435-469 ``state`` field onto the raw start_services
    # entries so legacy ``result``/``pid``/``log_file`` keys are preserved
    # for existing lifecycle tests (WG-006).
    services_payload = []
    has_failure = False
    for entry in started:
        enriched = dict(entry)
        result_val = entry.get("result", "unknown")
        if result_val in {"started", "already-running"}:
            enriched["state"] = "running"
        elif result_val == "timeout":
            enriched["state"] = "starting"
            has_failure = True
        elif result_val == "failed":
            enriched["state"] = "failed"
            has_failure = True
        else:
            enriched["state"] = result_val
        services_payload.append(enriched)

    payload = {
        "client_id": client_id,
        "profile": profile,
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
        "bootstrap_tasks": bootstrap_summary,
        "services": services_payload,
    }

    if has_failure:
        failed_ids = [
            str(e.get("id", ""))
            for e in started
            if e.get("result") in {"failed", "timeout"}
        ]
        payload.update(local_runtime_error(
            LOCAL_RUNTIME_START_BLOCKED,
            f"Some services did not become healthy: {', '.join(failed_ids)}",
            recoverable=True,
            blocked_services=failed_ids,
            next_action=(
                f"manage.py status --client {client_id} --profile {profile}"
            ),
        ))
        payload["error"]["requested_mode"] = requested_mode
        return EXIT_ERROR, payload

    return EXIT_OK, payload
