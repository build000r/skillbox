from __future__ import annotations

import socket

from .shared import *
from .validation import *
from lib.runtime_model import (
    PERSISTENT_PATH_OFF_STATE_ROOT,
    STATE_ROOT_LOW_SPACE,
    STATE_ROOT_MISSING,
    STATE_ROOT_WRONG_FILESYSTEM,
    STATE_ROOT_WRONG_OWNERSHIP,
)


def _storage_filesystem_type(path: Path) -> str:
    result = run_command(["findmnt", "-no", "FSTYPE", "--target", str(path)])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def validate_storage_posture(model: dict[str, Any]) -> list[CheckResult]:
    storage = model.get("storage") or {}
    bindings = storage.get("bindings") or []
    if not isinstance(storage, dict) or not bindings:
        return []

    provider = str(storage.get("provider") or "local").strip() or "local"
    required = bool(storage.get("required"))
    expected_filesystem = str(storage.get("filesystem") or "").strip()
    min_free_gb = float(storage.get("min_free_gb") or 0.0)
    state_root = Path(str(storage.get("state_root") or "")).expanduser()
    persistent_bindings = [
        binding for binding in bindings
        if str(binding.get("storage_class") or "").strip() == "persistent"
    ]

    if not str(state_root):
        return [
            CheckResult(
                status="fail",
                code=STATE_ROOT_MISSING,
                message="storage summary does not include a state_root",
            )
        ]

    results: list[CheckResult] = []
    missing_status = "fail" if provider == "digitalocean" or required else "warn"
    if not state_root.exists():
        results.append(
            CheckResult(
                status=missing_status,
                code=STATE_ROOT_MISSING,
                message="state root is missing",
                details={"state_root": str(state_root), "provider": provider},
            )
        )
        return results

    if provider == "digitalocean" and required and not state_root.is_mount():
        results.append(
            CheckResult(
                status="fail",
                code=STATE_ROOT_MISSING,
                message="DigitalOcean state root exists but is not mounted",
                details={"state_root": str(state_root)},
            )
        )

    if provider == "digitalocean" and expected_filesystem:
        actual_filesystem = _storage_filesystem_type(state_root)
        if actual_filesystem and actual_filesystem != expected_filesystem:
            results.append(
                CheckResult(
                    status="fail",
                    code=STATE_ROOT_WRONG_FILESYSTEM,
                    message="state root filesystem does not match policy",
                    details={
                        "state_root": str(state_root),
                        "expected_filesystem": expected_filesystem,
                        "actual_filesystem": actual_filesystem,
                    },
                )
            )

    if not os.access(state_root, os.W_OK | os.X_OK):
        results.append(
            CheckResult(
                status="fail",
                code=STATE_ROOT_WRONG_OWNERSHIP,
                message="state root is not writable by the current runtime user",
                details={"state_root": str(state_root)},
            )
        )

    usage = shutil.disk_usage(state_root)
    free_gb = round(usage.free / (1024 ** 3), 2)
    if free_gb < min_free_gb:
        results.append(
            CheckResult(
                status="fail",
                code=STATE_ROOT_LOW_SPACE,
                message="state root free space is below the configured minimum",
                details={
                    "state_root": str(state_root),
                    "required_free_gb": min_free_gb,
                    "actual_free_gb": free_gb,
                },
            )
        )

    state_root_resolved = state_root.resolve()
    for binding in persistent_bindings:
        resolved_host_path = Path(str(binding.get("resolved_host_path") or "")).expanduser()
        override_env = str(binding.get("override_env") or "").strip()
        try:
            resolved_host_path.resolve().relative_to(state_root_resolved)
        except ValueError:
            if override_env:
                continue
            results.append(
                CheckResult(
                    status="fail",
                    code=PERSISTENT_PATH_OFF_STATE_ROOT,
                    message="persistent binding resolves outside SKILLBOX_STATE_ROOT",
                    details={
                        "binding_id": str(binding.get("id") or ""),
                        "state_root": str(state_root),
                        "resolved_host_path": str(resolved_host_path),
                    },
                )
            )

    if results:
        return results

    return [
        CheckResult(
            status="pass",
            code="storage-posture",
            message="storage posture matches the compiled persistence contract",
            details={
                "provider": provider,
                "state_root": str(state_root),
                "persistent_bindings": [str(binding.get("id") or "") for binding in persistent_bindings],
                "free_gb": free_gb,
            },
        )
    ]

def normalize_file_mode(raw_mode: Any, default: int = 0o600) -> int:
    if raw_mode is None:
        return default
    if isinstance(raw_mode, int):
        return raw_mode & 0o777

    text = str(raw_mode).strip()
    if not text:
        return default
    try:
        return int(text, 8) & 0o777
    except ValueError as exc:
        raise RuntimeError(f"Invalid file mode {raw_mode!r}. Use an octal string such as '0600'.") from exc


def artifact_state(artifact: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(artifact["host_path"]))
    source = artifact.get("source") or {}
    source_kind = str(source.get("kind", "manual")).strip() or "manual"
    sync = artifact.get("sync") or {}
    sync_mode = str(
        sync.get("mode")
        or ("download-if-missing" if source_kind == "url" else "copy-if-missing" if source_kind == "file" else "manual")
    ).strip()

    source_path: Path | None = None
    raw_source_path = str(source.get("host_path") or source.get("path") or "").strip()
    if raw_source_path:
        source_path = Path(raw_source_path)

    present = path.is_file()
    source_present = bool(source_path and source_path.is_file())
    actual_sha256 = file_sha256(path) if present else ""
    desired_sha256 = ""
    state = "ok" if present else "missing"
    syncable = False

    if source_kind == "file" and sync_mode == "copy-if-missing":
        if source_present and source_path is not None:
            desired_sha256 = file_sha256(source_path)
            syncable = True
            if not present or actual_sha256 != desired_sha256:
                state = "missing" if not present else "stale"
            else:
                state = "ok"
        else:
            state = "present" if present else "missing"
    elif source_kind == "url" and sync_mode == "download-if-missing":
        url = str(source.get("url") or "").strip()
        raw_sha256 = str(source.get("sha256") or "").strip().lower()
        if url:
            syncable = True
        if SHA256_HEX_PATTERN.fullmatch(raw_sha256):
            desired_sha256 = raw_sha256
            if not present or actual_sha256 != desired_sha256:
                state = "missing" if not present else "stale"
            else:
                state = "ok"
        else:
            state = "present" if present else "missing"
    elif not present:
        state = "missing"
    else:
        state = "present"

    return {
        "id": artifact["id"],
        "kind": artifact.get("kind", "artifact"),
        "path": str(artifact.get("path") or artifact["host_path"]),
        "host_path": str(path),
        "present": present,
        "required": bool(artifact.get("required")),
        "profiles": artifact.get("profiles") or [],
        "source_kind": source_kind,
        "source_path": str(source.get("path") or ""),
        "source_host_path": str(source_path) if source_path else "",
        "source_present": source_present,
        "sync_mode": sync_mode,
        "state": state,
        "syncable": syncable,
        "actual_sha256": actual_sha256,
        "desired_sha256": desired_sha256,
    }


def env_file_state(env_file: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(env_file["host_path"]))
    source = env_file.get("source") or {}
    source_kind = str(source.get("kind", "manual")).strip() or "manual"
    sync = env_file.get("sync") or {}
    sync_mode = str(sync.get("mode") or ("write" if source_kind == "file" else "manual")).strip()
    desired_mode = normalize_file_mode(env_file.get("mode"), default=0o600)

    source_path: Path | None = None
    raw_source_path = str(source.get("host_path") or source.get("path") or "").strip()
    if raw_source_path:
        source_path = Path(raw_source_path)

    present = path.is_file()
    source_present = bool(source_path and source_path.is_file())
    state = "ok" if present else "missing"
    syncable = False

    if source_kind == "file" and sync_mode == "write":
        if not source_present:
            state = "source-missing"
        elif not present:
            state = "missing"
            syncable = True
        else:
            target_mode = path.stat().st_mode & 0o777
            if path.read_bytes() != source_path.read_bytes() or target_mode != desired_mode:
                state = "stale"
                syncable = True
            else:
                state = "ok"
    elif not present:
        state = "missing"

    return {
        "id": env_file["id"],
        "kind": env_file.get("kind", "env-file"),
        "repo": str(env_file.get("repo") or ""),
        "path": str(env_file["path"]),
        "host_path": str(path),
        "present": present,
        "required": bool(env_file.get("required")),
        "profiles": env_file.get("profiles") or [],
        "source_kind": source_kind,
        "source_path": str(source.get("path") or ""),
        "source_host_path": str(source_path) if source_path else "",
        "source_present": source_present,
        "sync_mode": sync_mode,
        "mode": f"{desired_mode:04o}",
        "state": state,
        "syncable": syncable,
    }


