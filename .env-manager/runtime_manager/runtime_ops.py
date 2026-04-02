from __future__ import annotations

from .shared import *
from .validation import *

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


def doctor_results(model: dict[str, Any], root_dir: Path) -> list[CheckResult]:
    results = check_manifest(model)
    if any(result.status == "fail" for result in results):
        return results
    connector_results = validate_connector_contract(model)
    if any(result.status == "fail" for result in connector_results):
        return results + connector_results
    return (
        results
        + connector_results
        + check_filesystem(model, root_dir)
        + validate_skill_locks_and_state(model)
        + validate_task_state(model)
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

        if path.exists():
            actions.append(f"exists: {path}")
            continue

        if sync_mode == "ensure-directory" or source_kind == "directory":
            ensure_directory(path, dry_run)
            actions.append(f"ensure-directory: {path}")
            continue

        if source_kind == "git" and sync_mode == "clone-if-missing":
            parent = path.parent
            ensure_directory(parent, dry_run)
            url = str(source["url"])
            branch = str(source.get("branch", "")).strip()
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

    actions.extend(sync_skill_sets(model, dry_run=dry_run))
    actions.extend(sync_dcg_config(model, DEFAULT_ROOT_DIR, dry_run=dry_run))
    if not dry_run:
        emit_event("sync.completed", "runtime", {"action_count": len(actions)})
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


def service_supports_lifecycle(service: dict[str, Any]) -> tuple[bool, str | None]:
    if not str(service.get("command") or "").strip():
        return False, "command missing"
    if str(service.get("kind") or "").strip() == "orchestration":
        return False, "orchestration services are status-only"
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
) -> list[dict[str, Any]]:
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
    manageable, reason = service_supports_lifecycle(service)
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
    repo_id = str(item.get("repo") or "").strip()
    repo = runtime_repo_map(model).get(repo_id)
    if repo is not None:
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


def task_success_state(task: dict[str, Any]) -> dict[str, Any]:
    success = task.get("success") or {}
    success_type = success.get("type")
    if success_type == "path_exists":
        path = Path(str(success["host_path"]))
        return {"state": "ok" if path.exists() else "down", "target": str(path)}
    return {"state": "unknown"}


def probe_task(model: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    success_state = task_success_state(task)
    tasks_by_id = task_id_map(model)
    dependency_states = {
        dependency_id: task_success_state(tasks_by_id[dependency_id]).get("state", "unknown")
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
        if process.poll() is not None:
            return {"state": "failed", "exit_code": process.returncode}

        probe = service_healthcheck_state(service)
        if probe.get("state") == "ok":
            return {"state": "ok"} | probe
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


def run_tasks(
    model: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
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

        emit_event("task.started", task["id"])
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
            emit_event("task.failed", task["id"], {"exit_code": completed.returncode})
            raise RuntimeError(
                f"Task {task['id']} failed with exit code {completed.returncode}."
                + (f" Recent logs: {' | '.join(tail)}" if tail else "")
            )

        post_state = probe_task(model, task)
        if post_state["state"] != "ready":
            tail = tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)
            emit_event("task.failed", task["id"], {"reason": "success_check_unsatisfied"})
            raise RuntimeError(
                f"Task {task['id']} completed but did not satisfy its success check."
                + (f" Success target: {post_state['target']}." if post_state.get("target") else "")
                + (f" Recent logs: {' | '.join(tail)}" if tail else "")
            )

        results.append(result | {"result": "completed", "target": post_state.get("target")})
        emit_event("task.completed", task["id"])
    return results


def start_services(
    model: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    dry_run: bool,
    wait_seconds: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for service in services:
        manageable, reason = service_supports_lifecycle(service)
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

        command, env = translated_runtime_command(model, service)
        cwd = resolve_runtime_command_cwd(model, service)
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
            emit_event("service.start_failed", service["id"], {"state": health_state.get("state")})
            raise RuntimeError(
                f"Service {service['id']} failed to become healthy."
                + (f" Exit code: {health_state['exit_code']}." if "exit_code" in health_state else "")
                + (f" Health target: {health_state['url']}." if "url" in health_state else "")
                + (f" Health target: {health_state['target']}." if "target" in health_state else "")
                + (f" Health pattern: {health_state['pattern']}." if "pattern" in health_state else "")
                + (f" Recent logs: {' | '.join(tail)}" if tail else "")
            )

        results.append(result | {"result": "started", "pid": process.pid})
        emit_event("service.started", service["id"], {"pid": process.pid})
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
        manageable, reason = service_supports_lifecycle(service)
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
        emit_event("service.stopped", service["id"], {"signal": signal_used})
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

    if pid is not None:
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

    return {
        "collected_at": time.time(),
        "repos": repo_states,
        "services": service_states,
        "checks": check_states,
        "logs": log_states,
        "sessions": session_states,
    }


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
        service_statuses.append(item)

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

    return {
        "clients": copy.deepcopy(model.get("clients") or []),
        "active_clients": model.get("active_clients") or [],
        "default_client": (model.get("selection") or {}).get("default_client"),
        "active_profiles": model.get("active_profiles") or [],
        "repos": repo_statuses,
        "artifacts": artifact_statuses,
        "env_files": env_file_statuses,
        "skills": skill_statuses,
        "tasks": task_statuses,
        "services": service_statuses,
        "logs": log_statuses,
        "checks": check_statuses,
    }
