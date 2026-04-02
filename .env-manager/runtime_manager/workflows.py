from __future__ import annotations

from .shared import *
from .validation import *
from .runtime_ops import *
from .context_rendering import *

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
    emit_event("onboard.completed", cid, {
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
    onboard_needed = created_client or scaffold_inputs_present

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
        step(
            "onboard",
            "skip",
            {
                "reason": f"client overlay already present at {overlay_runtime_path}",
            },
        )

    profile_args = [arg for profile in profiles for arg in ("--profile", profile)]
    acceptance_code, acceptance_payload = run_manage_json_command(
        root_dir,
        ["acceptance", cid, *profile_args, "--format", "json"],
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


def run_manage_json_command(root_dir: Path, args: list[str]) -> tuple[int, dict[str, Any]]:
    cmd = [sys.executable, str(SCRIPT_DIR / "manage.py"), "--root-dir", str(root_dir), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    stdout = proc.stdout.strip()
    if not stdout:
        payload: dict[str, Any] = {}
    else:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = {"stdout": stdout}
        payload = parsed if isinstance(parsed, dict) else {"payload": parsed}
    if proc.stderr.strip():
        payload["_stderr"] = proc.stderr.strip()
    return proc.returncode, payload


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
        raise RuntimeError(f"Missing MCP config at {repo_rel(root_dir, config_path)}.")
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
    for request in requested_mcp_servers(model):
        server_name = str(request["name"])
        config = server_configs.get(server_name)
        if not isinstance(config, dict):
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


def run_acceptance(
    *,
    root_dir: Path,
    client_id: str,
    profiles: list[str],
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

    profile_args = [arg for profile in profiles for arg in ("--profile", profile)]
    doctor_args = ["doctor", "--client", cid, *profile_args, "--format", "json"]
    sync_args = ["sync", "--client", cid, *profile_args, "--format", "json"]
    focus_args = ["focus", cid, *profile_args, "--format", "json"]

    doctor_pre_code, doctor_pre_payload = run_manage_json_command(root_dir, doctor_args)
    doctor_pre_status = doctor_step_status(doctor_pre_payload, doctor_pre_code)
    step("doctor-pre", doctor_pre_status, {"checks": doctor_pre_payload.get("checks") or []})
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

    doctor_post_code, doctor_post_payload = run_manage_json_command(root_dir, doctor_args)
    doctor_post_status = doctor_step_status(doctor_post_payload, doctor_post_code)
    step("doctor-post", doctor_post_status, {"checks": doctor_post_payload.get("checks") or []})

    ready = mcp_ok and doctor_post_status != "fail"
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
                "type": "doctor_post_failed",
                "message": "Post-focus doctor checks failed.",
                "recoverable": True,
            }
        )
    return emit_acceptance(payload)


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

    def step(name: str, status: str, detail: Any = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"step": name, "status": status}
        if detail is not None:
            entry["detail"] = detail
        steps.append(entry)
        if not is_json:
            marker = "ok" if status == "ok" else ("skip" if status == "skip" else "FAIL")
            print(f"[{marker}] {name}")
        return entry

    focus_path = root_dir / FOCUS_STATE_REL

    # --- Resume path ----------------------------------------------------------
    if resume:
        if not focus_path.is_file():
            err = {"error": "No .focus.json found. Run focus with a client_id first."}
            if is_json:
                emit_json(err)
            else:
                print(err["error"], file=sys.stderr)
            return EXIT_ERROR
        try:
            saved = json.loads(focus_path.read_text(encoding="utf-8"))
            client_id = saved.get("client_id", client_id)
            if not profiles:
                profiles = [
                    str(profile)
                    for profile in saved.get("active_profiles") or []
                    if str(profile).strip() and str(profile).strip() != "core"
                ]
        except (json.JSONDecodeError, OSError) as exc:
            err = {"error": f"Failed to read .focus.json: {exc}"}
            if is_json:
                emit_json(err)
            else:
                print(err["error"], file=sys.stderr)
            return EXIT_ERROR

    # --- Validate client exists -----------------------------------------------
    try:
        cid = validate_client_id(client_id)
    except RuntimeError as exc:
        if is_json:
            emit_json(classify_error(exc, "focus"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

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
        return EXIT_ERROR

    # --- Build model ----------------------------------------------------------
    try:
        model = build_runtime_model(root_dir)
        active_profiles = normalize_active_profiles(profiles or [])
        active_clients = normalize_active_clients(model, [cid])
        model = filter_model(model, active_profiles, active_clients)
    except RuntimeError as exc:
        payload: dict[str, Any] = {"client_id": cid, "steps": steps}
        payload.update(classify_error(exc, "focus"))
        if is_json:
            emit_json(payload)
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    # --- 0. Compose override ---------------------------------------------------
    try:
        override_path = generate_client_compose_override(root_dir, model, cid)
        step("compose-override", "ok", {"path": str(override_path)})
    except Exception as exc:
        step("compose-override", "fail", {"error": str(exc)})

    # --- 1. Sync --------------------------------------------------------------
    try:
        sync_actions = sync_runtime(model, dry_run=False)
        step("sync", "ok", {"actions": sync_actions})
    except RuntimeError as exc:
        step("sync", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "steps": steps}
        payload.update(classify_error(exc, "focus"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # --- 2. Bootstrap ---------------------------------------------------------
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
        payload = {"client_id": cid, "steps": steps}
        payload.update(classify_error(exc, "focus"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # --- 3. Up ----------------------------------------------------------------
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
            step("up", "ok", {"services": service_results})
        else:
            step("up", "skip", {"reason": "no services in scope"})
    except RuntimeError as exc:
        step("up", "fail", {"error": str(exc)})
        payload = {"client_id": cid, "steps": steps}
        payload.update(classify_error(exc, "focus"))
        if is_json:
            emit_json(payload)
        return EXIT_ERROR

    # --- 4. Collect live state ------------------------------------------------
    try:
        live = collect_live_state(model, root_dir)
        step("collect", "ok")
    except Exception as exc:
        step("collect", "fail", {"error": str(exc)})
        live = {
            "collected_at": time.time(),
            "repos": [],
            "services": [],
            "checks": [],
            "logs": [],
            "sessions": [],
        }

    # --- 5. Generate skill context.yaml ---------------------------------------
    try:
        skill_ctx_actions = generate_skill_context(model, root_dir, dry_run=False)
        if skill_ctx_actions:
            step("skill-context", "ok", {"actions": skill_ctx_actions})
        else:
            step("skill-context", "skip", {"reason": "no client context declared"})
    except Exception as exc:
        step("skill-context", "fail", {"error": str(exc)})

    # --- 6. Generate enriched context -----------------------------------------
    try:
        context_actions = sync_live_context(model, live, root_dir, context_dir=context_dir)
        step("context", "ok", {"actions": context_actions})
    except RuntimeError as exc:
        step("context", "fail", {"error": str(exc)})

    # --- 7. Persist focus state -----------------------------------------------
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
        focus_path.write_text(
            json.dumps(focus_data, indent=2), encoding="utf-8",
        )
        step("persist", "ok")
    except OSError as exc:
        step("persist", "fail", {"error": str(exc)})

    # --- Build summary --------------------------------------------------------
    has_fail = any(s.get("status") == "fail" for s in steps)

    # Compact counts for text output
    repos_present = sum(1 for r in live.get("repos", []) if r.get("present"))
    repos_dirty = sum(1 for r in live.get("repos", []) if r.get("dirty", 0) > 0)
    svcs_running = sum(1 for s in live.get("services", []) if s.get("healthy"))
    svcs_down = sum(
        1 for s in live.get("services", [])
        if s.get("state") in ("stopped", "not-running", "declared")
    )
    checks_ok = sum(1 for c in live.get("checks", []) if c.get("ok"))
    checks_total = len(live.get("checks", []))
    error_count = sum(
        len(lg.get("recent_errors", []))
        for lg in live.get("logs", [])
    )

    payload = {
        "client_id": cid,
        "steps": steps,
        "live_state": live,
        "summary": {
            "repos_present": repos_present,
            "repos_dirty": repos_dirty,
            "services_running": svcs_running,
            "services_down": svcs_down,
            "checks_passing": checks_ok,
            "checks_total": checks_total,
            "recent_errors": error_count,
        },
        "next_actions": next_actions_for_focus(cid, has_fail, live.get("services") or []),
    }

    emit_event("focus.activated", cid, payload.get("summary", {}), root_dir)

    if is_json:
        emit_json(payload)
    else:
        print()
        print(f"  Client:    {cid}")
        print(f"  Repos:     {repos_present} present, {repos_dirty} dirty")
        print(f"  Services:  {svcs_running} running, {svcs_down} down")
        print(f"  Checks:    {checks_ok}/{checks_total} passing")
        if error_count:
            print(f"  Errors:    {error_count} recent error(s) in logs")

    return EXIT_DRIFT if has_fail else EXIT_OK