def check_filesystem(model: dict[str, Any], root_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    missing_syncable_repo_paths: list[str] = []
    missing_required_repo_paths: list[str] = []
    missing_syncable_artifact_paths: list[str] = []
    stale_syncable_artifact_paths: list[str] = []
    missing_required_artifact_paths: list[str] = []
    syncable_env_files: list[str] = []
    missing_required_env_sources: list[str] = []
    missing_required_env_targets: list[str] = []
    missing_log_paths: list[str] = []
    missing_required_checks: list[str] = []
    bridge_output_paths = {
        str(path.resolve())
        for bridge in model.get("bridges") or []
        for path in bridge_expected_outputs(bridge)
    }

    for repo in model["repos"]:
        path = Path(str(repo["host_path"]))
        if path.exists():
            continue

        source = repo.get("source") or {}
        source_kind = source.get("kind", "manual")
        sync = repo.get("sync") or {}
        sync_mode = sync.get("mode") or (
            "ensure-directory" if source_kind == "directory" else "external"
        )

        if sync_mode in {"ensure-directory", "clone-if-missing"} or source_kind in {"directory", "git"}:
            missing_syncable_repo_paths.append(repo_rel(root_dir, path))
        elif repo.get("required"):
            missing_required_repo_paths.append(repo_rel(root_dir, path))

    for artifact in model["artifacts"]:
        state = artifact_state(artifact)
        display_path = repo_rel(root_dir, Path(state["host_path"]))

        if state["state"] == "missing":
            if state["syncable"]:
                missing_syncable_artifact_paths.append(display_path)
            elif artifact.get("required"):
                missing_required_artifact_paths.append(display_path)
        elif state["state"] == "stale" and state["syncable"]:
            stale_syncable_artifact_paths.append(display_path)

    for env_file in model["env_files"]:
        state = env_file_state(env_file)
        display_path = repo_rel(root_dir, Path(state["host_path"]))
        if state["state"] == "source-missing":
            if env_file.get("required"):
                source_host_path = str(Path(state["source_host_path"]).resolve()) if state["source_host_path"] else ""
                if source_host_path and source_host_path in bridge_output_paths:
                    continue
                if state["source_host_path"]:
                    missing_required_env_sources.append(repo_rel(root_dir, Path(state["source_host_path"])))
                else:
                    missing_required_env_sources.append(state["source_path"] or display_path)
        elif state["state"] in {"missing", "stale"}:
            if state["syncable"]:
                syncable_env_files.append(display_path)
            elif env_file.get("required"):
                missing_required_env_targets.append(display_path)

    for log_item in model["logs"]:
        path = Path(str(log_item["host_path"]))
        if not path.exists():
            missing_log_paths.append(repo_rel(root_dir, path))

    for check in model["checks"]:
        if check.get("type") != "path_exists":
            continue
        path = Path(str(check["host_path"]))
        if not path.exists() and check.get("required"):
            missing_required_checks.append(repo_rel(root_dir, path))

    if missing_required_repo_paths:
        results.append(
            CheckResult(
                status="fail",
                code="required-runtime-paths",
                message="required runtime repo paths are missing",
                details={"missing": missing_required_repo_paths},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="required-runtime-paths",
                message="required runtime repo paths are present",
            )
        )

    if missing_syncable_repo_paths:
        results.append(
            CheckResult(
                status="warn",
                code="syncable-repo-paths",
                message="managed repo paths are missing but can be created by sync",
                details={"missing": missing_syncable_repo_paths},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="syncable-repo-paths",
                message="managed repo paths do not need sync",
            )
        )

    if missing_required_artifact_paths:
        results.append(
            CheckResult(
                status="fail",
                code="required-runtime-artifacts",
                message="required runtime artifact paths are missing",
                details={"missing": missing_required_artifact_paths},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="required-runtime-artifacts",
                message="required runtime artifact paths are present",
            )
        )

    if missing_syncable_artifact_paths or stale_syncable_artifact_paths:
        details: dict[str, Any] = {}
        if missing_syncable_artifact_paths:
            details["missing"] = missing_syncable_artifact_paths
        if stale_syncable_artifact_paths:
            details["stale"] = stale_syncable_artifact_paths
        results.append(
            CheckResult(
                status="warn",
                code="syncable-artifact-paths",
                message="managed artifact paths are missing or stale but can be reconciled by sync",
                details=details,
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="syncable-artifact-paths",
                message="managed artifact paths do not need sync",
            )
        )

    if missing_required_env_sources or missing_required_env_targets:
        details: dict[str, Any] = {}
        if missing_required_env_sources:
            details["missing_sources"] = missing_required_env_sources
        if missing_required_env_targets:
            details["missing_targets"] = missing_required_env_targets
        results.append(
            CheckResult(
                status="fail",
                code="required-runtime-env-files",
                message="required runtime env files cannot be materialized",
                details=details,
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="required-runtime-env-files",
                message="required runtime env files are materialized or source-backed",
            )
        )

    if syncable_env_files:
        results.append(
            CheckResult(
                status="warn",
                code="syncable-env-files",
                message="managed env files are missing or stale but can be materialized by sync",
                details={"targets": syncable_env_files},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="syncable-env-files",
                message="managed env files do not need sync",
            )
        )

    if missing_log_paths:
        results.append(
            CheckResult(
                status="warn",
                code="runtime-log-paths",
                message="managed log directories are missing but can be created by sync",
                details={"missing": missing_log_paths},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="runtime-log-paths",
                message="managed log directories are present",
            )
        )

    if missing_required_checks:
        results.append(
            CheckResult(
                status="fail",
                code="required-runtime-checks",
                message="required runtime checks failed",
                details={"missing": missing_required_checks},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="required-runtime-checks",
                message="required runtime checks passed",
            )
        )

    return results


def validate_task_state(model: dict[str, Any]) -> list[CheckResult]:
    if not model["tasks"]:
        return []

    pending_tasks: list[str] = []
    blocked_tasks: list[str] = []

    for task in model["tasks"]:
        task_state = probe_task(model, task)
        if task_state["state"] == "ready":
            continue

        summary = task["id"]
        if task_state.get("target"):
            summary += f" -> {task_state['target']}"
        if task_state["state"] == "blocked":
            blocked_on = [
                dependency_id
                for dependency_id, dependency_state in task_state.get("dependency_states", {}).items()
                if dependency_state != "ok"
            ]
            if blocked_on:
                summary += f" (blocked by {', '.join(blocked_on)})"
            blocked_tasks.append(summary)
        else:
            pending_tasks.append(summary)

    if pending_tasks or blocked_tasks:
        details: dict[str, Any] = {}
        if pending_tasks:
            details["pending"] = pending_tasks
        if blocked_tasks:
            details["blocked"] = blocked_tasks
        return [
            CheckResult(
                status="warn",
                code="bootstrap-task-state",
                message="bootstrap tasks are pending and can be materialized by bootstrap",
                details=details,
            )
        ]

    return [
        CheckResult(
            status="pass",
            code="bootstrap-task-state",
            message="bootstrap task success checks are satisfied",
        )
    ]


def validate_bridges(model: dict[str, Any]) -> list[CheckResult]:
    results: list[CheckResult] = []
    bridges = bridge_id_map(model)
    for task in model.get("tasks") or []:
        bid = str(task.get("bridge_id", "")).strip()
        if bid and bid not in bridges:
            results.append(CheckResult(
                status="fail",
                code="bridge_reference_missing",
                message=f"Task {task['id']} references bridge {bid!r} which is not declared",
            ))
    for bridge in model.get("bridges") or []:
        state = bridge_outputs_state(bridge)
        if state["state"] == "missing":
            results.append(CheckResult(
                status="warn",
                code="bridge_outputs_missing",
                message=f"Bridge {bridge['id']} has missing outputs",
                details={"missing": state.get("missing", [])},
            ))
        elif state["state"] == "ok":
            results.append(CheckResult(
                status="pass",
                code="bridge_outputs_present",
                message=f"Bridge {bridge['id']} outputs are present",
            ))
    return results


def has_ingress_runtime(model: dict[str, Any]) -> bool:
    return bool(model.get("ingress_routes") or []) or any(
        str(service.get("kind") or "").strip() == "ingress"
        for service in model.get("services") or []
    )


def ingress_listener_settings(model: dict[str, Any], listener: str) -> dict[str, Any]:
    env = model.get("env") or {}
    normalized_listener = "private" if str(listener or "").strip().lower() == "private" else "public"
    host_key = f"SKILLBOX_INGRESS_{normalized_listener.upper()}_HOST"
    port_key = f"SKILLBOX_INGRESS_{normalized_listener.upper()}_PORT"
    base_url_key = f"SKILLBOX_INGRESS_{normalized_listener.upper()}_BASE_URL"

    host = str(env.get(host_key) or "").strip() or "127.0.0.1"
    port_raw = str(env.get(port_key) or "").strip() or ("9080" if normalized_listener == "private" else "8080")
    try:
        port = int(port_raw)
    except ValueError:
        port = 9080 if normalized_listener == "private" else 8080
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    raw_base_url = str(env.get(base_url_key) or "").strip().rstrip("/")
    base_url = raw_base_url or f"http://{display_host}:{port}"
    return {
        "listener": normalized_listener,
        "host": host,
        "port": port,
        "base_url": base_url,
    }


def service_origin_url(service: dict[str, Any] | None) -> str:
    if not service:
        return ""
    url = str(service.get("origin_url") or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def sorted_ingress_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        routes,
        key=lambda route: (
            str(route.get("listener") or "public"),
            0 if str(route.get("match") or "exact") == "exact" else 1,
            -len(str(route.get("path") or "")),
            str(route.get("path") or ""),
            str(route.get("id") or ""),
        ),
    )


def resolved_ingress_routes(
    model: dict[str, Any],
    *,
    include_service_state: bool = False,
) -> list[dict[str, Any]]:
    services_by_id = {
        str(service.get("id") or "").strip(): service
        for service in model.get("services") or []
        if str(service.get("id") or "").strip()
    }
    entries: list[dict[str, Any]] = []
    for route in model.get("ingress_routes") or []:
        listener = str(route.get("listener") or "public").strip().lower() or "public"
        path = str(route.get("path") or "").strip()
        match = str(route.get("match") or "exact").strip().lower() or "exact"
        service_id = str(route.get("service_id") or "").strip()
        service = services_by_id.get(service_id)
        listener_settings = ingress_listener_settings(model, listener)
        request_url = listener_settings["base_url"]
        if path:
            request_url = f"{request_url}{path}"
        entries.append(
            (
                {
                    "id": str(route.get("id") or "").strip(),
                    "client": str(route.get("client") or "").strip(),
                    "profiles": list(route.get("profiles") or []),
                    "listener": listener,
                    "path": path,
                    "match": match,
                    "service_id": service_id,
                    "request_url": request_url,
                    "origin_url": service_origin_url(service),
                }
                | (
                    {
                        "service_state": probe_service(model, service).get("state", "missing")
                        if service else "missing"
                    }
                    if include_service_state else {}
                )
            )
        )
    return sorted_ingress_routes(entries)


def ingress_config_paths(model: dict[str, Any]) -> dict[str, Path]:
    root_dir = Path(str(model["root_dir"]))
    env = model.get("env") or {}
    storage = model.get("storage")
    route_file = runtime_path_to_host_path(
        root_dir,
        env,
        str(env.get("SKILLBOX_INGRESS_ROUTE_FILE") or "/workspace/logs/runtime/ingress-routes.json"),
        storage=storage,
    )
    nginx_config = runtime_path_to_host_path(
        root_dir,
        env,
        str(env.get("SKILLBOX_INGRESS_NGINX_CONFIG") or "/workspace/logs/runtime/ingress-nginx.conf"),
        storage=storage,
    )
    return {
        "route_file": Path(str(route_file)),
        "nginx_config": Path(str(nginx_config)),
    }


def render_ingress_routes_document(model: dict[str, Any]) -> str:
    payload = {
        "version": 1,
        "listeners": {
            "public": ingress_listener_settings(model, "public"),
            "private": ingress_listener_settings(model, "private"),
        },
        "routes": [
            {
                "id": route["id"],
                "client": route["client"],
                "profiles": route["profiles"],
                "listener": route["listener"],
                "path": route["path"],
                "match": route["match"],
                "service_id": route["service_id"],
                "origin_url": route["origin_url"],
            }
            for route in resolved_ingress_routes(model)
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_ingress_nginx_config(model: dict[str, Any]) -> str:
    routes_by_listener: dict[str, list[dict[str, Any]]] = {"public": [], "private": []}
    for route in resolved_ingress_routes(model):
        routes_by_listener.setdefault(route["listener"], []).append(route)

    lines = [
        "# Auto-generated by skillbox runtime manager. Do not edit manually.",
        "events {",
        "  worker_connections 1024;",
        "}",
        "http {",
        "  access_log off;",
        "  sendfile on;",
    ]

    for listener in ("public", "private"):
        settings = ingress_listener_settings(model, listener)
        if settings["host"] in {"0.0.0.0", "::"}:
            listen_value = str(settings["port"])
        else:
            listen_value = f"{settings['host']}:{settings['port']}"
        lines.extend(
            [
                "  server {",
                f"    listen {listen_value};",
                "    server_name _;",
                "    location = /__skillbox/health {",
                "      add_header Content-Type text/plain;",
                "      return 200 'ok';",
                "    }",
            ]
        )
        for route in routes_by_listener.get(listener, []):
            location_modifier = "=" if route["match"] == "exact" else "^~"
            lines.extend(
                [
                    f"    location {location_modifier} {route['path']} {{",
                    f"      proxy_pass {route['origin_url']};",
                    "      proxy_http_version 1.1;",
                    "      proxy_set_header Host $host;",
                    "      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                    "      proxy_set_header X-Forwarded-Host $host;",
                    "      proxy_set_header X-Forwarded-Proto $scheme;",
                    "    }",
                ]
            )
        lines.extend(
            [
                "  }",
            ]
        )

    lines.append("}")
    return "\n".join(lines) + "\n"


def managed_text_artifact_state(path: Path, desired: str) -> str:
    if not path.is_file():
        return "missing"
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        return "stale"
    return "ok" if current == desired else "stale"


def sync_ingress_artifacts(model: dict[str, Any], dry_run: bool) -> list[str]:
    if not has_ingress_runtime(model):
        return []

    paths = ingress_config_paths(model)
    desired_route_payload = render_ingress_routes_document(model)
    desired_nginx_config = render_ingress_nginx_config(model)
    route_count = len(model.get("ingress_routes") or [])
    actions: list[str] = []

    for key, desired in (
        ("route_file", desired_route_payload),
        ("nginx_config", desired_nginx_config),
    ):
        path = paths[key]
        state = managed_text_artifact_state(path, desired)
        ensure_directory(path.parent, dry_run)
        action_label = "render-ingress-routes" if key == "route_file" else "render-ingress-nginx"
        if dry_run:
            actions.append(f"{action_label}: {path} ({route_count} routes)")
            continue
        if state == "ok":
            actions.append(f"{action_label}-unchanged: {path}")
            continue
        path.write_text(desired, encoding="utf-8")
        actions.append(f"{action_label}: {path} ({route_count} routes)")

    return actions


def validate_ingress(model: dict[str, Any]) -> list[CheckResult]:
    if not has_ingress_runtime(model):
        return []
    if not model.get("ingress_routes"):
        return []

    results: list[CheckResult] = []
    routes = resolved_ingress_routes(model)
    invalid_upstreams = [
        route["id"]
        for route in routes
        if route["service_id"] and not route["origin_url"]
    ]
    if invalid_upstreams:
        results.append(
            CheckResult(
                status="fail",
                code="ingress-upstream-missing",
                message="ingress routes require HTTP-backed services with resolvable upstream URLs",
                details={"routes": invalid_upstreams},
            )
        )
        return results

    desired_route_payload = render_ingress_routes_document(model)
    desired_nginx_config = render_ingress_nginx_config(model)
    paths = ingress_config_paths(model)
    route_state = managed_text_artifact_state(paths["route_file"], desired_route_payload)
    nginx_state = managed_text_artifact_state(paths["nginx_config"], desired_nginx_config)

    results.append(
        CheckResult(
            status="pass" if route_state == "ok" else "warn",
            code="ingress-route-manifest",
            message=(
                "ingress route manifest matches the active runtime graph"
                if route_state == "ok"
                else "ingress route manifest needs regeneration"
            ),
            details={"path": str(paths["route_file"]), "state": route_state, "routes": len(routes)},
        )
    )
    results.append(
        CheckResult(
            status="pass" if nginx_state == "ok" else "warn",
            code="ingress-nginx-config",
            message=(
                "ingress nginx config matches the active runtime graph"
                if nginx_state == "ok"
                else "ingress nginx config needs regeneration"
            ),
            details={"path": str(paths["nginx_config"]), "state": nginx_state, "routes": len(routes)},
        )
    )
    return results


def doctor_results(model: dict[str, Any], root_dir: Path) -> list[CheckResult]:
    results = check_manifest(model)
    if any(result.status == "fail" for result in results):
        return results
    connector_results = validate_connector_contract(model)
    if any(result.status == "fail" for result in connector_results):
        return results + connector_results
    # WG-006: fold the parity-ledger drift check into doctor so the
    # LOCAL_RUNTIME_COVERAGE_GAP contract from shared.md:473-507 fires at
    # the observational surface the operator actually runs.  We wrap the
    # underlying CheckResult so the doctor text/json renderers emit the
    # shared contract shape: ``subject=parity_ledger`` and
    # ``details.deferred_surfaces`` populated from the ledger.
    parity_results: list[CheckResult] = []
    for raw in validate_parity_ledger(model):
        details = dict(raw.details or {})
        details.setdefault("subject", "parity_ledger")
        details.setdefault(
            "deferred_surfaces", parity_ledger_deferred_surfaces(model),
        )
        details.setdefault(
            "covered_surfaces", parity_ledger_covered_surfaces(model),
        )
        parity_results.append(
            CheckResult(
                status=raw.status,
                code=raw.code,
                message=raw.message,
                details=details,
            )
        )
    return (
        results
        + connector_results
        + check_filesystem(model, root_dir)
        + validate_skill_repo_sets(model)
        + validate_skill_locks_and_state(model)
        + validate_task_state(model)
        + validate_storage_posture(model)
        + validate_bridges(model)
        + validate_ingress(model)
        + parity_results
    )


def sync_artifact(artifact: dict[str, Any], dry_run: bool) -> list[str]:
    state = artifact_state(artifact)
    path = Path(state["host_path"])
    source = artifact.get("source") or {}
    source_kind = state["source_kind"]
    sync_mode = state["sync_mode"]

    if state["state"] == "ok" or (path.exists() and not state["syncable"]):
        return [f"exists: {path}"]

    if sync_mode == "download-if-missing" and source_kind == "url":
        if not str(source.get("url") or "").strip():
            return [f"skip: {path} (artifact source url missing)"]
        url, expected_sha256 = validate_url_download_source(source, artifact_id=str(artifact["id"]))
        ensure_directory(path.parent, dry_run)
        action_name = "download-reconcile" if state["state"] == "stale" else "download-if-missing"
        if dry_run:
            return [f"{action_name}: {url} -> {path}"]

        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read()
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"artifact {artifact['id']} digest mismatch for {url}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        tmp_path = path.parent / f".{path.name}.tmp"
        tmp_path.write_bytes(payload)
        if source.get("executable", False):
            tmp_path.chmod(0o755)
        tmp_path.replace(path)
        return [f"{action_name}: {url} -> {path}"]

    if sync_mode == "copy-if-missing" and source_kind == "file":
        raw_source_path = str(source.get("host_path") or source.get("path") or "").strip()
        if not raw_source_path:
            return [f"skip: {path} (artifact source path missing)"]
        source_path = Path(raw_source_path)
        ensure_directory(path.parent, dry_run)
        action_name = "copy-reconcile" if state["state"] == "stale" else "copy-if-missing"
        if dry_run:
            return [f"{action_name}: {source_path} -> {path}"]
        if not source_path.is_file():
            raise RuntimeError(f"artifact source file is missing: {source_path}")
        tmp_path = path.parent / f".{path.name}.tmp"
        shutil.copyfile(source_path, tmp_path)
        if source.get("executable", False):
            tmp_path.chmod(0o755)
        tmp_path.replace(path)
        return [f"{action_name}: {source_path} -> {path}"]

    return [f"skip: {path} (sync mode {sync_mode})"]


def sync_env_file(env_file: dict[str, Any], dry_run: bool) -> list[str]:
    state = env_file_state(env_file)
    path = Path(state["host_path"])
    source_path = Path(state["source_host_path"]) if state["source_host_path"] else None

    if state["source_kind"] == "file" and state["sync_mode"] == "write":
        if source_path is None or not source_path.is_file():
            if env_file.get("required"):
                raise RuntimeError(
                    f"Required env file {env_file['id']} is missing source {state['source_path'] or state['source_host_path'] or path}."
                )
            return [f"skip: {path} (env source path missing)"]

        ensure_directory(path.parent, dry_run)
        if dry_run:
            return [f"hydrate-env: {source_path} -> {path}"]

        payload = source_path.read_bytes()
        current_payload = path.read_bytes() if path.is_file() else None
        desired_mode = normalize_file_mode(env_file.get("mode"), default=0o600)
        current_mode = path.stat().st_mode & 0o777 if path.is_file() else None
        if current_payload == payload and current_mode == desired_mode:
            return [f"env-unchanged: {path}"]

        path.write_bytes(payload)
        path.chmod(desired_mode)
        return [f"hydrate-env: {source_path} -> {path}"]

    if path.exists():
        return [f"exists: {path}"]

    if env_file.get("required"):
        raise RuntimeError(f"Required env file {env_file['id']} is missing at {path}.")
    return [f"skip: {path} (sync mode {state['sync_mode']})"]


def sync_dcg_config(model: dict[str, Any], root_dir: Path, dry_run: bool) -> list[str]:
    """Render .dcg.toml from env and client overlay dcg settings."""
    actions: list[str] = []
    env = model.get("env") or {}
    dcg_bin = env.get("SKILLBOX_DCG_BIN", "").strip()
    if not dcg_bin:
        return [f"skip: .dcg.toml (dcg not configured)"]

    packs_raw = env.get("SKILLBOX_DCG_PACKS", "core.git,core.filesystem").strip()
    packs = [p.strip() for p in packs_raw.split(",") if p.strip()]

    # Client overlays can declare extra dcg packs and allowlist rules
    client_dcg = {}
    for client in model.get("clients") or []:
        if "dcg" in client:
            client_dcg = client["dcg"]
            extra_packs = client_dcg.get("packs") or []
            for p in extra_packs:
                if p not in packs:
                    packs.append(p)

    allowlist = client_dcg.get("allowlist") or []

    lines = ["# Auto-generated by skillbox runtime manager. Do not edit manually.", ""]
    lines.append("[packs]")
    lines.append(f"enabled = [{', '.join(repr(p) for p in packs)}]")
    lines.append("")

    if allowlist:
        lines.append("[allowlist]")
        lines.append("rules = [")
        for rule in allowlist:
            lines.append(f"  {rule!r},")
        lines.append("]")
        lines.append("")

    content = "\n".join(lines) + "\n"

    dcg_config_path = root_dir / ".dcg.toml"
    if dcg_config_path.exists():
        existing = dcg_config_path.read_text()
        if existing == content:
            return [f"exists: {dcg_config_path}"]

    if dry_run:
        return [f"render-dcg-config: {dcg_config_path} (packs: {', '.join(packs)})"]

    dcg_config_path.write_text(content)
    actions.append(f"render-dcg-config: {dcg_config_path} (packs: {', '.join(packs)})")
    return actions


def runtime_repo_reference_id(entry: dict[str, Any]) -> str:
    return str(entry.get("repo_id") or entry.get("repo") or "").strip()


def repo_regenerable_file_rel_paths(model: dict[str, Any], repo: dict[str, Any]) -> set[Path]:
    repo_id = str(repo.get("id") or "").strip()
    repo_root = Path(str(repo["host_path"]))
    allowed: set[Path] = set()

    for env_file in model.get("env_files") or []:
        if runtime_repo_reference_id(env_file) != repo_id:
            continue
        try:
            allowed.add(Path(str(env_file["host_path"])).relative_to(repo_root))
        except ValueError:
            continue

    for task in model.get("tasks") or []:
        if runtime_repo_reference_id(task) != repo_id:
            continue
        success = task.get("success") or {}
        if str(success.get("type") or "").strip() != "path_exists":
            continue
        try:
            allowed.add(Path(str(success["host_path"])).relative_to(repo_root))
        except ValueError:
            continue

    return allowed


def repo_has_only_regenerable_git_residue(model: dict[str, Any], repo: dict[str, Any]) -> bool:
    path = Path(str(repo["host_path"]))
    if not path.is_dir():
        return False

    allowed_files = repo_regenerable_file_rel_paths(model, repo)
    for child in path.rglob("*"):
        if child.is_dir():
            continue
        rel_path = child.relative_to(path)
        if rel_path.parts and rel_path.parts[0] == ".skillbox":
            continue
        if rel_path in allowed_files:
            continue
        return False
    return True


def clear_repo_git_residue(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def sync_runtime(model: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []

    for repo in model["repos"]:
        path = Path(str(repo["host_path"]))
        source = repo.get("source") or {}
        source_kind = source.get("kind", "manual")
        sync = repo.get("sync") or {}
        sync_mode = sync.get("mode") or (
            "ensure-directory" if source_kind == "directory" else "external"
        )

        if source_kind == "git" and sync_mode == "clone-if-missing":
            url = str(source["url"])
            branch = str(source.get("branch", "")).strip()
            if path.exists():
                if path.is_dir() and git_repo_state(path).get("git"):
                    actions.append(f"exists: {path}")
                    continue
                if not repo_has_only_regenerable_git_residue(model, repo):
                    actions.append(f"exists: {path}")
                    continue
                if dry_run:
                    actions.append(f"clone-reconcile: {url} -> {path}")
                    continue
                clear_repo_git_residue(path)
                args = ["git", "clone"]
                if branch:
                    args.extend(["--branch", branch])
                args.extend([url, str(path)])
                result = run_command(args)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"git clone failed for {url}")
                actions.append(f"clone-reconcile: {url} -> {path}")
                continue

            parent = path.parent
            ensure_directory(parent, dry_run)
            if dry_run:
                actions.append(f"clone-if-missing: {url} -> {path}")
                continue

            args = ["git", "clone"]
            if branch:
                args.extend(["--branch", branch])
            args.extend([url, str(path)])
            result = run_command(args)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"git clone failed for {url}")
            actions.append(f"clone-if-missing: {url} -> {path}")
            continue

        if path.exists():
            actions.append(f"exists: {path}")
            continue

        if sync_mode == "ensure-directory" or source_kind == "directory":
            ensure_directory(path, dry_run)
            actions.append(f"ensure-directory: {path}")
            continue

        actions.append(f"skip: {path} (sync mode {sync_mode})")

    for artifact in model["artifacts"]:
        actions.extend(sync_artifact(artifact, dry_run=dry_run))

    for env_file in model["env_files"]:
        actions.extend(sync_env_file(env_file, dry_run=dry_run))

    for log_item in model["logs"]:
        path = Path(str(log_item["host_path"]))
        if path.exists():
            actions.append(f"exists: {path}")
            continue
        ensure_directory(path, dry_run)
        actions.append(f"ensure-directory: {path}")

    actions.extend(sync_skill_repo_sets(model, dry_run=dry_run))
    actions.extend(sync_skill_sets(model, dry_run=dry_run))
    actions.extend(sync_dcg_config(model, Path(str(model["root_dir"])), dry_run=dry_run))
    actions.extend(sync_ingress_artifacts(model, dry_run=dry_run))
    if not dry_run:
        log_runtime_event("sync.completed", "runtime", {"action_count": len(actions)})
    return actions


def runtime_log_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(log_item["id"]): log_item
        for log_item in model.get("logs") or []
        if str(log_item.get("id", "")).strip()
    }


def runtime_repo_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(repo["id"]): repo
        for repo in model.get("repos") or []
        if str(repo.get("id", "")).strip()
    }


def task_id_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(task["id"]): task
        for task in model.get("tasks") or []
        if str(task.get("id", "")).strip()
    }


def task_dependency_ids(task: dict[str, Any]) -> list[str]:
    raw_dependencies = task.get("depends_on") or []
    if not isinstance(raw_dependencies, list):
        return []

    dependencies: list[str] = []
    seen: set[str] = set()
    for raw_dependency in raw_dependencies:
        dependency_id = str(raw_dependency).strip()
        if not dependency_id or dependency_id in seen:
            continue
        dependencies.append(dependency_id)
        seen.add(dependency_id)
    return dependencies


def service_bootstrap_task_ids(service: dict[str, Any]) -> list[str]:
    raw_tasks = service.get("bootstrap_tasks") or []
    if not isinstance(raw_tasks, list):
        return []

    task_ids: list[str] = []
    seen: set[str] = set()
    for raw_task in raw_tasks:
        task_id = str(raw_task).strip()
        if not task_id or task_id in seen:
            continue
        task_ids.append(task_id)
        seen.add(task_id)
    return task_ids


def task_dependency_graph(model: dict[str, Any]) -> dict[str, list[str]]:
    return {
        task_id: task_dependency_ids(task)
        for task_id, task in task_id_map(model).items()
    }


def expand_graph_ids(graph: dict[str, list[str]], root_ids: list[str]) -> set[str]:
    expanded = set(root_ids)
    queue = list(root_ids)

    while queue:
        item_id = queue.pop()
        for linked_item_id in graph.get(item_id, []):
            if linked_item_id in expanded:
                continue
            expanded.add(linked_item_id)
            queue.append(linked_item_id)

    return expanded


def order_task_ids(model: dict[str, Any], selected_ids: set[str]) -> list[str]:
    tasks_by_id = task_id_map(model)
    dependency_graph = task_dependency_graph(model)
    ordered_ids: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise RuntimeError(f"Task dependency cycle detected at {task_id}.")
        if task_id not in tasks_by_id:
            raise RuntimeError(f"Task dependency references unknown task {task_id!r}.")

        visiting.add(task_id)
        for dependency_id in dependency_graph.get(task_id, []):
            if dependency_id in selected_ids:
                visit(dependency_id)
        visiting.remove(task_id)
        visited.add(task_id)
        ordered_ids.append(task_id)

    for task in model["tasks"]:
        task_id = str(task.get("id", "")).strip()
        if task_id and task_id in selected_ids:
            visit(task_id)

    return ordered_ids


def service_supports_lifecycle(
    service: dict[str, Any],
    model: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    # A service is manageable if it has either a top-level `command` or a
    # non-empty `commands` mapping (per-mode commands introduced by WG-001 for
    # the local_runtime_core_cutover slice).  The actual mode selection is
    # performed later in start_services / resolve_service_mode_command.
    has_command = bool(str(service.get("command") or "").strip())
    raw_commands = service.get("commands")
    has_mode_commands = isinstance(raw_commands, dict) and any(
        str(v).strip() for v in raw_commands.values()
    )
    if not has_command and not has_mode_commands:
        return False, "command missing"
    if str(service.get("kind") or "").strip() == "ingress" and model is not None and not model.get("ingress_routes"):
        return False, "no ingress routes active"
    if str(service.get("kind") or "").strip() == "orchestration":
        return False, "orchestration services are status-only"
    artifact_id = str(service.get("artifact") or "").strip()
    if artifact_id and model is not None and not service.get("required", True):
        for artifact in model.get("artifacts") or []:
            if str(artifact.get("id", "")).strip() == artifact_id:
                artifact_path = str(artifact.get("path") or "").strip()
                if artifact_path and not Path(artifact_path).exists():
                    return False, f"artifact {artifact_id!r} not available"
                break
    return True, None


def service_dependency_ids(service: dict[str, Any]) -> list[str]:
    raw_dependencies = service.get("depends_on") or []
    if not isinstance(raw_dependencies, list):
        return []

    dependencies: list[str] = []
    seen: set[str] = set()
    for raw_dependency in raw_dependencies:
        dependency_id = str(raw_dependency).strip()
        if not dependency_id or dependency_id in seen:
            continue
        dependencies.append(dependency_id)
        seen.add(dependency_id)
    return dependencies


def service_id_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(service["id"]): service
        for service in model["services"]
        if str(service.get("id", "")).strip()
    }


def service_dependency_graph(model: dict[str, Any]) -> dict[str, list[str]]:
    return {
        service_id: service_dependency_ids(service)
        for service_id, service in service_id_map(model).items()
    }


def reverse_service_dependency_graph(model: dict[str, Any]) -> dict[str, list[str]]:
    reverse_graph: dict[str, list[str]] = {
        service_id: []
        for service_id in service_id_map(model)
    }
    for service_id, dependency_ids in service_dependency_graph(model).items():
        for dependency_id in dependency_ids:
            reverse_graph.setdefault(dependency_id, []).append(service_id)
    return reverse_graph


def order_service_ids(model: dict[str, Any], selected_ids: set[str]) -> list[str]:
    services_by_id = service_id_map(model)
    artifact_ids = {str(a.get("id", "")).strip() for a in model.get("artifacts") or []}
    dependency_graph = service_dependency_graph(model)
    ordered_ids: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(service_id: str) -> None:
        if service_id in visited:
            return
        if service_id in visiting:
            raise RuntimeError(f"Service dependency cycle detected at {service_id}.")
        if service_id not in services_by_id:
            if service_id in artifact_ids:
                return
            raise RuntimeError(f"Service dependency references unknown service {service_id!r}.")

        visiting.add(service_id)
        for dependency_id in dependency_graph.get(service_id, []):
            if dependency_id in selected_ids:
                visit(dependency_id)
        visiting.remove(service_id)
        visited.add(service_id)
        ordered_ids.append(service_id)

    for service in model["services"]:
        service_id = str(service.get("id", "")).strip()
        if service_id and service_id in selected_ids:
            visit(service_id)

    return ordered_ids


def resolve_services_for_start(
    model: dict[str, Any],
    requested_services: list[dict[str, Any]],
    *,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    # `mode` is accepted so WG-005 callers can thread the effective mode
    # through the lifecycle pipeline; the dependency expansion + topo sort is
    # mode-agnostic, but keeping the parameter here documents the data-flow
    # and leaves room for mode-gated expansion (see WG-005 orchestration).
    del mode  # currently unused but reserved for future mode-gated expansion
    requested_ids = [str(service["id"]) for service in requested_services]
    expanded_ids = expand_graph_ids(service_dependency_graph(model), requested_ids)
    ordered_ids = order_service_ids(model, expanded_ids)
    services_by_id = service_id_map(model)
    return [services_by_id[service_id] for service_id in ordered_ids]


def resolve_services_for_stop(
    model: dict[str, Any],
    requested_services: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requested_ids = [str(service["id"]) for service in requested_services]
    expanded_ids = expand_graph_ids(reverse_service_dependency_graph(model), requested_ids)
    ordered_ids = list(reversed(order_service_ids(model, expanded_ids)))
    services_by_id = service_id_map(model)
    return [services_by_id[service_id] for service_id in ordered_ids]


def translated_runtime_env(root_dir: Path, runtime_env: dict[str, str]) -> dict[str, str]:
    translated: dict[str, str] = {}
    for key, value in runtime_env.items():
        if key in {"SKILLBOX_MONOSERVER_HOST_ROOT", "SKILLBOX_CLIENTS_HOST_ROOT"}:
            translated[key] = str(host_path_to_absolute_path(root_dir, value))
            continue
        if key in PATH_LIKE_ENV_KEYS and value:
            translated[key] = str(runtime_path_to_host_path(root_dir, runtime_env, value))
            continue
        translated[key] = value
    translated["ROOT_DIR"] = str(root_dir)
    return translated


def translate_runtime_paths(value: str, runtime_env: dict[str, str], translated_env: dict[str, str]) -> str:
    translated = value
    replacements: list[tuple[str, str]] = []
    for key in PATH_LIKE_ENV_KEYS:
        runtime_path = str(runtime_env.get(key, "")).strip()
        host_path = str(translated_env.get(key, "")).strip()
        if runtime_path and host_path and runtime_path != host_path:
            replacements.append((runtime_path, host_path))

    for runtime_path, host_path in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        translated = translated.replace(runtime_path, host_path)
    return translated


def runtime_item_log_dir(model: dict[str, Any], item: dict[str, Any]) -> Path:
    log_map = runtime_log_map(model)
    log_id = str(item.get("log") or "").strip()
    log_dir: Path
    if log_id and log_id in log_map:
        log_dir = Path(str(log_map[log_id]["host_path"]))
    elif "runtime" in log_map:
        log_dir = Path(str(log_map["runtime"]["host_path"]))
    else:
        log_dir = Path(str(model["root_dir"])) / "logs" / "runtime"
    return log_dir


def service_paths(model: dict[str, Any], service: dict[str, Any]) -> dict[str, Path]:
    log_dir = runtime_item_log_dir(model, service)
    service_slug = str(service["id"])
    return {
        "log_dir": log_dir,
        "log_file": log_dir / f"{service_slug}.log",
        "pid_file": log_dir / f"{service_slug}.pid",
    }


def task_paths(model: dict[str, Any], task: dict[str, Any]) -> dict[str, Path]:
    log_dir = runtime_item_log_dir(model, task)
    task_slug = str(task["id"])
    return {
        "log_dir": log_dir,
        "log_file": log_dir / f"{task_slug}.log",
    }


def read_service_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    raw_value = pid_path.read_text(encoding="utf-8").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        return None


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def remove_pid_file(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except FileNotFoundError:
        return


def live_service_pid(pid_path: Path) -> int | None:
    pid = read_service_pid(pid_path)
    if pid is None:
        return None
    if process_is_running(pid):
        return pid
    remove_pid_file(pid_path)
    return None


def service_manager_state(model: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    manageable, reason = service_supports_lifecycle(service, model)
    paths = service_paths(model, service)
    pid = live_service_pid(paths["pid_file"])
    return {
        "managed": manageable,
        "manager_reason": reason,
        "pid": pid,
        "pid_file": str(paths["pid_file"]),
        "log_file": str(paths["log_file"]),
        "log_present": paths["log_file"].is_file(),
    }


def resolve_runtime_command_cwd(model: dict[str, Any], item: dict[str, Any]) -> Path:
    # Overlay schema uses `repo_id`; legacy items use `repo`.  Accept either so
    # local_runtime_core_cutover services (which only declare `repo_id`) still
    # launch in the correct repo working directory.
    repo_id = str(item.get("repo") or item.get("repo_id") or "").strip()
    repo = runtime_repo_map(model).get(repo_id)
    if repo is not None and repo.get("host_path"):
        return Path(str(repo["host_path"]))

    host_path = str(item.get("host_path") or "").strip()
    if host_path:
        candidate = Path(host_path)
        return candidate if candidate.is_dir() else candidate.parent

    return Path(str(model["root_dir"]))


def translated_runtime_command(model: dict[str, Any], item: dict[str, Any]) -> tuple[str, dict[str, str]]:
    root_dir = Path(str(model["root_dir"]))
    runtime_env = dict(model.get("env") or {})
    translated_env = translated_runtime_env(root_dir, runtime_env)
    command = translate_runtime_paths(str(item["command"]), runtime_env, translated_env)
    env = os.environ.copy()
    env.update(translated_env)
    return command, env


def validate_local_runtime_profiles(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Check that local-* profiles in the active set have declared services."""
    errors: list[dict[str, Any]] = []
    active = set(model.get("active_profiles") or [])
    local_profiles = {p for p in active if p.startswith("local-")}
    if not local_profiles:
        return errors

    # Collect which local profiles have at least one service
    for lp in local_profiles:
        has_service = any(
            lp in (service.get("profiles") or [])
            for service in model.get("services") or []
        )
        if not has_service:
            available = sorted({
                p for service in model.get("services") or []
                for p in service.get("profiles") or []
                if p.startswith("local-")
            })
            errors.append(local_runtime_error(
                "LOCAL_RUNTIME_PROFILE_UNKNOWN",
                f"Profile '{lp}' has no declared local-runtime services",
                recoverable=False,
                available_profiles=available,
            ))
    return errors


LOCAL_RUNTIME_ERROR_CODES = {
    "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
    "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
    "LOCAL_RUNTIME_PROFILE_UNKNOWN",
    "LOCAL_RUNTIME_START_BLOCKED",
    "LOCAL_RUNTIME_SERVICE_DEFERRED",
    "LOCAL_RUNTIME_MODE_UNSUPPORTED",
    "LOCAL_RUNTIME_COVERAGE_GAP",
}


def bridge_id_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(bridge["id"]): bridge
        for bridge in model.get("bridges") or []
        if str(bridge.get("id", "")).strip()
    }


def bridge_expected_outputs(bridge: dict[str, Any]) -> list[Path]:
    output_root = bridge.get("output_root_host_path") or bridge.get("output_root", "")
    if not output_root:
        return []
    root = Path(str(output_root))
    env_tier = str(bridge.get("env_tier", "local"))
    return [
        root / str(target) / f"{env_tier}.env"
        for target in bridge.get("legacy_targets") or []
    ]


def bridge_outputs_state(bridge: dict[str, Any]) -> dict[str, Any]:
    expected = bridge_expected_outputs(bridge)
    if not expected:
        return {"state": "unknown", "outputs": []}
    missing = [str(p) for p in expected if not p.is_file()]
    if missing:
        return {"state": "missing", "outputs": [str(p) for p in expected], "missing": missing}
    return {"state": "ok", "outputs": [str(p) for p in expected]}


def _port_listening_state(port: int, *, host: str = "127.0.0.1", timeout: float = 0.5) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"state": "ok", "host": host, "port": port}
    except OSError:
        return {"state": "down", "host": host, "port": port}


def bridge_freshness(bridge: dict[str, Any], overlay_path: str | None = None) -> dict[str, Any]:
    outputs = bridge_expected_outputs(bridge)
    if not outputs:
        return {"fresh": False, "reason": "no_outputs_declared"}
    missing = [p for p in outputs if not p.is_file()]
    if missing:
        return {"fresh": False, "reason": "outputs_missing", "missing": [str(p) for p in missing]}
    if overlay_path:
        overlay_p = Path(overlay_path)
        if overlay_p.is_file():
            overlay_mtime = overlay_p.stat().st_mtime
            stale = [p for p in outputs if p.stat().st_mtime < overlay_mtime]
            if stale:
                return {"fresh": False, "reason": "stale", "stale": [str(p) for p in stale]}
    return {"fresh": True}


def local_runtime_error(
    error_type: str,
    detail: str,
    *,
    recoverable: bool = True,
    next_action: str = "",
    blocked_services: list[str] | None = None,
    available_profiles: list[str] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "error": {
            "type": error_type,
            "detail": detail,
            # WG-006: emit ``message`` alongside ``detail`` so legacy
            # structured-error consumers that key off ``error.message``
            # (classify_error shape) still see the underlying reason
            # without the caller having to branch on envelope style.
            "message": detail,
            "recoverable": recoverable,
        }
    }
    if next_action:
        err["error"]["next_action"] = next_action
    if blocked_services is not None:
        err["error"]["blocked_services"] = blocked_services
    if available_profiles is not None:
        err["error"]["available_profiles"] = available_profiles
    return err


# ---------------------------------------------------------------------------
# WG-004: focus + bridge/env reconciliation for local-core
# ---------------------------------------------------------------------------
#
# Flow 1 (flows.md:10-34) and Rule 3 + Rule 4 (backend.md:37-65) require that
# `manage.py focus` for a local-* profile:
#   1. Resolve the declared service graph for the profile
#   2. Re-run the declared bridge task only when outputs are missing or stale
#      against the overlay mtime; otherwise verify without re-execution
#   3. Validate each generated env source exists (LOCAL_RUNTIME_ENV_OUTPUT_MISSING)
#   4. Validate each env target_path matches the repo contract — if an
#      env_file declares ``enforce_filename``, the target basename must match
#      it exactly. This is the seam for repos whose env contract diverges
#      from the default ``.env.local`` convention.
#   5. Emit stable error codes at each decision point
#
# These helpers are the shared reconciliation surface used by focus.  The
# `up`/orchestration functions (WG-005) will call the same helpers but from
# the lifecycle path and with their own policy around retries.
#
# Env filename policy: the runtime does not assume a uniform ``.env.local``
# convention. Overlays declare an optional ``enforce_filename`` on each
# env_file; when set, the target basename must match exactly. Repos that read
# ``.env`` instead of ``.env.local`` (or any other variant) opt in through
# that field rather than through a hardcoded allowlist.


def local_runtime_active_profile(model: dict[str, Any]) -> str | None:
    """Return the single active `local-*` profile, if any.

    Focus only ever operates on one local-* profile at a time; this helper
    normalises the lookup and ignores the framing `core` profile.
    """
    for raw_profile in model.get("active_profiles") or []:
        profile = str(raw_profile).strip()
        if profile.startswith("local-"):
            return profile
    return None


def select_local_runtime_bridges(
    model: dict[str, Any],
    profile: str,
) -> list[dict[str, Any]]:
    """Return bridges that declare the given local profile."""
    bridges: list[dict[str, Any]] = []
    for bridge in model.get("bridges") or []:
        declared = [str(p).strip() for p in (bridge.get("profiles") or [])]
        if profile in declared:
            bridges.append(bridge)
    return bridges


def select_local_runtime_tasks_for_bridges(
    model: dict[str, Any],
    bridges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return bootstrap tasks that materialise the given bridges."""
    bridge_ids = {
        str(bridge.get("id", "")).strip()
        for bridge in bridges
        if str(bridge.get("id", "")).strip()
    }
    if not bridge_ids:
        return []
    return [
        task
        for task in model.get("tasks") or []
        if str(task.get("bridge_id", "")).strip() in bridge_ids
    ]


def select_local_runtime_services(
    model: dict[str, Any],
    profile: str,
) -> list[dict[str, Any]]:
    """Return services declared in the given local profile, preserving overlay order."""
    services: list[dict[str, Any]] = []
    for service in model.get("services") or []:
        declared = [str(p).strip() for p in (service.get("profiles") or [])]
        if profile in declared:
            services.append(service)
    return services


def select_local_runtime_env_files(
    model: dict[str, Any],
    profile: str,
) -> list[dict[str, Any]]:
    """Return env_files declared in the given local profile."""
    files: list[dict[str, Any]] = []
    for env_file in model.get("env_files") or []:
        declared = [str(p).strip() for p in (env_file.get("profiles") or [])]
        if profile in declared:
            files.append(env_file)
    return files


def local_runtime_overlay_path(
    model: dict[str, Any],
    client_id: str | None = None,
) -> str | None:
    """Return the host path of the overlay for the active client, if known.

    The loader stores the overlay path on each client entry as
    `_overlay_path` (see workflows.run_focus).
    """
    target_client = (client_id or "").strip()
    for client in model.get("clients") or []:
        cid = str(client.get("id", "")).strip()
        if target_client and cid != target_client:
            continue
        overlay_path = client.get("_overlay_path")
        if overlay_path:
            return str(overlay_path)
    return None


def validate_env_file_target_paths(
    env_files: list[dict[str, Any]],
    model: dict[str, Any],
) -> list[str]:
    """Return a list of violation messages for env target paths.

    Rule 4 (backend.md:56-65): each declared env target_path must match the
    repo's real env contract. The runtime must not assume a uniform
    ``.env.local`` convention; overlays declare ``enforce_filename`` on any
    env_file whose target repo requires a specific filename.

    The validation runs against each env_file's post-normalisation `host_path`
    (the materialised target path) and uses the `repo` id to locate the repo's
    host directory.
    """
    violations: list[str] = []
    repo_map = runtime_repo_map(model)

    for env_file in env_files:
        env_id = str(env_file.get("id", "")).strip() or "(missing id)"
        target_raw = env_file.get("host_path") or env_file.get("path")
        if not target_raw:
            violations.append(f"{env_id}: missing target path")
            continue

        target_path = Path(str(target_raw))
        repo_id = str(env_file.get("repo") or "").strip()

        # Overlay-declared exact filename enforcement.
        required_name = str(env_file.get("enforce_filename") or "").strip()
        if required_name:
            if target_path.name != required_name:
                violations.append(
                    f"{env_id}: env target filename must be "
                    f"{required_name!r}, got {target_path.name!r} "
                    f"(target_path={target_path})"
                )
                continue

        # All env targets must look like env files (either .env or .env.<tier>)
        name = target_path.name
        if not (name == ".env" or name.startswith(".env.")):
            violations.append(
                f"{env_id}: target path {target_path} is not an env-style file"
            )
            continue

        # The env target must live inside the declared repo host directory.
        if repo_id and repo_id in repo_map:
            repo_host = Path(str(repo_map[repo_id].get("host_path") or ""))
            if repo_host:
                try:
                    target_path.resolve().relative_to(repo_host.resolve())
                except (ValueError, OSError):
                    violations.append(
                        f"{env_id}: target_path {target_path} is not inside "
                        f"repo {repo_id} at {repo_host}"
                    )

    return violations


def bridges_need_rerun(
    bridges: list[dict[str, Any]],
    overlay_path: str | None,
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Return (needs_rerun, freshness_by_id) for the given bridges.

    A bridge needs rerun when its freshness reports anything other than
    `fresh=True` (outputs missing, stale vs overlay mtime, or no outputs).
    """
    freshness_by_id: dict[str, dict[str, Any]] = {}
    needs_rerun = False
    for bridge in bridges:
        bid = str(bridge.get("id", "")).strip() or "(missing id)"
        freshness = bridge_freshness(bridge, overlay_path)
        freshness_by_id[bid] = freshness
        if not freshness.get("fresh"):
            needs_rerun = True
    return needs_rerun, freshness_by_id


def run_local_runtime_bridge_tasks(
    model: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Execute the given bridge-backed bootstrap tasks in dependency order.

    Wraps `run_tasks` but translates any failure into a
    RuntimeError tagged so callers can map it to
    LOCAL_RUNTIME_ENV_BRIDGE_FAILED without guessing from the error string.
    """
    if not tasks:
        return []
    ordered = resolve_tasks_for_run(model, tasks)
    return run_tasks(model, ordered, dry_run=dry_run)


def reconcile_local_runtime_env(
    model: dict[str, Any],
    profile: str,
    *,
    overlay_path: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Implement Flow 1 bridge/env reconciliation for a local-* profile.

    Returns a result dict with shape:
      {
        "status": "ready" | "blocked",
        "profile": <profile>,
        "bridges": [ {id, status, freshness, ...}, ... ],
        "env_files": [ {id, path, present}, ... ],
        "actions": [ ... ],  # action log lines
        "error": {...}       # only when status == "blocked"
      }

    Steps follow backend.md:136-146:
      1. Resolve bridges + tasks + env files for the profile
      2. Re-run bridge tasks when stale / outputs missing
      3. Validate each generated env source exists
      4. Validate target_path matches repo contract
      5. Mark blocked on any failure with a stable LOCAL_RUNTIME_* code
    """
    actions: list[str] = []
    result: dict[str, Any] = {
        "status": "ready",
        "profile": profile,
        "bridges": [],
        "env_files": [],
        "actions": actions,
    }

    # (0) Unknown / empty profile -> LOCAL_RUNTIME_PROFILE_UNKNOWN
    if not profile or not profile.strip():
        result["status"] = "blocked"
        result.update(local_runtime_error(
            "LOCAL_RUNTIME_PROFILE_UNKNOWN",
            "No local runtime profile selected.",
            recoverable=False,
            available_profiles=sorted({
                str(p).strip()
                for service in model.get("services") or []
                for p in service.get("profiles") or []
                if str(p).strip().startswith("local-")
            }),
        ))
        return result

    services = select_local_runtime_services(model, profile)
    if not services:
        result["status"] = "blocked"
        result.update(local_runtime_error(
            "LOCAL_RUNTIME_PROFILE_UNKNOWN",
            f"Profile {profile!r} has no declared local-runtime services.",
            recoverable=False,
            available_profiles=sorted({
                str(p).strip()
                for service in model.get("services") or []
                for p in service.get("profiles") or []
                if str(p).strip().startswith("local-")
            }),
        ))
        return result

    # (1) Resolve bridges + bridge-backed tasks + env files
    bridges = select_local_runtime_bridges(model, profile)
    bridge_tasks = select_local_runtime_tasks_for_bridges(model, bridges)
    env_files = select_local_runtime_env_files(model, profile)

    # (2) Bridge freshness decision -- Flow 1 decision point
    needs_rerun, freshness_by_id = bridges_need_rerun(bridges, overlay_path)
    if needs_rerun and bridge_tasks:
        actions.append(
            f"bridge-rerun: stale or missing outputs for "
            f"{', '.join(sorted(freshness_by_id))}"
        )
        try:
            run_local_runtime_bridge_tasks(model, bridge_tasks, dry_run=dry_run)
        except RuntimeError as exc:
            result["status"] = "blocked"
            result.update(local_runtime_error(
                "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
                str(exc),
                recoverable=True,
                next_action="Inspect sync.sh bridge logs and rerun focus.",
            ))
            # Still surface bridge freshness for observability
            result["bridges"] = [
                {
                    "id": str(bridge.get("id", "")),
                    "status": "failed",
                    "freshness": freshness_by_id.get(str(bridge.get("id", ""))),
                }
                for bridge in bridges
            ]
            return result
    elif not bridges:
        actions.append("bridge-skip: no bridges declared for profile")
    else:
        actions.append("bridge-verify: all bridge outputs are fresh")

    # Re-probe bridge state after any rerun.
    bridge_report: list[dict[str, Any]] = []
    any_bridge_missing = False
    missing_outputs: list[str] = []
    for bridge in bridges:
        bid = str(bridge.get("id", "")).strip()
        state = bridge_outputs_state(bridge)
        ready = state["state"] == "ok"
        if not ready:
            any_bridge_missing = True
            missing_outputs.extend(state.get("missing", []))
        bridge_report.append({
            "id": bid,
            "status": "ready" if ready else state["state"],
            "freshness": freshness_by_id.get(bid),
            "outputs": state.get("outputs", []),
            "missing": state.get("missing", []),
        })
    result["bridges"] = bridge_report

    if any_bridge_missing:
        result["status"] = "blocked"
        result.update(local_runtime_error(
            "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            "Bridge outputs missing after reconciliation: "
            + ", ".join(missing_outputs),
            recoverable=True,
            next_action="Inspect sync.sh bridge and rerun focus.",
        ))
        return result

    # (3) Validate each generated env source exists
    missing_sources: list[str] = []
    env_file_report: list[dict[str, Any]] = []
    for env_file in env_files:
        state = env_file_state(env_file)
        source_present = True
        source_host_path = state.get("source_host_path") or ""
        if source_host_path:
            source_present = Path(source_host_path).is_file()
        env_file_report.append({
            "id": env_file.get("id"),
            "repo": env_file.get("repo"),
            "target_path": state.get("host_path"),
            "source_path": source_host_path,
            "source_present": source_present,
            "target_present": state.get("present"),
            "state": state.get("state"),
        })
        if env_file.get("required") and source_host_path and not source_present:
            missing_sources.append(source_host_path)
    result["env_files"] = env_file_report

    if missing_sources:
        result["status"] = "blocked"
        result.update(local_runtime_error(
            "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            "Required env sources missing: " + ", ".join(missing_sources),
            recoverable=True,
            next_action="Inspect sync.sh bridge outputs and rerun focus.",
        ))
        return result

    # (4) Validate each target_path matches the repo contract (Rule 4)
    target_violations = validate_env_file_target_paths(env_files, model)
    if target_violations:
        result["status"] = "blocked"
        result.update(local_runtime_error(
            "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            "Env target path contract violated: " + "; ".join(target_violations),
            recoverable=False,
            next_action="Update overlay env_files target_path to match repo contract.",
        ))
        return result

    # (5) All prerequisites satisfied
    return result


def local_runtime_focus_payload(
    model: dict[str, Any],
    reconcile_result: dict[str, Any],
    *,
    client_id: str,
) -> dict[str, Any]:
    """Build the US-1 focus response payload (shared.md:397-416).

    Shape:
      {
        "client_id": <cid>,
        "active_profiles": [...],
        "local_runtime": {
          "profile": <profile>,
          "default_mode": "reuse",
          "env_bridge": {"id": ..., "status": ...},
          "services": [{"id": ..., "state": "stopped"}, ...]
        },
        "next_actions": [...]
      }
    """
    profile = str(reconcile_result.get("profile") or "")
    active_profiles = sorted(model.get("active_profiles") or [])

    bridge_entries = reconcile_result.get("bridges") or []
    if len(bridge_entries) == 1:
        env_bridge: Any = {
            "id": bridge_entries[0]["id"],
            "status": bridge_entries[0]["status"],
        }
    elif bridge_entries:
        env_bridge = [
            {"id": entry["id"], "status": entry["status"]}
            for entry in bridge_entries
        ]
    else:
        env_bridge = {"id": None, "status": "absent"}

    service_entries: list[dict[str, Any]] = []
    for service in select_local_runtime_services(model, profile):
        probe = probe_service(model, service)
        service_entries.append({
            "id": service["id"],
            "state": probe.get("state", "stopped"),
        })

    next_actions = [
        f"manage.py up --client {client_id} --profile {profile} --mode reuse",
        f"manage.py status --client {client_id} --profile {profile}",
    ]

    return {
        "client_id": client_id,
        "active_profiles": active_profiles,
        "local_runtime": {
            "profile": profile,
            "default_mode": "reuse",
            "env_bridge": env_bridge,
            "services": service_entries,
        },
        "next_actions": next_actions,
    }


def task_success_state(task: dict[str, Any], model: dict[str, Any] | None = None) -> dict[str, Any]:
    success = task.get("success") or {}
    success_type = success.get("type")
    if success_type == "all_outputs_exist" and model:
        bridge_id = str(success.get("target") or task.get("bridge_id") or "").strip()
        if not bridge_id:
            return {"state": "unknown"}
        bridges = bridge_id_map(model)
        bridge = bridges.get(bridge_id)
        if bridge:
            state = bridge_outputs_state(bridge)
            if state["state"] == "ok":
                return {"state": "ok", "target": f"bridge:{bridge_id}"}
            return {"state": "down", "target": f"bridge:{bridge_id}", "missing": state.get("missing", [])}
        return {"state": "unknown", "target": f"bridge:{bridge_id} (not found)"}
    if success_type == "path_exists":
        path = Path(str(success["host_path"]))
        return {"state": "ok" if path.exists() else "down", "target": str(path)}
    if success_type == "port_listening":
        try:
            port = int(success["port"])
        except (KeyError, TypeError, ValueError):
            return {"state": "unknown"}
        host = str(success.get("host") or "127.0.0.1")
        result = _port_listening_state(port, host=host)
        return result | {"target": f"{host}:{port}"}

    # Backward-compatible bridge-backed task: check all bridge outputs exist.
    bridge_id = str(task.get("bridge_id", "")).strip()
    if bridge_id and model:
        bridges = bridge_id_map(model)
        bridge = bridges.get(bridge_id)
        if bridge:
            state = bridge_outputs_state(bridge)
            if state["state"] == "ok":
                return {"state": "ok", "target": f"bridge:{bridge_id}"}
            return {"state": "down", "target": f"bridge:{bridge_id}", "missing": state.get("missing", [])}
        return {"state": "unknown", "target": f"bridge:{bridge_id} (not found)"}
    return {"state": "unknown"}


def probe_task(model: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    success_state = task_success_state(task, model)
    tasks_by_id = task_id_map(model)
    dependency_states = {
        dependency_id: task_success_state(tasks_by_id[dependency_id], model).get("state", "unknown")
        for dependency_id in task_dependency_ids(task)
        if dependency_id in tasks_by_id
    }

    if success_state.get("state") == "ok":
        state = "ready"
    elif any(dependency_state != "ok" for dependency_state in dependency_states.values()):
        state = "blocked"
    else:
        state = "pending"

    result = {
        "state": state,
        "depends_on": task_dependency_ids(task),
        "dependency_states": dependency_states,
    }
    if success_state.get("target"):
        result["target"] = success_state["target"]
    return result


def service_healthcheck_state(service: dict[str, Any]) -> dict[str, Any]:
    healthcheck = service.get("healthcheck") or {}
    healthcheck_type = healthcheck.get("type")
    if not healthcheck_type:
        return {"state": "declared"}

    if healthcheck_type == "path_exists":
        path = Path(str(healthcheck["host_path"]))
        return {"state": "ok" if path.exists() else "down", "target": str(path)}

    if healthcheck_type == "http":
        url = str(healthcheck["url"])
        timeout = float(healthcheck.get("timeout_seconds", 0.5))
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return {"state": "ok", "status_code": response.getcode(), "url": url}
        except (urllib.error.URLError, TimeoutError, ValueError):
            return {"state": "down", "url": url}

    if healthcheck_type == "port":
        try:
            port = int(healthcheck["port"])
        except (KeyError, TypeError, ValueError):
            return {"state": "unknown"}
        host = str(healthcheck.get("host") or "127.0.0.1")
        return _port_listening_state(port, host=host)

    if healthcheck_type == "process_running":
        pattern = str(healthcheck["pattern"]).strip()
        if not pattern:
            return {"state": "down", "pattern": pattern}

        result = run_command(["ps", "-axo", "pid=,command="])
        if result.returncode != 0:
            return {"state": "unknown", "pattern": pattern}

        matches: list[tuple[int, str]] = []
        for raw_line in result.stdout.splitlines():
            parts = raw_line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            raw_pid, command = parts
            try:
                pid = int(raw_pid)
            except ValueError:
                continue
            if pattern in command:
                matches.append((pid, command))

        if matches:
            matched_pid, matched_command = matches[0]
            return {
                "state": "ok",
                "pattern": pattern,
                "matched_pid": matched_pid,
                "match_count": len(matches),
                "matched_command": matched_command,
            }
        return {"state": "down", "pattern": pattern}

    return {"state": "unknown"}


def wait_for_service_health(
    service: dict[str, Any],
    process: subprocess.Popen[str],
    wait_seconds: float,
) -> dict[str, Any]:
    healthcheck = service.get("healthcheck") or {}
    if not healthcheck.get("type"):
        if process.poll() is not None:
            return {"state": "failed", "exit_code": process.returncode}
        return {"state": "started"}

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() <= deadline:
        probe = service_healthcheck_state(service)
        if probe.get("state") == "ok":
            reused_existing = process.poll() is not None
            state = {"state": "ok"} | probe
            if reused_existing:
                state["reused_existing"] = True
                state["exit_code"] = process.returncode
            return state
        if process.poll() is not None:
            return {"state": "failed", "exit_code": process.returncode}
        time.sleep(0.25)

    return {"state": "timeout"} | service_healthcheck_state(service)


def tail_lines(path: Path, line_count: int) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if line_count <= 0:
        return lines
    return lines[-line_count:]


def stop_process(pid: int, wait_seconds: float) -> tuple[str, int | None]:
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return "not-running", None

    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return "not-running", None

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() <= deadline:
        if not process_is_running(pid):
            return "stopped", signal.SIGTERM
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return "stopped", signal.SIGTERM

    deadline = time.monotonic() + 1.0
    while time.monotonic() <= deadline:
        if not process_is_running(pid):
            return "killed", signal.SIGKILL
        time.sleep(0.1)

    return "stuck", None


def select_tasks(model: dict[str, Any], task_ids: list[str] | None) -> list[dict[str, Any]]:
    requested_ids = [task_id.strip() for task_id in task_ids or [] if task_id.strip()]
    available = {
        str(task["id"]): task
        for task in model["tasks"]
        if str(task.get("id", "")).strip()
    }
    unknown = sorted(task_id for task_id in requested_ids if task_id not in available)
    if unknown:
        raise RuntimeError(
            "Unknown task id(s): "
            + ", ".join(unknown)
            + ". Available tasks: "
            + (", ".join(sorted(available)) or "(none)")
        )
    if not requested_ids:
        return list(model["tasks"])

    requested = set(requested_ids)
    return [task for task in model["tasks"] if task["id"] in requested]


def resolve_tasks_for_run(
    model: dict[str, Any],
    requested_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requested_ids = [str(task["id"]) for task in requested_tasks]
    expanded_ids = expand_graph_ids(task_dependency_graph(model), requested_ids)
    ordered_ids = order_task_ids(model, expanded_ids)
    tasks_by_id = task_id_map(model)
    return [tasks_by_id[task_id] for task_id in ordered_ids]


def resolve_tasks_for_services(
    model: dict[str, Any],
    services: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    root_task_ids: list[str] = []
    for service in services:
        root_task_ids.extend(service_bootstrap_task_ids(service))
    if not root_task_ids:
        return []
    expanded_ids = expand_graph_ids(task_dependency_graph(model), root_task_ids)
    ordered_ids = order_task_ids(model, expanded_ids)
    tasks_by_id = task_id_map(model)
    return [tasks_by_id[task_id] for task_id in ordered_ids]


def select_services(model: dict[str, Any], service_ids: list[str] | None) -> list[dict[str, Any]]:
    requested_ids = [service_id.strip() for service_id in service_ids or [] if service_id.strip()]
    available = {
        str(service["id"]): service
        for service in model["services"]
        if str(service.get("id", "")).strip()
    }
    unknown = sorted(service_id for service_id in requested_ids if service_id not in available)
    if unknown:
        raise RuntimeError(
            "Unknown service id(s): "
            + ", ".join(unknown)
            + ". Available services: "
            + (", ".join(sorted(available)) or "(none)")
        )
    if not requested_ids:
        return list(model["services"])

    requested = set(requested_ids)
    return [service for service in model["services"] if service["id"] in requested]


def select_env_files_for_services(model: dict[str, Any], services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not services:
        return list(model["env_files"])

    repo_ids = {
        str(service.get("repo") or "").strip()
        for service in services
        if str(service.get("repo") or "").strip()
    }
    return [
        env_file
        for env_file in model["env_files"]
        if not str(env_file.get("repo") or "").strip() or str(env_file.get("repo") or "").strip() in repo_ids
    ]


def select_env_files_for_tasks(model: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not tasks:
        return []

    repo_ids = {
        str(task.get("repo") or "").strip()
        for task in tasks
        if str(task.get("repo") or "").strip()
    }
    return [
        env_file
        for env_file in model["env_files"]
        if not str(env_file.get("repo") or "").strip() or str(env_file.get("repo") or "").strip() in repo_ids
    ]


def ensure_required_env_files_ready(env_files: list[dict[str, Any]]) -> None:
    unresolved: list[str] = []
    for env_file in env_files:
        state = env_file_state(env_file)
        if not env_file.get("required") or state["state"] == "ok":
            continue
        detail = state["state"]
        if state["state"] == "source-missing" and state["source_path"]:
            detail = f"{detail}: {state['source_path']}"
        unresolved.append(f"{env_file['id']} ({detail})")

    if unresolved:
        raise RuntimeError(f"Required env files are not ready: {', '.join(unresolved)}")


# ---------------------------------------------------------------------------
# WG-005: mode-aware up orchestration helpers for local-core
# ---------------------------------------------------------------------------
#
# These helpers live alongside start_services / run_tasks and are the
# lifecycle-side companion to the WG-004 focus reconciliation surface.  They
# do NOT re-run bridges or env reconciliation themselves; the caller
# (workflows.run_up) is responsible for invoking reconcile_local_runtime_env
# first and bailing out on any blocked result.  These helpers only:
#
#   * resolve the per-mode command for a service (flows.md Flow 2)
#   * validate that EVERY requested service supports the effective mode
#     before any mutation (backend.md Rule 2, lines 25-35)
#   * build the deterministic topological launch plan with bootstrap tasks
#     interleaved in declared order (shared.md:148-158)
#
# The WG-004 helpers (reconcile_local_runtime_env, local_runtime_focus_payload,
# select_local_runtime_*, validate_env_file_target_paths, bridges_need_rerun,
# local_runtime_overlay_path) are intentionally left untouched above.


def service_has_mode_commands(service: dict[str, Any]) -> bool:
    raw = service.get("commands")
    return isinstance(raw, dict) and any(str(v).strip() for v in raw.values())


def resolve_service_mode_command(
    service: dict[str, Any],
    mode: str | None,
) -> str | None:
    """Return the command string for the effective mode, or None.

    Local-runtime services declare a ``commands`` mapping keyed by mode
    (``reuse`` / ``prod`` / ``fresh``).  When the caller threads a mode we
    return that specific command; otherwise we fall back to the reuse command
    so the existing cli.py up path keeps working for services that only
    declare mode commands.  Returns ``None`` when the service only has a
    top-level ``command`` (nothing to resolve).
    """
    raw = service.get("commands")
    if not isinstance(raw, dict) or not raw:
        return None

    wanted = (mode or "").strip() or "reuse"
    command = raw.get(wanted)
    if command is None and wanted != "reuse":
        return None
    if command is None:
        # fall back: pick the first non-empty declared command
        for value in raw.values():
            text = str(value or "").strip()
            if text:
                return text
        return None
    text = str(command or "").strip()
    return text or None


def service_supports_mode(service: dict[str, Any], mode: str) -> bool:
    """Return True if the service explicitly declares support for ``mode``.

    A service supports a mode when its ``commands`` mapping has a non-empty
    entry for the mode (per backend.md Rule 2).  Legacy services that only
    declare a top-level ``command`` are considered mode-agnostic and thus
    support every mode.
    """
    if not service_has_mode_commands(service):
        # legacy top-level command: mode is orthogonal, accept any mode
        return bool(str(service.get("command") or "").strip())
    command = resolve_service_mode_command(service, mode)
    return bool(command)


def validate_services_support_mode(
    services: list[dict[str, Any]],
    mode: str,
) -> list[str]:
    """Return the ids of services that do NOT support the effective mode.

    Used pre-mutation by run_up (backend.md:33-35): if any requested service
    lacks a command for the effective mode the entire request is rejected
    with LOCAL_RUNTIME_MODE_UNSUPPORTED and no state is mutated.
    """
    unsupported: list[str] = []
    for service in services:
        if not service_supports_mode(service, mode):
            unsupported.append(str(service.get("id", "")).strip() or "(missing id)")
    return unsupported


def run_tasks(
    model: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    dry_run: bool,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    # Bootstrap tasks are currently mode-agnostic (bridge and DB bootstrap use
    # a single command); the parameter is threaded here so WG-005 lifecycle
    # callers can pass the resolved mode without a second signature.
    del mode  # reserved for future mode-gated bootstrap commands
    results: list[dict[str, Any]] = []
    for task in tasks:
        paths = task_paths(model, task)
        result = {
            "id": task["id"],
            "kind": task.get("kind", "task"),
            "log_file": str(paths["log_file"]),
            "depends_on": task_dependency_ids(task),
        }
        task_state = probe_task(model, task)
        if task_state["state"] == "ready":
            results.append(result | {"result": "ready", "target": task_state.get("target")})
            continue
        if task_state["state"] == "blocked":
            blocked_on = [
                dependency_id
                for dependency_id, dependency_state in task_state.get("dependency_states", {}).items()
                if dependency_state != "ok"
            ]
            raise RuntimeError(
                f"Task {task['id']} is blocked by incomplete dependencies: {', '.join(blocked_on)}"
            )

        command, env = translated_runtime_command(model, task)
        cwd = resolve_runtime_command_cwd(model, task)
        result["command"] = command
        result["cwd"] = str(cwd)

        ensure_directory(paths["log_dir"], dry_run)
        if dry_run:
            results.append(result | {"result": "dry-run"})
            continue

        log_runtime_event("task.started", task["id"])
        with paths["log_file"].open("a", encoding="utf-8") as log_handle:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=True,
                text=True,
                check=False,
            )

        if completed.returncode != 0:
            tail = tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)
            log_runtime_event("task.failed", task["id"], {"exit_code": completed.returncode})
            raise RuntimeError(
                f"Task {task['id']} failed with exit code {completed.returncode}."
                + (f" Recent logs: {' | '.join(tail)}" if tail else "")
            )

        post_state = probe_task(model, task)
        if post_state["state"] != "ready":
            tail = tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)
            log_runtime_event("task.failed", task["id"], {"reason": "success_check_unsatisfied"})
            raise RuntimeError(
                f"Task {task['id']} completed but did not satisfy its success check."
                + (f" Success target: {post_state['target']}." if post_state.get("target") else "")
                + (f" Recent logs: {' | '.join(tail)}" if tail else "")
            )

        results.append(result | {"result": "completed", "target": post_state.get("target")})
        log_runtime_event("task.completed", task["id"])
    return results


def start_services(
    model: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    dry_run: bool,
    wait_seconds: float,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    effective_mode = (mode or "").strip() or None
    for service in services:
        manageable, reason = service_supports_lifecycle(service, model)
        paths = service_paths(model, service)
        result = {
            "id": service["id"],
            "kind": service.get("kind", "service"),
            "log_file": str(paths["log_file"]),
            "pid_file": str(paths["pid_file"]),
        }

        if not manageable:
            results.append(result | {"result": "skipped", "reason": reason})
            continue

        pid = live_service_pid(paths["pid_file"])
        if pid is not None:
            results.append(result | {"result": "already-running", "pid": pid})
            continue

        # Resolve the per-mode command for local-runtime services.  Services
        # that only declare a `commands` mapping (no top-level `command`) are
        # the local_runtime_core_cutover shape; pick the effective mode (or
        # fall back to the reuse command when the caller did not thread one).
        launch_service = service
        mode_command = resolve_service_mode_command(service, effective_mode)
        if mode_command is not None:
            launch_service = dict(service)
            launch_service["command"] = mode_command
            if effective_mode is not None:
                result["mode"] = effective_mode

        command, env = translated_runtime_command(model, launch_service)
        cwd = resolve_runtime_command_cwd(model, launch_service)
        result["command"] = command
        result["cwd"] = str(cwd)

        ensure_directory(paths["log_dir"], dry_run)
        if dry_run:
            results.append(result | {"result": "dry-run"})
            continue

        with paths["log_file"].open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=True,
                start_new_session=True,
                text=True,
            )

        paths["pid_file"].write_text(f"{process.pid}\n", encoding="utf-8")
        health_state = wait_for_service_health(service, process, wait_seconds)
        if health_state.get("state") in {"failed", "timeout"}:
            stop_process(process.pid, DEFAULT_SERVICE_STOP_WAIT_SECONDS)
            remove_pid_file(paths["pid_file"])
            tail = tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)
            detail = result | {"result": "failed", "tail": tail}
            if "exit_code" in health_state:
                detail["exit_code"] = health_state["exit_code"]
            if "url" in health_state:
                detail["url"] = health_state["url"]
            if "target" in health_state:
                detail["target"] = health_state["target"]
            if "pattern" in health_state:
                detail["pattern"] = health_state["pattern"]
            log_runtime_event("service.start_failed", service["id"], {"state": health_state.get("state")})
            raise RuntimeError(
                f"Service {service['id']} failed to become healthy."
                + (f" Exit code: {health_state['exit_code']}." if "exit_code" in health_state else "")
                + (f" Health target: {health_state['url']}." if "url" in health_state else "")
                + (f" Health target: {health_state['target']}." if "target" in health_state else "")
                + (f" Health pattern: {health_state['pattern']}." if "pattern" in health_state else "")
                + (f" Recent logs: {' | '.join(tail)}" if tail else "")
            )

        if health_state.get("reused_existing"):
            remove_pid_file(paths["pid_file"])
            reused_result = result | {"result": "already-running"}
            if "url" in health_state:
                reused_result["url"] = health_state["url"]
            if "target" in health_state:
                reused_result["target"] = health_state["target"]
            results.append(reused_result)
            log_runtime_event("service.reused", service["id"])
            continue

        results.append(result | {"result": "started", "pid": process.pid})
        log_runtime_event("service.started", service["id"], {"pid": process.pid})
    return results


def stop_services(
    model: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    dry_run: bool,
    wait_seconds: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for service in services:
        manageable, reason = service_supports_lifecycle(service, model)
        paths = service_paths(model, service)
        pid = live_service_pid(paths["pid_file"])
        result = {
            "id": service["id"],
            "kind": service.get("kind", "service"),
            "log_file": str(paths["log_file"]),
            "pid_file": str(paths["pid_file"]),
        }

        can_stop_idle_ingress = (
            pid is not None
            and str(service.get("kind") or "").strip() == "ingress"
            and reason == "no ingress routes active"
        )

        if not manageable and not can_stop_idle_ingress:
            results.append(result | {"result": "skipped", "reason": reason})
            continue

        if pid is None:
            external_state = service_healthcheck_state(service)
            if external_state.get("state") == "ok":
                results.append(result | {"result": "external"})
            else:
                results.append(result | {"result": "not-running"})
            continue

        if dry_run:
            results.append(result | {"result": "dry-run", "pid": pid})
            continue

        stop_result, signal_used = stop_process(pid, wait_seconds)
        remove_pid_file(paths["pid_file"])
        results.append(
            result
            | {
                "result": stop_result,
                "pid": pid,
                "signal": signal_used,
            }
        )
        log_runtime_event("service.stopped", service["id"], {"signal": signal_used})
    return results


def collect_service_logs(
    model: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    line_count: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for service in services:
        paths = service_paths(model, service)
        log_file = paths["log_file"]
        results.append(
            {
                "id": service["id"],
                "kind": service.get("kind", "service"),
                "log_file": str(log_file),
                "present": log_file.is_file(),
                "lines": tail_lines(log_file, line_count),
            }
        )
    return results


def git_repo_state(path: Path) -> dict[str, Any]:
    top_level = run_command(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if top_level.returncode != 0:
        return {"git": False}

    if Path(top_level.stdout.strip()).resolve() != path.resolve():
        return {"git": False}

    result = run_command(["git", "status", "--short", "--branch"], cwd=path)
    if result.returncode != 0:
        return {"git": False}

    branch = ""
    dirty = 0
    untracked = 0
    for index, line in enumerate(result.stdout.splitlines()):
        if index == 0 and line.startswith("## "):
            branch = line[3:].strip()
            continue
        if not line.strip():
            continue
        if line.startswith("?? "):
            untracked += 1
        else:
            dirty += 1

    return {"git": True, "branch": branch, "dirty": dirty, "untracked": untracked}


def log_directory_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"present": False, "files": 0, "bytes": 0}

    file_count = 0
    total_bytes = 0
    for child in path.rglob("*"):
        if child.is_file():
            file_count += 1
            total_bytes += child.stat().st_size
    return {"present": True, "files": file_count, "bytes": total_bytes}


def probe_service(model: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    manager_state = service_manager_state(model, service)
    health_state = service_healthcheck_state(service)
    pid = manager_state.get("pid")

    if (
        pid is None
        and str(service.get("kind") or "").strip() == "ingress"
        and manager_state.get("managed") is False
        and manager_state.get("manager_reason") == "no ingress routes active"
    ):
        state = "idle"
    elif pid is not None:
        if health_state.get("state") == "ok":
            state = "running"
        elif health_state.get("state") == "declared":
            state = "running"
        else:
            state = "starting"
    else:
        state = health_state.get("state", "declared")

    return {
        "state": state,
        "managed": manager_state["managed"],
        "manager_reason": manager_state["manager_reason"],
        "pid": pid,
        "pid_file": manager_state["pid_file"],
        "log_file": manager_state["log_file"],
        "log_present": manager_state["log_present"],
    } | {
        key: value
        for key, value in health_state.items()
        if key != "state"
    }


FOCUS_STATE_REL = Path("workspace") / ".focus.json"
FOCUS_ERROR_PATTERNS = re.compile(
    r"(?:error|exception|traceback|fatal|panic|fail(?:ed|ure)?)",
    re.IGNORECASE,
)
MCP_CONFIG_REL = Path(".mcp.json")
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SMOKE_TIMEOUT_SECONDS = 5.0


def collect_live_state(
    model: dict[str, Any],
    root_dir: Path = DEFAULT_ROOT_DIR,
) -> dict[str, Any]:
    """Snapshot volatile runtime state: git branches, service health, check results, recent errors."""
    repo_states: list[dict[str, Any]] = []
    for repo in model.get("repos") or []:
        path = Path(str(repo["host_path"]))
        item: dict[str, Any] = {
            "id": repo["id"],
            "path": str(repo["path"]),
            "present": path.exists(),
        }
        if path.exists() and path.is_dir():
            git_state = git_repo_state(path)
            item.update(git_state)
            if git_state.get("git"):
                log_result = run_command(
                    ["git", "log", "--oneline", "-1"], cwd=path,
                )
                if log_result.returncode == 0:
                    item["last_commit"] = log_result.stdout.strip()
        repo_states.append(item)

    service_states: list[dict[str, Any]] = []
    for service in model.get("services") or []:
        probe = probe_service(model, service)
        service_states.append({
            "id": service["id"],
            "kind": service.get("kind", "service"),
            "state": probe.get("state", "declared"),
            "pid": probe.get("pid"),
            "healthy": probe.get("state") == "running",
        })

    check_states: list[dict[str, Any]] = []
    for check in model.get("checks") or []:
        item = {"id": check["id"], "type": check["type"], "ok": False}
        if check["type"] == "path_exists":
            item["ok"] = Path(str(check["host_path"])).exists()
        check_states.append(item)

    log_states: list[dict[str, Any]] = []
    for log_item in model.get("logs") or []:
        path = Path(str(log_item["host_path"]))
        item = {
            "id": log_item["id"],
            "path": str(log_item["path"]),
            "present": path.exists(),
            "recent_errors": [],
        }
        if path.exists():
            # Scan the most recently modified log file for error-like lines.
            log_files = sorted(
                (f for f in path.rglob("*") if f.is_file()),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            if log_files:
                lines = tail_lines(log_files[0], 100)
                errors = [
                    line for line in lines if FOCUS_ERROR_PATTERNS.search(line)
                ]
                item["recent_errors"] = errors[-5:]  # Keep at most 5
                item["scanned_file"] = str(log_files[0].name)
        log_states.append(item)

    session_states: list[dict[str, Any]] = []
    active_clients = [str(client_id) for client_id in model.get("active_clients") or [] if str(client_id).strip()]
    for client_id in active_clients:
        for session in list_client_sessions(root_dir, client_id, limit=5):
            session_states.append(
                {
                    "client_id": client_id,
                    "session_id": session["session_id"],
                    "status": session.get("status"),
                    "label": session.get("label") or "",
                    "goal": session.get("goal") or "",
                    "updated_at": session.get("updated_at"),
                    "last_event_type": session.get("last_event_type") or "",
                    "last_message": session.get("last_message") or "",
                }
            )

    session_states.sort(
        key=lambda item: float(item.get("updated_at") or 0),
        reverse=True,
    )

    bridge_states: list[dict[str, Any]] = []
    for bridge in model.get("bridges") or []:
        state = bridge_outputs_state(bridge)
        bridge_states.append({
            "id": bridge["id"],
            "env_tier": bridge.get("env_tier", "local"),
            "targets": bridge.get("legacy_targets", []),
            "state": state["state"],
            "missing": state.get("missing", []),
        })

    return {
        "collected_at": time.time(),
        "repos": repo_states,
        "services": service_states,
        "checks": check_states,
        "logs": log_states,
        "sessions": session_states,
        "bridges": bridge_states,
    }


def _collect_skill_repo_status(skillset: dict[str, Any]) -> dict[str, Any]:
    """Build status payload for a skill-repo-set skillset."""
    config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
    lock_path = Path(str(skillset.get("lock_path_host_path", "")))

    lock_present = lock_path.is_file()
    lock_payload: dict[str, Any] | None = None
    lock_error: str | None = None
    if lock_present:
        try:
            lock_payload = load_json_file(lock_path)
        except RuntimeError as exc:
            lock_error = str(exc)

    lock_skills_by_name: dict[str, dict[str, Any]] = {}
    if lock_payload:
        for entry in lock_payload.get("skills") or []:
            name = str(entry.get("name", ""))
            if name:
                lock_skills_by_name[name] = entry

    skills: list[dict[str, Any]] = []
    for name, lock_record in sorted(lock_skills_by_name.items()):
        skill_entry: dict[str, Any] = {
            "name": name,
            "repo": lock_record.get("repo"),
            "source_path": lock_record.get("source_path"),
            "declared_ref": lock_record.get("declared_ref"),
            "resolved_commit": lock_record.get("resolved_commit"),
            "targets": [],
        }

        for target in skillset.get("install_targets") or []:
            install_dir = Path(str(target["host_path"])) / name
            installed_sha = directory_tree_sha256(install_dir) if install_dir.is_dir() else None
            lock_sha = lock_record.get("install_tree_sha")

            if not install_dir.is_dir():
                state = "SKILL_NOT_INSTALLED"
            elif lock_sha and installed_sha == lock_sha:
                state = "ok"
            elif lock_sha:
                state = "SKILL_INSTALL_STALE"
            else:
                state = "present"

            skill_entry["targets"].append({
                "id": target["id"],
                "host_path": str(install_dir),
                "present": install_dir.is_dir(),
                "tree_sha256": installed_sha,
                "state": state,
            })

        skills.append(skill_entry)

    return {
        "id": skillset["id"],
        "kind": "skill-repo-set",
        "skill_repos_config": str(skillset.get("skill_repos_config", "")),
        "lock_path": str(skillset.get("lock_path", "")),
        "lock_present": lock_present,
        "lock_error": lock_error,
        "skills": skills,
    }


# ---------------------------------------------------------------------------
# WG-006: parity-ledger enforcement helpers for status/logs/doctor/up
# ---------------------------------------------------------------------------
#
# These helpers consult the declared parity_ledger so the observational and
# lifecycle surfaces (status, logs, doctor, up) can treat the ledger as
# runtime truth instead of documentation.  They deliberately do NOT mutate
# any bridge, bootstrap, or service state -- they only classify requested
# surfaces and build structured error envelopes.
#
# Contract anchors:
#   * flows.md Flow 4/5 (lines 94-138)
#   * backend.md Rule 3a + Rule 6 (lines 47-54, 77-90, 159-169)
#   * shared.md US-4 ACs and doctor contract (lines 148-180, 473-507)


def parity_ledger_items(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the declared parity_ledger items for the active model."""
    items = model.get("parity_ledger") or []
    return [item for item in items if isinstance(item, dict)]


def _parity_surface_keys(item: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("id", "legacy_surface"):
        value = str(item.get(field, "")).strip()
        if value:
            keys.append(value)
    return keys


def parity_ledger_by_surface(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index parity_ledger items by id and legacy_surface for fast lookup."""
    index: dict[str, dict[str, Any]] = {}
    for item in parity_ledger_items(model):
        for key in _parity_surface_keys(item):
            index.setdefault(key, item)
    return index


def parity_ledger_deferred_surfaces(model: dict[str, Any]) -> list[str]:
    """Return the sorted list of non-covered surfaces declared in the ledger.

    Used by the doctor contract (shared.md:473-507) to populate
    ``details.deferred_surfaces`` and by status/focus text renderers to
    advertise what the operator has explicitly chosen to defer.
    """
    deferred: set[str] = set()
    for item in parity_ledger_items(model):
        state = str(item.get("ownership_state", "")).strip()
        if state in ("deferred", "bridge-only", "external"):
            surface = (
                str(item.get("legacy_surface", "")).strip()
                or str(item.get("id", "")).strip()
            )
            if surface:
                deferred.add(surface)
    return sorted(deferred)


def parity_ledger_covered_surfaces(model: dict[str, Any]) -> list[str]:
    """Return the sorted list of covered surfaces declared in the ledger."""
    covered: set[str] = set()
    for item in parity_ledger_items(model):
        if str(item.get("ownership_state", "")).strip() == "covered":
            surface = (
                str(item.get("legacy_surface", "")).strip()
                or str(item.get("id", "")).strip()
            )
            if surface:
                covered.add(surface)
    return sorted(covered)


def ownership_state_for_service(
    model: dict[str, Any],
    service_id: str,
) -> str:
    """Return the ownership_state for a declared service.

    Declared services are treated as ``covered`` unless the parity ledger
    explicitly classifies them otherwise.  Surfaces that are not in the
    service graph and have no ledger entry are reported as ``unknown`` so
    renderers can still surface something meaningful.
    """
    target = str(service_id or "").strip()
    if not target:
        return "unknown"
    index = parity_ledger_by_surface(model)
    item = index.get(target)
    if item is not None:
        state = str(item.get("ownership_state", "")).strip()
        if state:
            return state
    service_ids = {
        str(service.get("id", "")).strip()
        for service in model.get("services") or []
    }
    if target in service_ids:
        return "covered"
    return "unknown"


def parity_ledger_next_action(
    item: dict[str, Any],
    *,
    client_id: str | None = None,
    profile: str | None = None,
) -> str:
    """Compose a next_action hint for a deferred/bridge-only/external item.

    The overlay may declare an explicit ``next_action`` on the item; when
    absent, we synthesize a reasonable pointer from the item's action and
    bridge_dependency per backend.md:159-169.
    """
    explicit = str(item.get("next_action", "")).strip()
    if explicit:
        return explicit
    action = str(item.get("action", "")).strip()
    ownership = str(item.get("ownership_state", "")).strip()
    bridge_dep = str(item.get("bridge_dependency") or "").strip()
    cid = (client_id or "personal").strip() or "personal"
    prof = (profile or "local-core").strip() or "local-core"

    if ownership == "bridge-only" and bridge_dep:
        return (
            f"bridge seam {bridge_dep!r} is only available to covered workflows; "
            f"run 'manage.py focus --client {cid} --profile {prof}' to reconcile it"
        )
    if ownership == "external":
        return (
            "surface is external to the skillbox runtime contract; consult the "
            "upstream owner documented in the overlay parity_ledger"
        )
    if action == "build":
        return (
            f"surface is deferred pending a build slice; track follow-on work "
            f"in the parity_ledger for {str(item.get('id') or item.get('legacy_surface') or '')}"
        )
    if action == "drop":
        return (
            "surface is marked drop in the parity_ledger; do not request it "
            "through the runtime"
        )
    return (
        f"manage.py doctor --client {cid} --profile {prof} --format json"
    )


def build_local_runtime_service_deferred_error(
    item: dict[str, Any],
    *,
    client_id: str | None = None,
    profile: str | None = None,
    requested_mode: str | None = None,
    surface_id: str | None = None,
) -> dict[str, Any]:
    """Build a ``LOCAL_RUNTIME_SERVICE_DEFERRED`` envelope from a ledger item."""
    ownership = str(item.get("ownership_state", "")).strip() or "deferred"
    target_surface = (
        surface_id
        or str(item.get("legacy_surface", "")).strip()
        or str(item.get("id", "")).strip()
    )
    detail = (
        f"Requested surface {target_surface!r} is classified {ownership!r} "
        f"in the parity ledger and cannot be started through the runtime "
        f"lifecycle."
    )
    err = local_runtime_error(
        LOCAL_RUNTIME_SERVICE_DEFERRED,
        detail,
        recoverable=True,
        next_action=parity_ledger_next_action(
            item, client_id=client_id, profile=profile,
        ),
        blocked_services=[target_surface] if target_surface else [],
    )
    err["error"]["ownership_state"] = ownership
    if requested_mode is not None:
        err["error"]["requested_mode"] = requested_mode
    ledger_id = str(item.get("id", "")).strip()
    if ledger_id:
        err["error"]["parity_ledger_id"] = ledger_id
    return err


def classify_requested_surfaces(
    model: dict[str, Any],
    requested_ids: list[str] | None,
) -> dict[str, Any]:
    """Split requested service ids against the graph and the parity ledger.

    Returns a dict with three keys:
      * ``covered``: ids present in the service graph (safe to forward to
        ``select_services``)
      * ``deferred``: list of ``(surface_id, ledger_item)`` tuples for ids
        whose parity ledger entry marks them deferred/bridge-only/external
      * ``unknown``: ids that are neither in the graph nor in the ledger

    An empty ``requested_ids`` list yields an empty classification -- callers
    should interpret that as "no explicit filter; use the profile graph".
    """
    normalized = [str(s).strip() for s in (requested_ids or []) if str(s).strip()]
    service_ids = {
        str(service.get("id", "")).strip()
        for service in model.get("services") or []
    }
    ledger_index = parity_ledger_by_surface(model)

    covered: list[str] = []
    deferred: list[tuple[str, dict[str, Any]]] = []
    unknown: list[str] = []
    for sid in normalized:
        if sid in service_ids:
            item = ledger_index.get(sid)
            if item is not None and str(
                item.get("ownership_state", "")
            ).strip() in ("deferred", "bridge-only", "external"):
                deferred.append((sid, item))
            else:
                covered.append(sid)
            continue
        item = ledger_index.get(sid)
        if item is not None:
            deferred.append((sid, item))
            continue
        unknown.append(sid)

    return {
        "requested": normalized,
        "covered": covered,
        "deferred": deferred,
        "unknown": unknown,
    }


def collect_deferred_log_entries(
    deferred: list[tuple[str, dict[str, Any]]],
    *,
    client_id: str | None = None,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """Produce log-payload entries for deferred/bridge-only/external ids.

    Matches ``collect_service_logs``' shape enough for ``print_service_logs_text``
    to render a deferred note instead of tailing a log file that does not
    exist (flows.md Flow 5).
    """
    entries: list[dict[str, Any]] = []
    for surface_id, item in deferred:
        entries.append(
            {
                "id": surface_id,
                "kind": "deferred",
                "log_file": "",
                "present": False,
                "lines": [],
                "ownership_state": str(item.get("ownership_state", "")).strip()
                or "deferred",
                "deferred": True,
                "next_action": parity_ledger_next_action(
                    item, client_id=client_id, profile=profile,
                ),
            }
        )
    return entries


def runtime_status(model: dict[str, Any]) -> dict[str, Any]:
    repo_statuses: list[dict[str, Any]] = []
    for repo in model["repos"]:
        path = Path(str(repo["host_path"]))
        item = {
            "id": repo["id"],
            "kind": repo.get("kind", "repo"),
            "path": str(repo["path"]),
            "host_path": str(path),
            "present": path.exists(),
            "profiles": repo.get("profiles") or [],
        }
        if path.exists() and path.is_dir():
            item.update(git_repo_state(path))
        repo_statuses.append(item)

    artifact_statuses: list[dict[str, Any]] = []
    for artifact in model["artifacts"]:
        artifact_statuses.append(artifact_state(artifact))

    env_file_statuses = [env_file_state(env_file) for env_file in model["env_files"]]

    skill_statuses: list[dict[str, Any]] = []
    for skillset in model["skills"]:
        if skillset.get("kind") == "skill-repo-set":
            skill_statuses.append(_collect_skill_repo_status(skillset))
            continue
        inventory = collect_skill_inventory(skillset)
        skill_statuses.append(
            {
                "id": inventory["id"],
                "kind": inventory["kind"],
                "bundle_dir": inventory["bundle_dir"],
                "bundle_dir_host_path": inventory["bundle_dir_host_path"],
                "manifest": inventory["manifest"],
                "lock_path": inventory["lock_path"],
                "lock_present": inventory["lock_present"],
                "lock_error": inventory["lock_error"],
                "missing_bundles": inventory["missing_bundles"],
                "extra_bundles": inventory["extra_bundles"],
                "skills": inventory["skills"],
            }
        )

    task_statuses: list[dict[str, Any]] = []
    for task in model["tasks"]:
        item = {
            "id": task["id"],
            "kind": task.get("kind", "task"),
            "profiles": task.get("profiles") or [],
            "depends_on": task_dependency_ids(task),
            "inputs": list(task.get("inputs") or []),
            "outputs": list(task.get("outputs") or []),
        }
        item.update(probe_task(model, task))
        task_statuses.append(item)

    service_statuses: list[dict[str, Any]] = []
    for service in model["services"]:
        item = {
            "id": service["id"],
            "kind": service.get("kind", "service"),
            "profiles": service.get("profiles") or [],
            "depends_on": service_dependency_ids(service),
            "bootstrap_tasks": service_bootstrap_task_ids(service),
        }
        item.update(probe_service(model, service))
        # WG-006: annotate with the parity-ledger ownership_state so
        # observational surfaces (text renderers, json consumers, doctor)
        # can show coverage classification without a second lookup.
        item["ownership_state"] = ownership_state_for_service(
            model, str(service.get("id", ""))
        )
        service_statuses.append(item)

    # WG-006: blocked_services lists declared services whose current probe
    # did not report a running/ok state.  Status stays observational per
    # backend.md Rule 3a -- we do NOT re-run bridges or bootstrap tasks;
    # we just summarise what the graph currently looks like.
    blocked_services: list[str] = [
        str(entry.get("id", ""))
        for entry in service_statuses
        if entry.get("state") not in {"running", "ok", "idle"}
        and str(entry.get("id", "")).strip()
    ]

    log_statuses: list[dict[str, Any]] = []
    for log_item in model["logs"]:
        path = Path(str(log_item["host_path"]))
        item = {
            "id": log_item["id"],
            "path": str(log_item["path"]),
            "host_path": str(path),
        }
        item.update(log_directory_state(path))
        log_statuses.append(item)

    check_statuses: list[dict[str, Any]] = []
    for check in model["checks"]:
        item = {
            "id": check["id"],
            "type": check["type"],
        }
        if check["type"] == "path_exists":
            path = Path(str(check["host_path"]))
            item["path"] = str(check["path"])
            item["host_path"] = str(path)
            item["ok"] = path.exists()
        check_statuses.append(item)

    ingress_paths = ingress_config_paths(model) if has_ingress_runtime(model) else {}
    ingress_payload = {
        "listeners": {
            "public": ingress_listener_settings(model, "public"),
            "private": ingress_listener_settings(model, "private"),
        },
        "route_file": str(ingress_paths["route_file"]) if ingress_paths else "",
        "route_file_present": ingress_paths["route_file"].is_file() if ingress_paths else False,
        "nginx_config": str(ingress_paths["nginx_config"]) if ingress_paths else "",
        "nginx_config_present": ingress_paths["nginx_config"].is_file() if ingress_paths else False,
        "routes": resolved_ingress_routes(model, include_service_state=True),
    }

    return {
        "clients": copy.deepcopy(model.get("clients") or []),
        "active_clients": model.get("active_clients") or [],
        "default_client": (model.get("selection") or {}).get("default_client"),
        "active_profiles": model.get("active_profiles") or [],
        "storage": copy.deepcopy(model.get("storage") or {}),
        "repos": repo_statuses,
        "artifacts": artifact_statuses,
        "env_files": env_file_statuses,
        "skills": skill_statuses,
        "tasks": task_statuses,
        "services": service_statuses,
        "blocked_services": blocked_services,
        "logs": log_statuses,
        "checks": check_statuses,
        "ingress": ingress_payload,
        "parity_ledger": {
            "covered_surfaces": parity_ledger_covered_surfaces(model),
            "deferred_surfaces": parity_ledger_deferred_surfaces(model),
        },
    }
