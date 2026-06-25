from __future__ import annotations

import http.client
import io
import shlex
import socket
import tarfile

from .shared import *
from .validation import *
from .port_registry import build_port_registry, declared_service_ports
from .pressure_report import collect_pressure_report
from .rch_report import collect_rch_report
from .sbh_report import collect_sbh_report
from lib.runtime_model import (
    PERSISTENT_PATH_OFF_STATE_ROOT,
    STATE_ROOT_LOW_SPACE,
    STATE_ROOT_MISSING,
    STATE_ROOT_WRONG_FILESYSTEM,
    STATE_ROOT_WRONG_OWNERSHIP,
    is_runtime_absolute_path,
)


_STARTED_SERVICE_PROCESSES: list[subprocess.Popen[str]] = []


def reap_started_service_processes() -> None:
    """Reap service launcher processes that have exited since startup."""
    running: list[subprocess.Popen[str]] = []
    for process in _STARTED_SERVICE_PROCESSES:
        if process.poll() is None:
            running.append(process)
            continue
        try:
            process.communicate(timeout=0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    _STARTED_SERVICE_PROCESSES[:] = running


def track_started_service_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        reap_started_service_processes()
        _STARTED_SERVICE_PROCESSES.append(process)


def _root_for_runtime_log_dir(log_dir: Path) -> Path | None:
    expected_parts = RUNTIME_LOG_REL.parent.parts
    parts = log_dir.expanduser().parts
    if len(parts) < len(expected_parts):
        return None
    if parts[-len(expected_parts):] != expected_parts:
        return None
    root_parts = parts[:-len(expected_parts)]
    if not root_parts:
        return Path(".")
    return Path(*root_parts)


def _model_absolute_path(model: dict[str, Any], raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return Path(str(model.get("root_dir") or DEFAULT_ROOT_DIR)) / path


def _runtime_event_root(model: dict[str, Any]) -> Path:
    for log in model.get("logs") or []:
        if str(log.get("id") or "") != "runtime":
            continue
        raw_host_path = str(log.get("host_path") or "").strip()
        if not raw_host_path:
            continue
        event_root = _root_for_runtime_log_dir(_model_absolute_path(model, raw_host_path))
        if event_root is not None:
            return event_root

    storage = model.get("storage") if isinstance(model.get("storage"), dict) else {}
    raw_state_root = str((storage or {}).get("state_root") or "").strip()
    if raw_state_root:
        return _model_absolute_path(model, raw_state_root)

    return Path(str(model.get("root_dir") or DEFAULT_ROOT_DIR))


def _log_model_runtime_event(
    model: dict[str, Any],
    event_type: str,
    subject: str,
    detail: dict[str, Any] | None = None,
) -> None:
    log_runtime_event(event_type, subject, detail, root_dir=_runtime_event_root(model))


def _storage_filesystem_type(path: Path) -> str:
    result = run_command(["findmnt", "-no", "FSTYPE", "--target", str(path)])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _persistent_storage_bindings(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        binding for binding in bindings
        if str(binding.get("storage_class") or "").strip() == "persistent"
    ]


def _missing_state_root_status(provider: str, required: bool) -> str:
    return "fail" if provider == "digitalocean" or required else "warn"


def _missing_state_root_result(
    state_root: Path,
    provider: str,
    required: bool,
) -> CheckResult:
    return CheckResult(
        status=_missing_state_root_status(provider, required),
        code=STATE_ROOT_MISSING,
        message="state root is missing",
        details={"state_root": str(state_root), "provider": provider},
    )


def _digitalocean_mount_results(provider: str, required: bool, state_root: Path) -> list[CheckResult]:
    if provider != "digitalocean" or not required or state_root.is_mount():
        return []
    return [
        CheckResult(
            status="fail",
            code=STATE_ROOT_MISSING,
            message="DigitalOcean state root exists but is not mounted",
            details={"state_root": str(state_root)},
        )
    ]


def _filesystem_posture_results(
    provider: str,
    expected_filesystem: str,
    state_root: Path,
) -> list[CheckResult]:
    if provider != "digitalocean" or not expected_filesystem:
        return []
    actual_filesystem = _storage_filesystem_type(state_root)
    if not actual_filesystem or actual_filesystem == expected_filesystem:
        return []
    return [
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
    ]


def _ownership_posture_results(state_root: Path) -> list[CheckResult]:
    if os.access(state_root, os.W_OK | os.X_OK):
        return []
    return [
        CheckResult(
            status="fail",
            code=STATE_ROOT_WRONG_OWNERSHIP,
            message="state root is not writable by the current runtime user",
            details={"state_root": str(state_root)},
        )
    ]


def _state_root_free_gb(state_root: Path) -> float:
    usage = shutil.disk_usage(state_root)
    return round(usage.free / (1024 ** 3), 2)


def _free_space_posture_results(
    state_root: Path,
    min_free_gb: float,
    free_gb: float,
) -> list[CheckResult]:
    if free_gb >= min_free_gb:
        return []
    return [
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
    ]


def _off_state_root_binding_results(
    state_root: Path,
    persistent_bindings: list[dict[str, Any]],
) -> list[CheckResult]:
    results: list[CheckResult] = []
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
    return results


def _storage_posture_pass_result(
    provider: str,
    state_root: Path,
    persistent_bindings: list[dict[str, Any]],
    free_gb: float,
) -> CheckResult:
    return CheckResult(
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
    persistent_bindings = _persistent_storage_bindings(bindings)

    if not str(state_root):
        return [
            CheckResult(
                status="fail",
                code=STATE_ROOT_MISSING,
                message="storage summary does not include a state_root",
            )
        ]

    results: list[CheckResult] = []
    if not state_root.exists():
        return [_missing_state_root_result(state_root, provider, required)]

    results.extend(_digitalocean_mount_results(provider, required, state_root))
    results.extend(_filesystem_posture_results(provider, expected_filesystem, state_root))
    results.extend(_ownership_posture_results(state_root))
    free_gb = _state_root_free_gb(state_root)
    results.extend(_free_space_posture_results(state_root, min_free_gb, free_gb))
    results.extend(_off_state_root_binding_results(state_root, persistent_bindings))

    if results:
        return results

    return [_storage_posture_pass_result(provider, state_root, persistent_bindings, free_gb)]

def normalize_file_mode(raw_mode: Any, default: int = 0o600) -> int:
    if raw_mode is None:
        return default
    if isinstance(raw_mode, bool):
        raise ValidationError(
            "runtime_error",
            f"Invalid file mode {raw_mode!r}. Use an octal string such as '0600'.",
            context={"mode": raw_mode},
        )
    if isinstance(raw_mode, int):
        mode = raw_mode
    else:
        text = str(raw_mode).strip()
        if not text:
            return default
        try:
            mode = int(text, 8)
        except ValueError as exc:
            raise ValidationError(
                "runtime_error",
                f"Invalid file mode {raw_mode!r}. Use an octal string such as '0600'.",
                context={"mode": raw_mode},
            ) from exc

    if mode < 0 or mode > 0o777:
        raise ValidationError(
            "runtime_error",
            f"Invalid file mode {raw_mode!r}. Use an octal value between '0000' and '0777'.",
            context={"mode": raw_mode},
        )
    return mode


def artifact_state(artifact: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(artifact["host_path"]))
    source = artifact.get("source") or {}
    source_kind = str(source.get("kind", "manual")).strip() or "manual"
    sync_mode = _artifact_sync_mode(artifact, source_kind)
    source_path = _artifact_source_path(source)
    present = path.is_file()
    source_present = bool(source_path and source_path.is_file())
    actual_sha256 = file_sha256(path) if present else ""
    state, syncable, desired_sha256 = _artifact_state_details(
        source=source,
        source_kind=source_kind,
        sync_mode=sync_mode,
        present=present,
        source_present=source_present,
        source_path=source_path,
        actual_sha256=actual_sha256,
    )

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


def _artifact_sync_mode(artifact: dict[str, Any], source_kind: str) -> str:
    sync = artifact.get("sync") or {}
    default_mode = "download-if-missing" if source_kind == "url" else "copy-if-missing" if source_kind == "file" else "manual"
    return str(sync.get("mode") or default_mode).strip()


def _artifact_source_path(source: dict[str, Any]) -> Path | None:
    raw_source_path = str(source.get("host_path") or source.get("path") or "").strip()
    return Path(raw_source_path) if raw_source_path else None


def _artifact_digest_state(present: bool, actual_sha256: str, desired_sha256: str) -> str:
    if not present:
        return "missing"
    return "ok" if actual_sha256 == desired_sha256 else "stale"


def _file_artifact_state_details(
    *,
    present: bool,
    source_present: bool,
    source_path: Path | None,
    actual_sha256: str,
) -> tuple[str, bool, str]:
    if not source_present or source_path is None:
        return ("present" if present else "missing"), False, ""
    desired_sha256 = file_sha256(source_path)
    return _artifact_digest_state(present, actual_sha256, desired_sha256), True, desired_sha256


def _url_artifact_state_details(
    *,
    source: dict[str, Any],
    present: bool,
    actual_sha256: str,
) -> tuple[str, bool, str]:
    url = str(source.get("url") or "").strip()
    raw_sha256 = str(source.get("sha256") or "").strip().lower()
    syncable = bool(url)
    if not SHA256_HEX_PATTERN.fullmatch(raw_sha256):
        return ("present" if present else "missing"), syncable, ""
    state = _artifact_digest_state(present, actual_sha256, raw_sha256)
    return state, syncable, raw_sha256


def _artifact_state_details(
    *,
    source: dict[str, Any],
    source_kind: str,
    sync_mode: str,
    present: bool,
    source_present: bool,
    source_path: Path | None,
    actual_sha256: str,
) -> tuple[str, bool, str]:
    if source_kind == "file" and sync_mode == "copy-if-missing":
        return _file_artifact_state_details(
            present=present,
            source_present=source_present,
            source_path=source_path,
            actual_sha256=actual_sha256,
        )
    if source_kind == "url" and sync_mode == "download-if-missing":
        return _url_artifact_state_details(
            source=source,
            present=present,
            actual_sha256=actual_sha256,
        )
    return ("present" if present else "missing"), False, ""


def env_file_state(env_file: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(env_file["host_path"]))
    source = env_file.get("source") or {}
    source_kind = str(source.get("kind", "manual")).strip() or "manual"
    sync_mode = _env_file_sync_mode(env_file, source_kind)
    desired_mode = normalize_file_mode(env_file.get("mode"), default=0o600)
    source_path = _artifact_source_path(source)
    present = path.is_file()
    source_present = bool(source_path and source_path.is_file())
    state, syncable = _env_file_state_details(
        source_kind=source_kind,
        sync_mode=sync_mode,
        present=present,
        source_present=source_present,
        path=path,
        source_path=source_path,
        desired_mode=desired_mode,
    )

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


def _env_file_sync_mode(env_file: dict[str, Any], source_kind: str) -> str:
    sync = env_file.get("sync") or {}
    return str(sync.get("mode") or ("write" if source_kind == "file" else "manual")).strip()


def _env_file_write_state(
    *,
    present: bool,
    source_present: bool,
    path: Path,
    source_path: Path | None,
    desired_mode: int,
) -> tuple[str, bool]:
    if not source_present or source_path is None:
        return "source-missing", False
    if not present:
        return "missing", True
    target_mode = path.stat().st_mode & 0o777
    if path.read_bytes() != source_path.read_bytes() or target_mode != desired_mode:
        return "stale", True
    return "ok", False


def _env_file_state_details(
    *,
    source_kind: str,
    sync_mode: str,
    present: bool,
    source_present: bool,
    path: Path,
    source_path: Path | None,
    desired_mode: int,
) -> tuple[str, bool]:
    if source_kind == "file" and sync_mode == "write":
        return _env_file_write_state(
            present=present,
            source_present=source_present,
            path=path,
            source_path=source_path,
            desired_mode=desired_mode,
        )
    return ("ok" if present else "missing"), False


def _scan_repo_paths(model: dict[str, Any], root_dir: Path) -> tuple[list[str], list[str]]:
    missing_syncable: list[str] = []
    missing_required: list[str] = []
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
            missing_syncable.append(repo_rel(root_dir, path))
        elif repo.get("required"):
            missing_required.append(repo_rel(root_dir, path))
    return missing_syncable, missing_required


def _scan_artifact_paths(model: dict[str, Any], root_dir: Path) -> tuple[list[str], list[str], list[str]]:
    missing_syncable: list[str] = []
    stale_syncable: list[str] = []
    missing_required: list[str] = []
    for artifact in model["artifacts"]:
        state = artifact_state(artifact)
        display_path = repo_rel(root_dir, Path(state["host_path"]))
        if state["state"] == "missing":
            if state["syncable"]:
                missing_syncable.append(display_path)
            elif artifact.get("required"):
                missing_required.append(display_path)
        elif state["state"] == "stale" and state["syncable"]:
            stale_syncable.append(display_path)
    return missing_syncable, stale_syncable, missing_required


def _scan_env_file_paths(
    model: dict[str, Any], root_dir: Path, bridge_output_paths: set[str],
) -> tuple[list[str], list[str], list[str]]:
    syncable: list[str] = []
    missing_sources: list[str] = []
    missing_targets: list[str] = []
    for env_file in model["env_files"]:
        state = env_file_state(env_file)
        display_path = repo_rel(root_dir, Path(state["host_path"]))
        if state["state"] == "source-missing":
            if not env_file.get("required"):
                continue
            source_host_path = (
                str(Path(state["source_host_path"]).resolve())
                if state["source_host_path"] else ""
            )
            if source_host_path and source_host_path in bridge_output_paths:
                continue
            if state["source_host_path"]:
                missing_sources.append(repo_rel(root_dir, Path(state["source_host_path"])))
            else:
                missing_sources.append(state["source_path"] or display_path)
        elif state["state"] in {"missing", "stale"}:
            if state["syncable"]:
                syncable.append(display_path)
            elif env_file.get("required"):
                missing_targets.append(display_path)
    return syncable, missing_sources, missing_targets


def _scan_log_paths(model: dict[str, Any], root_dir: Path) -> list[str]:
    return [
        repo_rel(root_dir, Path(str(log_item["host_path"])))
        for log_item in model["logs"]
        if not Path(str(log_item["host_path"])).exists()
    ]


def _scan_check_paths(model: dict[str, Any], root_dir: Path) -> list[str]:
    missing: list[str] = []
    for check in model["checks"]:
        if check.get("type") != "path_exists":
            continue
        path = Path(str(check["host_path"]))
        if not path.exists() and check.get("required"):
            missing.append(repo_rel(root_dir, path))
    return missing


def _filesystem_check_result(
    code: str,
    fail_status: str,
    fail_msg: str,
    pass_msg: str,
    details: dict[str, Any] | None,
) -> CheckResult:
    """Build pass result when details is empty/None, otherwise fail/warn with details."""
    if not details:
        return CheckResult(status="pass", code=code, message=pass_msg)
    return CheckResult(status=fail_status, code=code, message=fail_msg, details=details)


def check_filesystem(model: dict[str, Any], root_dir: Path) -> list[CheckResult]:
    bridge_output_paths = _filesystem_bridge_output_paths(model)
    syncable_repos, required_repos = _scan_repo_paths(model, root_dir)
    syncable_artifacts, stale_artifacts, required_artifacts = _scan_artifact_paths(model, root_dir)
    syncable_env_files, missing_env_sources, missing_env_targets = _scan_env_file_paths(
        model, root_dir, bridge_output_paths,
    )
    missing_logs = _scan_log_paths(model, root_dir)
    missing_checks = _scan_check_paths(model, root_dir)
    artifact_warn_details = _artifact_warn_details(syncable_artifacts, stale_artifacts)
    env_fail_details = _env_fail_details(missing_env_sources, missing_env_targets)
    return _filesystem_results(
        required_repos=required_repos,
        syncable_repos=syncable_repos,
        required_artifacts=required_artifacts,
        artifact_warn_details=artifact_warn_details,
        env_fail_details=env_fail_details,
        syncable_env_files=syncable_env_files,
        missing_logs=missing_logs,
        missing_checks=missing_checks,
    )


def _filesystem_bridge_output_paths(model: dict[str, Any]) -> set[str]:
    return {
        str(path.resolve())
        for bridge in model.get("bridges") or []
        for path in bridge_expected_outputs(bridge)
    }


def _artifact_warn_details(syncable_artifacts: list[str], stale_artifacts: list[str]) -> dict[str, Any]:
    artifact_warn_details: dict[str, Any] = {}
    if syncable_artifacts:
        artifact_warn_details["missing"] = syncable_artifacts
    if stale_artifacts:
        artifact_warn_details["stale"] = stale_artifacts
    return artifact_warn_details


def _env_fail_details(missing_env_sources: list[str], missing_env_targets: list[str]) -> dict[str, Any]:
    env_fail_details: dict[str, Any] = {}
    if missing_env_sources:
        env_fail_details["missing_sources"] = missing_env_sources
    if missing_env_targets:
        env_fail_details["missing_targets"] = missing_env_targets
    return env_fail_details


def _filesystem_results(
    *,
    required_repos: list[str],
    syncable_repos: list[str],
    required_artifacts: list[str],
    artifact_warn_details: dict[str, Any],
    env_fail_details: dict[str, Any],
    syncable_env_files: list[str],
    missing_logs: list[str],
    missing_checks: list[str],
) -> list[CheckResult]:
    return [
        _filesystem_check_result(
            "required-runtime-paths", "fail",
            "required runtime repo paths are missing",
            "required runtime repo paths are present",
            {"missing": required_repos} if required_repos else None,
        ),
        _filesystem_check_result(
            "syncable-repo-paths", "warn",
            "managed repo paths are missing but can be created by sync",
            "managed repo paths do not need sync",
            {"missing": syncable_repos} if syncable_repos else None,
        ),
        _filesystem_check_result(
            "required-runtime-artifacts", "fail",
            "required runtime artifact paths are missing",
            "required runtime artifact paths are present",
            {"missing": required_artifacts} if required_artifacts else None,
        ),
        _filesystem_check_result(
            "syncable-artifact-paths", "warn",
            "managed artifact paths are missing or stale but can be reconciled by sync",
            "managed artifact paths do not need sync",
            artifact_warn_details or None,
        ),
        _filesystem_check_result(
            "required-runtime-env-files", "fail",
            "required runtime env files cannot be materialized",
            "required runtime env files are materialized or source-backed",
            env_fail_details or None,
        ),
        _filesystem_check_result(
            "syncable-env-files", "warn",
            "managed env files are missing or stale but can be materialized by sync",
            "managed env files do not need sync",
            {"targets": syncable_env_files} if syncable_env_files else None,
        ),
        _filesystem_check_result(
            "runtime-log-paths", "warn",
            "managed log directories are missing but can be created by sync",
            "managed log directories are present",
            {"missing": missing_logs} if missing_logs else None,
        ),
        _filesystem_check_result(
            "required-runtime-checks", "fail",
            "required runtime checks failed",
            "required runtime checks passed",
            {"missing": missing_checks} if missing_checks else None,
        ),
    ]


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


def ingress_route_path(route: dict[str, Any]) -> str:
    return str(route.get("path") or route.get("path_prefix") or "").strip()


def ingress_route_strip_prefix(route: dict[str, Any]) -> bool:
    return route.get("strip_prefix") is True


def sorted_ingress_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        routes,
        key=lambda route: (
            str(route.get("listener") or "public"),
            0 if str(route.get("match") or "exact") == "exact" else 1,
            -len(ingress_route_path(route)),
            ingress_route_path(route),
            str(route.get("id") or ""),
        ),
    )


def _services_by_id(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(service.get("id") or "").strip(): service
        for service in model.get("services") or []
        if str(service.get("id") or "").strip()
    }


def _resolved_ingress_route_entry(
    model: dict[str, Any],
    route: dict[str, Any],
    services_by_id: dict[str, dict[str, Any]],
    *,
    include_service_state: bool,
) -> dict[str, Any]:
    listener = str(route.get("listener") or "public").strip().lower() or "public"
    path = ingress_route_path(route)
    match = str(route.get("match") or "exact").strip().lower() or "exact"
    service_id = str(route.get("service_id") or "").strip()
    service = services_by_id.get(service_id)
    listener_settings = ingress_listener_settings(model, listener)
    entry = {
        "id": str(route.get("id") or "").strip(),
        "client": str(route.get("client") or "").strip(),
        "profiles": list(route.get("profiles") or []),
        "listener": listener,
        "path": path,
        "path_prefix": str(route.get("path_prefix") or "").strip(),
        "match": match,
        "strip_prefix": ingress_route_strip_prefix(route),
        "host": str(route.get("host") or "").strip(),
        "service_id": service_id,
        "request_url": f"{listener_settings['base_url']}{path}" if path else listener_settings["base_url"],
        "origin_url": service_origin_url(service),
    }
    if include_service_state:
        entry["service_state"] = probe_service(model, service).get("state", "missing") if service else "missing"
    return entry


def resolved_ingress_routes(
    model: dict[str, Any],
    *,
    include_service_state: bool = False,
) -> list[dict[str, Any]]:
    services_by_id = _services_by_id(model)
    return sorted_ingress_routes([
        _resolved_ingress_route_entry(
            model,
            route,
            services_by_id,
            include_service_state=include_service_state,
        )
        for route in model.get("ingress_routes") or []
    ])


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
                "path_prefix": route["path_prefix"],
                "match": route["match"],
                "strip_prefix": route["strip_prefix"],
                "host": route["host"],
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
        atomic_write_text(path, desired)
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


def validate_service_exposure(model: dict[str, Any]) -> list[CheckResult]:
    """Lint service exposure against the box's network posture policy."""
    box_access = runtime_box_access_from_env(model.get("env") or {})
    posture = str(os.environ.get("SKILLBOX_NETWORK_POSTURE") or "").strip()
    if not posture:
        return []
    try:
        from .endpoints import service_endpoint_exposure
    except Exception:
        return []
    violations: list[dict[str, Any]] = []
    for svc in model.get("services") or []:
        endpoint = service_endpoint_exposure(model, svc, box_access=box_access)
        if endpoint is None:
            continue
        exposure = endpoint.get("exposure", "")
        if posture == "tailnet_only" and exposure == "wildcard-direct":
            violations.append({
                "service_id": svc.get("id"),
                "exposure": exposure,
                "warning": endpoint.get("warning", ""),
            })
    if violations:
        return [
            CheckResult(
                status="warn",
                code="service-exposure-violation",
                message=(
                    f"{len(violations)} service(s) use wildcard-direct exposure "
                    f"which violates {posture} posture"
                ),
                details={"posture": posture, "violations": violations},
            )
        ]
    return [
        CheckResult(
            status="pass",
            code="service-exposure-posture",
            message=f"all service exposures comply with {posture} posture",
            details={"posture": posture},
        )
    ]


PORT_COLLISION = "PORT_COLLISION"
PORT_WILDCARD_BIND = "PORT_WILDCARD_BIND"
PORT_UNDECLARED_RESERVED = "PORT_UNDECLARED_RESERVED"
PORT_REGISTRY_WARNING = "PORT_REGISTRY_WARNING"
PORT_CROSS_CLIENT_OVERLAP = "PORT_CROSS_CLIENT_OVERLAP"
PORT_CONTRACT_UNADOPTED = "PORT_CONTRACT_UNADOPTED"
PORT_CONTRACT_GITIGNORE = "PORT_CONTRACT_GITIGNORE"
PORT_CONTRACT_STALE = "PORT_CONTRACT_STALE"
PORT_CONTRACT_FILE_NAME = ".skillbox-port.env"
PORT_GUARD_TELEMETRY_NAME = "port-guard.telemetry.json"

_PORT_CONTRACT_ADOPTION_SNIPPET = (
    "Load .skillbox-port.env before dev startup; for Vite use "
    "server: { host: process.env.HOST || '127.0.0.1', "
    "port: Number(process.env.PORT), strictPort: true }."
)
_PORT_CONTRACT_ADOPTION_FILES = (
    "vite.config.ts",
    "vite.config.js",
    "vite.config.mts",
    "vite.config.mjs",
    "vite.config.cts",
    "vite.config.cjs",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "package.json",
    "Makefile",
    ".envrc",
    "README.md",
)

# Box network postures under which a wildcard (0.0.0.0/::) bind is a violation.
_WILDCARD_DENY_POSTURES = frozenset({"tailnet_only"})


def _resolved_network_posture(model: dict[str, Any]) -> str:
    """Resolve the box network posture from env, falling back to the process env.

    Managed boxes default to ``tailnet_only`` (see AGENTS.md Network Posture),
    but we only treat the posture as wildcard-denying when it is explicitly
    declared so local dev (no posture set) stays permissive.
    """
    env = model.get("env") or {}
    posture = str(env.get("SKILLBOX_NETWORK_POSTURE") or "").strip()
    if not posture:
        posture = str(os.environ.get("SKILLBOX_NETWORK_POSTURE") or "").strip()
    return posture.lower()


def _reserved_port_ranges(model: dict[str, Any]) -> list[tuple[int, int, str]]:
    """Parse declared reserved port ranges into ``(low, high, label)`` tuples.

    Sources, in order: a model-level ``port_reserved_ranges`` list (each item
    ``{low,high,label?}`` or ``[low,high]``) and the
    ``SKILLBOX_RESERVED_PORT_RANGES`` env key (``"9000-9100:agents,4000"``).
    Unparseable ranges are skipped silently so a typo never crashes doctor.
    """
    ranges: list[tuple[int, int, str]] = []

    for raw in model.get("port_reserved_ranges") or []:
        try:
            if isinstance(raw, dict):
                low = int(raw["low"])
                high = int(raw.get("high", raw["low"]))
                label = str(raw.get("label") or "")
            elif isinstance(raw, (list, tuple)) and raw:
                low = int(raw[0])
                high = int(raw[1]) if len(raw) > 1 else low
                label = str(raw[2]) if len(raw) > 2 else ""
            else:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if low <= high:
            ranges.append((low, high, label))

    env = model.get("env") or {}
    raw_env = str(env.get("SKILLBOX_RESERVED_PORT_RANGES") or "").strip()
    for chunk in raw_env.split(","):
        token = chunk.strip()
        if not token:
            continue
        label = ""
        if ":" in token:
            token, label = token.split(":", 1)
            token, label = token.strip(), label.strip()
        try:
            if "-" in token:
                low_s, high_s = token.split("-", 1)
                low, high = int(low_s), int(high_s)
            else:
                low = high = int(token)
        except ValueError:
            continue
        if low <= high:
            ranges.append((low, high, label))
    return ranges


def _active_scope_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entries that actively bind a port, declared ports only.

    Only ``service`` entries are real listeners for collision purposes:

    - ``env_surface`` entries document the declared env value, not a live
      listener, so flagging an env key against the service that consumes it
      would be a false positive.
    - ``ingress`` listener entries are synthesized from env and are realized by
      the ingress-router SERVICE (already a service entry on the same port), so
      counting both would double-claim the listener port.
    """
    return [
        entry
        for entry in entries
        if entry.get("port") is not None and entry.get("owner_kind") == "service"
    ]


def _collision_results(entries: list[dict[str, Any]]) -> list[CheckResult]:
    """PORT_COLLISION within the active scope, plus a cross-client ADVISORY.

    Two active-scope owners on the same port collide. Collisions where the
    owners belong to DIFFERENT clients are downgraded to a non-fatal advisory
    (warn) because client overlays load only when that client is active; same
    or core scope collisions are hard failures.
    """
    by_port: dict[int, list[dict[str, Any]]] = {}
    for entry in _active_scope_entries(entries):
        by_port.setdefault(int(entry["port"]), []).append(entry)

    results: list[CheckResult] = []
    for port in sorted(by_port):
        owners = by_port[port]
        if len(owners) < 2:
            continue
        clients = {str(o.get("client") or "") for o in owners}
        named = [
            {
                "owner_id": o["owner_id"],
                "owner_kind": o["owner_kind"],
                "client": o.get("client") or "",
                "source": o.get("source"),
            }
            for o in owners
        ]
        owner_phrase = " and ".join(
            f"{o['owner_id']} ({o['source'].get('file')}:{o['source'].get('key')})"
            for o in named
        )
        cross_client_only = len(clients) > 1 and not _same_scope_collision(owners)
        if cross_client_only:
            results.append(
                CheckResult(
                    status="warn",
                    code=PORT_CROSS_CLIENT_OVERLAP,
                    message=(
                        f"port {port} is claimed across clients by {owner_phrase}; "
                        "advisory only — these overlays do not load simultaneously"
                    ),
                    details={"port": port, "owners": named, "advisory": True},
                )
            )
        else:
            results.append(
                CheckResult(
                    status="fail",
                    code=PORT_COLLISION,
                    message=f"port {port} is claimed by {owner_phrase}",
                    details={"port": port, "owners": named},
                )
            )
    return results


def _same_scope_collision(owners: list[dict[str, Any]]) -> bool:
    """True when at least two colliding owners share the active (core/same) scope.

    A core-scope owner (client == "") collides with everything in the active
    scope; two owners under the SAME client also collide hard.
    """
    core_owners = [o for o in owners if not str(o.get("client") or "")]
    if len(core_owners) >= 2:
        return True
    if core_owners and len(owners) > len(core_owners):
        return True
    seen_clients: set[str] = set()
    for owner in owners:
        client = str(owner.get("client") or "")
        if client and client in seen_clients:
            return True
        if client:
            seen_clients.add(client)
    return False


def _wildcard_results(entries: list[dict[str, Any]], posture: str) -> list[CheckResult]:
    if posture not in _WILDCARD_DENY_POSTURES:
        return []
    offenders = [
        {
            "port": entry["port"],
            "owner_id": entry["owner_id"],
            "owner_kind": entry["owner_kind"],
            "client": entry.get("client") or "",
            "source": entry.get("source"),
        }
        for entry in entries
        if entry.get("bind_scope") == "wildcard" and entry.get("port") is not None
    ]
    if not offenders:
        return []
    return [
        CheckResult(
            status="fail",
            code=PORT_WILDCARD_BIND,
            message=(
                f"{len(offenders)} port(s) bind 0.0.0.0/:: while box posture is "
                f"{posture}: " + ", ".join(f"{o['owner_id']}:{o['port']}" for o in offenders)
            ),
            details={"posture": posture, "offenders": offenders},
        )
    ]


def _reserved_results(entries: list[dict[str, Any]], model: dict[str, Any]) -> list[CheckResult]:
    ranges = _reserved_port_ranges(model)
    if not ranges:
        return []
    owned_ports = {int(e["port"]) for e in entries if e.get("port") is not None}
    undeclared: list[dict[str, Any]] = []
    for low, high, label in ranges:
        for port in range(low, high + 1):
            if port not in owned_ports:
                undeclared.append({"port": port, "range": [low, high], "label": label})
    if not undeclared:
        return [
            CheckResult(
                status="pass",
                code="port-reserved-ranges",
                message=f"all {len(ranges)} reserved port range(s) are fully owned",
                details={"ranges": [{"low": r[0], "high": r[1], "label": r[2]} for r in ranges]},
            )
        ]
    return [
        CheckResult(
            status="fail",
            code=PORT_UNDECLARED_RESERVED,
            message=(
                f"{len(undeclared)} reserved port(s) have no declared owner: "
                + ", ".join(str(item["port"]) for item in undeclared[:8])
                + ("..." if len(undeclared) > 8 else "")
            ),
            details={"undeclared": undeclared},
        )
    ]


def _repo_host_path(repo: dict[str, Any]) -> Path | None:
    raw_path = str(repo.get("host_path") or repo.get("path") or "").strip()
    if not raw_path:
        return None
    return Path(raw_path).expanduser()


def _port_contract_env_key(service_id: str, suffix: str) -> str:
    normalized = "".join(
        char.upper() if char.isalnum() else "_"
        for char in str(service_id or "").strip()
    ).strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    if not normalized:
        normalized = "SERVICE"
    return f"SKILLBOX_{normalized}_{suffix}"


def _normalize_port_contract_host(host: str) -> str:
    normalized = str(host or "").strip()
    if not normalized or normalized.lower() == "localhost":
        return "127.0.0.1"
    return normalized


def _service_command_host(service: dict[str, Any]) -> str:
    command = str(service.get("command") or "").strip()
    if not command and isinstance(service.get("commands"), dict):
        commands = service.get("commands") or {}
        command = str(commands.get("reuse") or commands.get("start") or commands.get("run") or "").strip()
    if not command:
        return ""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for index, token in enumerate(tokens):
        for flag in ("--host", "--hostname"):
            if token == flag and index + 1 < len(tokens):
                return str(tokens[index + 1]).strip()
            prefix = f"{flag}="
            if token.startswith(prefix):
                return token[len(prefix):].strip()
    return ""


def _service_health_host(service: dict[str, Any]) -> str:
    healthcheck = service.get("healthcheck") if isinstance(service.get("healthcheck"), dict) else {}
    if not healthcheck:
        return ""
    raw_host = str(healthcheck.get("host") or "").strip()
    if raw_host:
        return raw_host
    raw_url = str(healthcheck.get("url") or "").strip()
    if not raw_url:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw_url)
    except ValueError:
        return ""
    return str(parsed.hostname or "").strip()


def _port_contract_host_for_service(service: dict[str, Any], entry: dict[str, Any]) -> str:
    command_host = _service_command_host(service)
    if command_host:
        return _normalize_port_contract_host(command_host)

    declared = _service_declared_listen_port(service)
    if declared is not None and int(declared[1]) == int(entry["port"]):
        return _normalize_port_contract_host(declared[0])

    health_host = _service_health_host(service)
    if health_host:
        return _normalize_port_contract_host(health_host)

    if str(entry.get("bind_scope") or "") == "wildcard":
        return "0.0.0.0"
    return "127.0.0.1"


def _port_contract_records(model: dict[str, Any]) -> list[dict[str, Any]]:
    repos = runtime_repo_map(model)
    services = {
        str(service.get("id") or "").strip(): service
        for service in model.get("services") or []
        if str(service.get("id") or "").strip()
    }
    records: list[dict[str, Any]] = []
    for entry in build_port_registry(model):
        if entry.get("owner_kind") != "service" or entry.get("port") is None or entry.get("warning"):
            continue
        service_id = str(entry.get("owner_id") or "").strip()
        service = services.get(service_id)
        if service is None or ownership_state_for_service(model, service_id) != "covered":
            continue
        repo_id = str(service.get("repo") or service.get("repo_id") or "").strip()
        if not repo_id:
            continue
        repo = repos.get(repo_id)
        if repo is None:
            continue
        repo_path = _repo_host_path(repo)
        if repo_path is None:
            continue
        records.append(
            {
                "service_id": service_id,
                "repo_id": repo_id,
                "repo_path": repo_path,
                "port": int(entry["port"]),
                "host": _port_contract_host_for_service(service, entry),
                "source": dict(entry.get("source") or {}),
                "protocol": str(entry.get("protocol") or ""),
                "bind_scope": str(entry.get("bind_scope") or "unknown"),
                "service": service,
            }
        )
    return sorted(
        records,
        key=lambda item: (str(item["repo_path"]), str(item["service_id"]), int(item["port"])),
    )


def _port_contract_default_rank(record: dict[str, Any]) -> tuple[int, str]:
    config = record["service"].get("port_contract")
    is_default = isinstance(config, dict) and bool(config.get("default"))
    return (0 if is_default else 1, str(record["service_id"]))


def _render_port_contract_file(records: list[dict[str, Any]]) -> str:
    first = sorted(records, key=_port_contract_default_rank)[0]
    lines = [
        "# Generated by Skillbox. Do not commit.",
        "# Source of truth: python3 .env-manager/manage.py ports --format json",
        f"PORT={first['port']}",
        f"HOST={first['host']}",
        f"SKILLBOX_SERVICE_ID={first['service_id']}",
    ]
    source = first.get("source") or {}
    source_file = str(source.get("file") or "").strip()
    source_key = str(source.get("key") or "").strip()
    if source_file or source_key:
        lines.append(f"SKILLBOX_PORT_SOURCE={source_file}:{source_key}".rstrip(":"))
    if len(records) > 1:
        lines.append("")
        lines.append("# Additional declared services in this repo.")
        for record in records:
            if str(record["service_id"]) == str(first["service_id"]):
                continue
            prefix = _port_contract_env_key(str(record["service_id"]), "")
            lines.extend(
                [
                    f"{prefix}PORT={record['port']}",
                    f"{prefix}HOST={record['host']}",
                ]
            )
    return "\n".join(lines) + "\n"


def _port_contract_groups(model: dict[str, Any]) -> list[tuple[Path, list[dict[str, Any]]]]:
    grouped: dict[Path, list[dict[str, Any]]] = {}
    for record in _port_contract_records(model):
        grouped.setdefault(record["repo_path"], []).append(record)
    return [(path, grouped[path]) for path in sorted(grouped, key=lambda p: str(p))]


def sync_port_contracts(model: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []
    for repo_path, records in _port_contract_groups(model):
        target = repo_path / PORT_CONTRACT_FILE_NAME
        service_count = len(records)
        if not repo_path.is_dir():
            actions.append(f"skip-port-contract: {target} (repo missing)")
            continue
        desired = _render_port_contract_file(records)
        state = managed_text_artifact_state(target, desired)
        ensure_directory(target.parent, dry_run)
        if dry_run:
            actions.append(f"render-port-contract: {target} ({service_count} service(s))")
            continue
        if state == "ok":
            actions.append(f"port-contract-unchanged: {target}")
            continue
        atomic_write_text(target, desired)
        actions.append(f"render-port-contract: {target} ({service_count} service(s))")
    return actions


def _port_contract_gitignored(repo_path: Path) -> bool:
    gitignore = repo_path / ".gitignore"
    try:
        lines = gitignore.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(line.strip().rstrip("/") == PORT_CONTRACT_FILE_NAME for line in lines)


def _port_contract_adoption_files(repo_path: Path) -> list[Path]:
    candidates = [repo_path / name for name in _PORT_CONTRACT_ADOPTION_FILES]
    candidates.extend(sorted(repo_path.glob("*.config.*")))
    seen: set[Path] = set()
    existing: list[Path] = []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            existing.append(path)
    return existing


def _repo_mentions_port_contract(repo_path: Path) -> bool:
    for path in _port_contract_adoption_files(repo_path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if PORT_CONTRACT_FILE_NAME in text or "SKILLBOX_SERVICE_ID" in text:
            return True
        if "strictPort" in text and ("process.env.PORT" in text or "$PORT" in text):
            return True
        if "--strictPort" in text and ("$PORT" in text or "PORT" in text):
            return True
    return False


def _port_contract_advisory_suppressed(service: dict[str, Any]) -> bool:
    config = service.get("port_contract")
    if not isinstance(config, dict):
        return False
    if bool(config.get("adopted") or config.get("suppress_advisory") or config.get("suppress_unadopted")):
        return True
    adoption = str(config.get("adoption") or "").strip().lower()
    return adoption in {"manual", "external", "suppressed"}


def validate_port_contracts(model: dict[str, Any]) -> list[CheckResult]:
    groups = _port_contract_groups(model)
    if not groups:
        return []

    results: list[CheckResult] = []
    checked_count = 0
    for repo_path, grouped_records in groups:
        if not repo_path.is_dir():
            continue
        checked_count += len(grouped_records)
        target = repo_path / PORT_CONTRACT_FILE_NAME
        service_ids = [str(record["service_id"]) for record in grouped_records]
        desired = _render_port_contract_file(grouped_records)
        group_details = {
            "service_ids": service_ids,
            "repo_id": grouped_records[0]["repo_id"],
            "repo_path": str(repo_path),
            "contract_path": str(target),
            "adoption_snippet": _PORT_CONTRACT_ADOPTION_SNIPPET,
        }
        if target.is_file():
            state = managed_text_artifact_state(target, desired)
            if state != "ok":
                results.append(
                    CheckResult(
                        status="warn",
                        code=PORT_CONTRACT_STALE,
                        message=(
                            f"{PORT_CONTRACT_FILE_NAME} in repo {grouped_records[0]['repo_id']} "
                            "does not match the current port registry"
                        ),
                        details={**group_details, "state": state},
                    )
                )
        if target.is_file() and not _port_contract_gitignored(repo_path):
            results.append(
                CheckResult(
                    status="warn",
                    code=PORT_CONTRACT_GITIGNORE,
                    message=(
                        f"{PORT_CONTRACT_FILE_NAME} is generated under repo {grouped_records[0]['repo_id']} "
                        "but is not covered by .gitignore"
                    ),
                    details={**group_details, "exact_line": PORT_CONTRACT_FILE_NAME},
                )
            )
        repo_adopted = _repo_mentions_port_contract(repo_path)
        for record in grouped_records:
            service_id = str(record["service_id"])
            if _port_contract_advisory_suppressed(record["service"]) or repo_adopted:
                continue
            base_details = {
                "service_id": service_id,
                "repo_id": record["repo_id"],
                "repo_path": str(repo_path),
                "contract_path": str(target),
                "adoption_snippet": _PORT_CONTRACT_ADOPTION_SNIPPET,
            }
            results.append(
                CheckResult(
                    status="warn",
                    code=PORT_CONTRACT_UNADOPTED,
                    message=(
                        f"service {service_id} has a generated port contract, but repo "
                        f"{record['repo_id']} does not appear to load it with strict port settings"
                    ),
                    details={
                        **base_details,
                        "suppress_with": "services[].port_contract.suppress_advisory: true",
                    },
                )
            )

    if not results and checked_count:
        results.append(
            CheckResult(
                status="pass",
                code="port-contracts",
                message=f"{checked_count} generated port contract(s) are adopted or suppressed",
                details={"count": checked_count},
            )
        )
    return results


def validate_port_registry(model: dict[str, Any]) -> list[CheckResult]:
    """Port-guard doctor checks built on the scope-aware port registry view.

    Emits ``PORT_COLLISION`` (hard, names both declarations),
    ``PORT_WILDCARD_BIND`` (hard under tailnet_only), and
    ``PORT_UNDECLARED_RESERVED`` (hard, only when reserved ranges are
    declared). Cross-client overlaps are downgraded to a non-fatal advisory
    (``PORT_CROSS_CLIENT_OVERLAP``) because client overlays are mutually
    exclusive at load time. A clean active scope yields a single PASS plus any
    extraction warnings surfaced as warn entries.
    """
    entries = build_port_registry(model)
    results: list[CheckResult] = []
    results.extend(_collision_results(entries))
    results.extend(_wildcard_results(entries, _resolved_network_posture(model)))
    results.extend(_reserved_results(entries, model))

    for entry in entries:
        if entry.get("warning"):
            results.append(
                CheckResult(
                    status="warn",
                    code=PORT_REGISTRY_WARNING,
                    message=entry["warning"],
                    details={"owner_id": entry["owner_id"], "source": entry.get("source")},
                )
            )

    if not any(r.status in {"fail", "warn"} for r in results):
        declared = sum(1 for e in entries if e.get("port") is not None)
        results.append(
            CheckResult(
                status="pass",
                code="port-registry",
                message=f"{declared} declared port(s) in active scope have no collisions",
                details={"count": declared},
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
        + validate_service_exposure(model)
        + validate_port_registry(model)
        + validate_port_contracts(model)
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
        return _sync_url_artifact(artifact, source, state, path, dry_run)
    if sync_mode == "copy-if-missing" and source_kind == "file":
        return _sync_file_artifact(source, state, path, dry_run)
    return [f"skip: {path} (sync mode {sync_mode})"]


def _artifact_action_name(state: dict[str, Any], stale_action: str, missing_action: str) -> str:
    return stale_action if state["state"] == "stale" else missing_action


def _replace_artifact_payload(path: Path, payload: bytes, executable: bool) -> None:
    tmp_path = path.parent / f".{path.name}.tmp"
    tmp_path.write_bytes(payload)
    if executable:
        tmp_path.chmod(0o755)
    tmp_path.replace(path)


def _sync_url_artifact(
    artifact: dict[str, Any],
    source: dict[str, Any],
    state: dict[str, Any],
    path: Path,
    dry_run: bool,
) -> list[str]:
    if not str(source.get("url") or "").strip():
        return [f"skip: {path} (artifact source url missing)"]
    url, expected_sha256 = validate_url_download_source(source, artifact_id=str(artifact["id"]))
    ensure_directory(path.parent, dry_run)
    action_name = _artifact_action_name(state, "download-reconcile", "download-if-missing")
    if dry_run:
        return [f"{action_name}: {url} -> {path}"]
    _download_artifact_to_path(artifact, source, path, url, expected_sha256)
    return [f"{action_name}: {url} -> {path}"]


def _download_artifact_to_path(
    artifact: dict[str, Any],
    source: dict[str, Any],
    path: Path,
    url: str,
    expected_sha256: str,
) -> None:
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = response.read()
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise NetworkError(
            "runtime_error",
            f"artifact {artifact['id']} digest mismatch for {url}: "
            f"expected {expected_sha256}, got {actual_sha256}",
            context={
                "artifact": artifact["id"],
                "url": url,
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
            },
        )
    payload = _downloaded_artifact_payload(artifact, source, payload)
    _replace_artifact_payload(path, payload, bool(source.get("executable", False)))


def _downloaded_artifact_payload(
    artifact: dict[str, Any],
    source: dict[str, Any],
    payload: bytes,
) -> bytes:
    archive_kind = str(source.get("archive") or "").strip().lower()
    if not archive_kind:
        return payload
    if archive_kind not in {"tar.gz", "tgz"}:
        raise AdapterError(
            "runtime_error",
            f"artifact {artifact['id']} has unsupported source.archive {archive_kind!r}",
            context={"artifact": artifact["id"], "archive": archive_kind},
        )

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        if len(members) != 1:
            raise AdapterError(
                "runtime_error",
                f"artifact {artifact['id']} archive must contain exactly one regular file; found {len(members)}",
                context={"artifact": artifact["id"], "member_count": len(members)},
            )
        member = members[0]
        member_path = PurePosixPath(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise AdapterError(
                "runtime_error",
                f"artifact {artifact['id']} archive member has unsafe path {member.name!r}",
                context={"artifact": artifact["id"], "member": member.name},
            )
        extracted = archive.extractfile(member)
        if extracted is None:
            raise AdapterError(
                "runtime_error",
                f"artifact {artifact['id']} archive member {member.name!r} could not be read",
                context={"artifact": artifact["id"], "member": member.name},
            )
        return extracted.read()


def _sync_file_artifact(
    source: dict[str, Any],
    state: dict[str, Any],
    path: Path,
    dry_run: bool,
) -> list[str]:
    raw_source_path = str(source.get("host_path") or source.get("path") or "").strip()
    if not raw_source_path:
        return [f"skip: {path} (artifact source path missing)"]
    source_path = Path(raw_source_path)
    ensure_directory(path.parent, dry_run)
    action_name = _artifact_action_name(state, "copy-reconcile", "copy-if-missing")
    if dry_run:
        return [f"{action_name}: {source_path} -> {path}"]
    _copy_artifact_to_path(source, source_path, path)
    return [f"{action_name}: {source_path} -> {path}"]


def _copy_artifact_to_path(source: dict[str, Any], source_path: Path, path: Path) -> None:
    if not source_path.is_file():
        raise AdapterError(
            "runtime_error",
            f"artifact source file is missing: {source_path}",
            context={"source_path": str(source_path)},
        )
    tmp_path = path.parent / f".{path.name}.tmp"
    shutil.copyfile(source_path, tmp_path)
    if source.get("executable", False):
        tmp_path.chmod(0o755)
    tmp_path.replace(path)


def sync_env_file(env_file: dict[str, Any], dry_run: bool) -> list[str]:
    state = env_file_state(env_file)
    path = Path(state["host_path"])
    source_path = Path(state["source_host_path"]) if state["source_host_path"] else None

    if state["source_kind"] == "file" and state["sync_mode"] == "write":
        return _sync_written_env_file(env_file, state, path, source_path, dry_run)
    return _sync_existing_or_manual_env_file(env_file, state, path)


def _sync_written_env_file(
    env_file: dict[str, Any],
    state: dict[str, Any],
    path: Path,
    source_path: Path | None,
    dry_run: bool,
) -> list[str]:
    if source_path is None or not source_path.is_file():
        return _missing_env_source_action(env_file, state, path)

    ensure_directory(path.parent, dry_run)
    if dry_run:
        return [f"hydrate-env: {source_path} -> {path}"]
    return _write_env_payload_if_changed(env_file, path, source_path)


def _missing_env_source_action(
    env_file: dict[str, Any],
    state: dict[str, Any],
    path: Path,
) -> list[str]:
    if env_file.get("required"):
        raise ValidationError(
            "missing_env_file",
            f"Required env file {env_file['id']} is missing source {state['source_path'] or state['source_host_path'] or path}.",
            context={"env_file": env_file["id"]},
        )
    return [f"skip: {path} (env source path missing)"]


def _write_env_payload_if_changed(
    env_file: dict[str, Any],
    path: Path,
    source_path: Path,
) -> list[str]:
    payload = source_path.read_bytes()
    current_payload = path.read_bytes() if path.is_file() else None
    desired_mode = normalize_file_mode(env_file.get("mode"), default=0o600)
    current_mode = path.stat().st_mode & 0o777 if path.is_file() else None
    if current_payload == payload and current_mode == desired_mode:
        return [f"env-unchanged: {path}"]
    path.write_bytes(payload)
    path.chmod(desired_mode)
    return [f"hydrate-env: {source_path} -> {path}"]


def _sync_existing_or_manual_env_file(
    env_file: dict[str, Any],
    state: dict[str, Any],
    path: Path,
) -> list[str]:
    if path.exists():
        return [f"exists: {path}"]
    if env_file.get("required"):
        raise ValidationError(
            "missing_env_file",
            f"Required env file {env_file['id']} is missing at {path}.",
            context={"env_file": env_file["id"], "path": str(path)},
        )
    return [f"skip: {path} (sync mode {state['sync_mode']})"]


def sync_dcg_config(model: dict[str, Any], root_dir: Path, dry_run: bool) -> list[str]:
    """Render .dcg.toml from env and client overlay dcg settings."""
    env = model.get("env") or {}
    dcg_bin = env.get("SKILLBOX_DCG_BIN", "").strip()
    if not dcg_bin:
        return [f"skip: .dcg.toml (dcg not configured)"]

    packs, allowlist = _dcg_packs_and_allowlist(model, env)
    content = _dcg_config_content(packs, allowlist)
    dcg_config_path = root_dir / ".dcg.toml"
    if _dcg_config_current(dcg_config_path, content):
        return [f"exists: {dcg_config_path}"]

    action = f"render-dcg-config: {dcg_config_path} (packs: {', '.join(packs)})"
    if dry_run:
        return [action]

    atomic_write_text(dcg_config_path, content)
    return [action]


def _dcg_packs_and_allowlist(model: dict[str, Any], env: dict[str, Any]) -> tuple[list[str], list[Any]]:
    packs_raw = env.get("SKILLBOX_DCG_PACKS", "core.git,core.filesystem").strip()
    packs = [pack.strip() for pack in packs_raw.split(",") if pack.strip()]
    allowlist: list[Any] = []
    for client in model.get("clients") or []:
        client_dcg = client.get("dcg")
        if not isinstance(client_dcg, dict):
            continue
        for pack in client_dcg.get("packs") or []:
            if pack not in packs:
                packs.append(pack)
        # Union allowlist rules across clients too. Returning only the last
        # client's allowlist would silently drop earlier clients' command rules
        # from the generated .dcg.toml even though their packs were merged.
        for rule in client_dcg.get("allowlist") or []:
            if rule not in allowlist:
                allowlist.append(rule)
    return packs, allowlist


def _dcg_config_content(packs: list[str], allowlist: list[Any]) -> str:
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
    return "\n".join(lines) + "\n"


def _dcg_config_current(dcg_config_path: Path, content: str) -> bool:
    return dcg_config_path.exists() and dcg_config_path.read_text() == content


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


def _sync_distributor_sources(model: dict[str, Any], dry_run: bool) -> list[str]:
    from .distribution.sync import sync_distributor_sources
    return sync_distributor_sources(model, dry_run=dry_run)


def _runtime_status_distributors(model: dict[str, Any]) -> list[dict[str, Any]]:
    from .distribution.status import collect_distributor_status
    return collect_distributor_status(model)


def _repo_source_kind(source: dict[str, Any]) -> str:
    return str(source.get("kind", "manual"))


def _repo_sync_mode(repo: dict[str, Any], source_kind: str) -> str:
    sync = repo.get("sync") or {}
    return str(sync.get("mode") or ("ensure-directory" if source_kind == "directory" else "external"))


def _git_clone_args(url: str, branch: str, path: Path) -> list[str]:
    args = ["git", "clone"]
    if branch:
        args.extend(["--branch", branch])
    args.extend([url, str(path)])
    return args


def _run_git_clone(url: str, branch: str, path: Path) -> None:
    result = run_command(_git_clone_args(url, branch, path))
    if result.returncode != 0:
        raise AdapterError(
            "runtime_error",
            result.stderr.strip() or result.stdout.strip() or f"git clone failed for {url}",
            context={"url": url, "returncode": result.returncode},
        )


def _sync_existing_git_repo(
    model: dict[str, Any],
    repo: dict[str, Any],
    path: Path,
    url: str,
    branch: str,
    dry_run: bool,
) -> str:
    if path.is_dir() and git_repo_state(path).get("git"):
        return f"exists: {path}"
    if not repo_has_only_regenerable_git_residue(model, repo):
        return f"exists: {path}"
    if dry_run:
        return f"clone-reconcile: {url} -> {path}"
    clear_repo_git_residue(path)
    _run_git_clone(url, branch, path)
    return f"clone-reconcile: {url} -> {path}"


def _sync_git_repo(
    model: dict[str, Any],
    repo: dict[str, Any],
    path: Path,
    source: dict[str, Any],
    dry_run: bool,
) -> list[str]:
    url = str(source["url"])
    branch = str(source.get("branch", "")).strip()
    if path.exists():
        return [_sync_existing_git_repo(model, repo, path, url, branch, dry_run)]

    ensure_directory(path.parent, dry_run)
    if not dry_run:
        _run_git_clone(url, branch, path)
    return [f"clone-if-missing: {url} -> {path}"]


def _sync_repo(model: dict[str, Any], repo: dict[str, Any], dry_run: bool) -> list[str]:
    path = Path(str(repo["host_path"]))
    source = repo.get("source") or {}
    source_kind = _repo_source_kind(source)
    sync_mode = _repo_sync_mode(repo, source_kind)
    if source_kind == "git" and sync_mode == "clone-if-missing":
        return _sync_git_repo(model, repo, path, source, dry_run)
    if path.exists():
        return [f"exists: {path}"]
    if sync_mode == "ensure-directory" or source_kind == "directory":
        ensure_directory(path, dry_run)
        return [f"ensure-directory: {path}"]
    return [f"skip: {path} (sync mode {sync_mode})"]


def _sync_repos(model: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []
    for repo in model["repos"]:
        actions.extend(_sync_repo(model, repo, dry_run))
    return actions


def _sync_log_dirs(model: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []
    for log_item in model["logs"]:
        path = Path(str(log_item["host_path"]))
        if path.exists():
            actions.append(f"exists: {path}")
            continue
        ensure_directory(path, dry_run)
        actions.append(f"ensure-directory: {path}")
    return actions


def sync_runtime(model: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []
    actions.extend(_sync_repos(model, dry_run))
    for artifact in model["artifacts"]:
        actions.extend(sync_artifact(artifact, dry_run=dry_run))

    for env_file in model["env_files"]:
        actions.extend(sync_env_file(env_file, dry_run=dry_run))

    actions.extend(sync_port_contracts(model, dry_run=dry_run))
    actions.extend(_sync_log_dirs(model, dry_run))
    actions.extend(sync_skill_repo_sets(model, dry_run=dry_run))
    actions.extend(_sync_distributor_sources(model, dry_run=dry_run))
    actions.extend(sync_skill_sets(model, dry_run=dry_run))
    actions.extend(sync_dcg_config(model, Path(str(model["root_dir"])), dry_run=dry_run))
    actions.extend(sync_ingress_artifacts(model, dry_run=dry_run))
    if not dry_run:
        _log_model_runtime_event(model, "sync.completed", "runtime", {"action_count": len(actions)})
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
    return unique_string_field_values(task, "depends_on")


def service_bootstrap_task_ids(service: dict[str, Any]) -> list[str]:
    return unique_string_field_values(service, "bootstrap_tasks")


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
            raise StateConflictError(
                "runtime_error",
                f"Task dependency cycle detected at {task_id}.",
                context={"task_id": task_id},
            )
        if task_id not in tasks_by_id:
            raise ValidationError(
                "runtime_error",
                f"Task dependency references unknown task {task_id!r}.",
                context={"task_id": task_id},
            )

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
    if not _service_has_lifecycle_command(service):
        return False, "command missing"
    if _service_is_idle_ingress(service, model):
        return False, "no ingress routes active"
    if _service_is_orchestration(service):
        return False, "orchestration services are status-only"
    artifact_reason = _optional_service_artifact_unavailable_reason(service, model)
    if artifact_reason is not None:
        return False, artifact_reason
    return True, None


def _service_has_lifecycle_command(service: dict[str, Any]) -> bool:
    has_command = bool(str(service.get("command") or "").strip())
    raw_commands = service.get("commands")
    has_mode_commands = isinstance(raw_commands, dict) and any(
        str(v).strip() for v in raw_commands.values()
    )
    return has_command or has_mode_commands


def _service_is_idle_ingress(service: dict[str, Any], model: dict[str, Any] | None) -> bool:
    return (
        str(service.get("kind") or "").strip() == "ingress"
        and model is not None
        and not model.get("ingress_routes")
    )


def _service_is_orchestration(service: dict[str, Any]) -> bool:
    return str(service.get("kind") or "").strip() == "orchestration"


def _optional_service_artifact_unavailable_reason(
    service: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    artifact_id = str(service.get("artifact") or "").strip()
    if not artifact_id or model is None or service.get("required", True):
        return None
    for artifact in model.get("artifacts") or []:
        if str(artifact.get("id", "")).strip() != artifact_id:
            continue
        artifact_path = str(artifact.get("host_path") or artifact.get("path") or "").strip()
        if artifact_path and not Path(artifact_path).exists():
            return f"optional artifact {artifact_id!r} not configured"
        break
    return None


def service_dependency_ids(service: dict[str, Any]) -> list[str]:
    return unique_string_field_values(service, "depends_on")


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
            raise StateConflictError(
                "runtime_error",
                f"Service dependency cycle detected at {service_id}.",
                context={"service_id": service_id},
            )
        if service_id not in services_by_id:
            if service_id in artifact_ids:
                return
            raise ValidationError(
                "runtime_error",
                f"Service dependency references unknown service {service_id!r}.",
                context={"service_id": service_id},
            )

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
    try:
        storage = compile_persistence_summary(root_dir, runtime_env)
    except RuntimeError:
        storage = None

    def _already_host_path(raw_value: str) -> bool:
        raw_text = str(raw_value).strip()
        if not raw_text:
            return False
        path = Path(raw_text).expanduser()
        if not path.is_absolute():
            return False
        return not is_runtime_absolute_path(raw_text)

    translated: dict[str, str] = {}
    for key, value in runtime_env.items():
        if key in {"SKILLBOX_MONOSERVER_HOST_ROOT", "SKILLBOX_CLIENTS_HOST_ROOT"}:
            translated[key] = str(host_path_to_absolute_path(root_dir, value))
            continue
        if key in PATH_LIKE_ENV_KEYS and value:
            if _already_host_path(value):
                translated[key] = str(Path(value).expanduser())
            else:
                translated[key] = str(runtime_path_to_host_path(root_dir, runtime_env, value, storage=storage))
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


def _process_is_zombie(pid: int) -> bool:
    # A zombie (defunct) process has already exited; it holds no resources,
    # runs no code, and listens on no ports, but it lingers in the process
    # table until its parent reaps it. os.kill(pid, 0) still succeeds for a
    # zombie, so callers that only probe with signal 0 would wrongly treat a
    # dead-but-unreaped child as live. On Linux the kernel exposes the process
    # state as the first token after the (parenthesised) comm field in
    # /proc/<pid>/stat; "Z" means zombie. Platforms without /proc fall through
    # to the signal-0 result.
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # comm may contain spaces/parens, so split on the last ')' to isolate the
    # state field that immediately follows it.
    _, _, after = raw.rpartition(")")
    state_field = after.split(None, 1)
    return bool(state_field) and state_field[0] == "Z"


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return not _process_is_zombie(pid)


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
    item_env = item.get("env") or {}
    if isinstance(item_env, dict):
        for key, value in item_env.items():
            env[str(key)] = translate_runtime_paths(str(value), runtime_env, translated_env)
    env["SKILLBOX_MANAGED_RUN"] = "1"
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
    "LOCAL_RUNTIME_PORT_MISMATCH",
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


def _service_declared_listen_port(service: dict[str, Any]) -> tuple[str, int] | None:
    healthcheck = service.get("healthcheck") or {}
    hc_type = healthcheck.get("type")
    if hc_type == "port":
        try:
            port = int(healthcheck["port"])
        except (KeyError, TypeError, ValueError):
            return None
        host = str(healthcheck.get("host") or "127.0.0.1")
        return host, port
    if hc_type in {"http", "https"}:
        url = str(healthcheck.get("url") or "")
        if not url:
            return None
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return None
        host = parsed.hostname or "127.0.0.1"
        if parsed.port is not None:
            return host, int(parsed.port)
        if parsed.scheme == "https":
            return host, 443
        if parsed.scheme == "http":
            return host, 80
        return None
    return None


def _external_listener_owner(host: str, port: int) -> dict[str, Any] | None:
    if shutil.which("lsof") is None:
        return None
    result = run_command(["lsof", "-nP", f"-iTCP@{host}:{port}", "-sTCP:LISTEN", "-Fpcn"])
    if result.returncode != 0 or not result.stdout:
        return None
    owner: dict[str, Any] = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        tag, _, value = line[:1], line[1:2], line[1:]
        if tag == "p":
            try:
                owner["pid"] = int(value)
            except ValueError:
                continue
        elif tag == "c":
            owner["command"] = value
        if "pid" in owner and "command" in owner:
            break
    if "pid" not in owner:
        return None
    try:
        ps_result = run_command(["ps", "-o", "etime=,command=", "-p", str(owner["pid"])])
        if ps_result.returncode == 0:
            head = ps_result.stdout.strip().split(None, 1)
            if head:
                owner["age"] = head[0]
                if len(head) > 1 and "command" not in owner:
                    owner["command"] = head[1]
    except OSError:
        pass
    return owner


def _declared_ports_for_service(model: dict[str, Any], service: dict[str, Any]) -> list[int]:
    service_id = str(service.get("id") or "").strip()
    if not service_id:
        return []
    try:
        return declared_service_ports(model, service_id)
    except Exception:
        declared = _service_declared_listen_port(service)
        return [declared[1]] if declared is not None else []


def _parse_proc_stat_ppid(stat_text: str) -> int | None:
    _before, _sep, after = stat_text.rpartition(")")
    fields = after.split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _process_tree_pids(root_pid: int) -> set[int]:
    pids: set[int] = {int(root_pid)}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return pids

    changed = True
    while changed:
        changed = False
        for child in proc_root.iterdir():
            if not child.name.isdigit():
                continue
            try:
                pid = int(child.name)
            except ValueError:
                continue
            if pid in pids:
                continue
            try:
                ppid = _parse_proc_stat_ppid((child / "stat").read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if ppid in pids:
                pids.add(pid)
                changed = True
    return pids


def _all_proc_pids() -> set[int]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return set()
    pids: set[int] = set()
    for child in proc_root.iterdir():
        if not child.name.isdigit():
            continue
        try:
            pids.add(int(child.name))
        except ValueError:
            continue
    return pids


def process_tree_pids(root_pid: int) -> set[int]:
    """Return the root process and descendants visible through /proc."""
    return _process_tree_pids(root_pid)


def _parse_listener_port(raw_local_address: str) -> int | None:
    value = str(raw_local_address or "").strip()
    if not value:
        return None
    if value.startswith("[") and "]:" in value:
        port_text = value.rsplit("]:", 1)[1]
    elif ":" in value:
        port_text = value.rsplit(":", 1)[1]
    else:
        return None
    port_text = port_text.strip()
    if port_text == "*":
        return None
    try:
        return int(port_text)
    except ValueError:
        return None


def _parse_ss_listener_rows(stdout: str, target_pids: set[int]) -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 5)
        if len(parts) < 4:
            continue
        if parts[0].upper() != "LISTEN":
            continue
        port = _parse_listener_port(parts[3])
        if port is None:
            continue
        pids = {
            int(match.group(1))
            for match in re.finditer(r"pid=(\d+)", parts[5] if len(parts) > 5 else "")
        }
        for pid in sorted(pids & target_pids):
            listeners.append({"pid": pid, "port": port, "source": "ss"})
    return listeners


def _ss_process_tree_listeners(target_pids: set[int]) -> list[dict[str, Any]]:
    if shutil.which("ss") is None:
        return []
    result = run_command(["ss", "-H", "-tlnp"])
    if result.returncode != 0:
        return []
    return _parse_ss_listener_rows(result.stdout, target_pids)


def _proc_net_tcp_listen_inodes(path: Path) -> dict[str, int]:
    inodes: dict[str, int] = {}
    try:
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return inodes
    for raw_line in rows[1:]:
        parts = raw_line.split()
        if len(parts) < 10 or parts[3] != "0A":
            continue
        _host_hex, _sep, port_hex = parts[1].partition(":")
        if not port_hex:
            continue
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        inode = parts[9]
        if inode:
            inodes[inode] = port
    return inodes


def _proc_socket_inode(fd_path: Path) -> str | None:
    try:
        target = os.readlink(fd_path)
    except OSError:
        return None
    match = re.fullmatch(r"socket:\[(\d+)\]", target)
    return match.group(1) if match else None


def _proc_process_tree_listeners(target_pids: set[int]) -> list[dict[str, Any]]:
    inode_to_port: dict[str, int] = {}
    inode_to_port.update(_proc_net_tcp_listen_inodes(Path("/proc/net/tcp")))
    inode_to_port.update(_proc_net_tcp_listen_inodes(Path("/proc/net/tcp6")))
    if not inode_to_port:
        return []

    listeners: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for pid in sorted(target_pids):
        fd_dir = Path("/proc") / str(pid) / "fd"
        try:
            fd_paths = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd_path in fd_paths:
            inode = _proc_socket_inode(fd_path)
            if inode is None or inode not in inode_to_port:
                continue
            port = inode_to_port[inode]
            key = (pid, port)
            if key in seen:
                continue
            seen.add(key)
            listeners.append({"pid": pid, "port": port, "source": "proc"})
    return listeners


def _process_tree_listener_snapshot(pid: int) -> dict[str, Any]:
    pids = _process_tree_pids(pid)
    listeners = _ss_process_tree_listeners(pids)
    source = "ss"
    if not listeners:
        listeners = _proc_process_tree_listeners(pids)
        source = "proc"
    observed_ports = sorted({int(listener["port"]) for listener in listeners if listener.get("port") is not None})
    return {
        "pid": pid,
        "pids": sorted(pids),
        "listeners": listeners,
        "observed_ports": observed_ports,
        "source": source,
    }


def process_tree_listener_snapshot(pid: int) -> dict[str, Any]:
    """Return listening ports owned by a process tree."""
    return _process_tree_listener_snapshot(pid)


def all_process_listeners() -> list[dict[str, Any]]:
    """Return visible listening sockets keyed by owning process pid."""
    pids = _all_proc_pids()
    if not pids:
        return []
    listeners = _proc_process_tree_listeners(pids)
    if not listeners:
        listeners = _ss_process_tree_listeners(pids)
    return sorted(
        listeners,
        key=lambda item: (int(item.get("port") or 0), int(item.get("pid") or 0)),
    )


def _service_port_guard_disabled() -> bool:
    return str(os.environ.get("SKILLBOX_PORT_GUARD") or "").strip().lower() == "off"


def _port_guard_telemetry_path(root_dir: Path) -> Path:
    return Path(root_dir) / "logs" / "runtime" / PORT_GUARD_TELEMETRY_NAME


def _record_port_guard_telemetry(root_dir: Path, counter: str) -> None:
    if counter not in {"post_bind_mismatches"}:
        return
    path = _port_guard_telemetry_path(root_dir)

    def mutate(current: Any) -> dict[str, Any]:
        payload = current if isinstance(current, dict) else {}
        counters = payload.get("counters") if isinstance(payload.get("counters"), dict) else {}
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        counters[counter] = int(counters.get(counter) or 0) + 1
        counters.setdefault("first_seen_at", stamp)
        counters["last_seen_at"] = stamp
        payload["counters"] = counters
        return payload

    try:
        locked_json_update(path, mutate)
    except (StateLockTimeout, OSError):
        pass


def _verify_service_declared_ports(
    model: dict[str, Any],
    service: dict[str, Any],
    pid: int | None,
    *,
    attempts: int = 3,
    sleep_seconds: float = 1.0,
) -> dict[str, Any]:
    declared_ports = _declared_ports_for_service(model, service)
    if not declared_ports:
        return {"state": "not-declared", "declared_ports": []}
    if _service_port_guard_disabled():
        return {"state": "skipped", "reason": "SKILLBOX_PORT_GUARD=off", "declared_ports": declared_ports}
    if pid is None:
        return {
            "state": "unknown",
            "reason": "pid unavailable",
            "declared_ports": declared_ports,
            "observed_ports": [],
        }

    attempts = max(1, int(attempts))
    declared = set(declared_ports)
    snapshot: dict[str, Any] = {
        "pid": pid,
        "pids": [pid],
        "listeners": [],
        "observed_ports": [],
        "source": "unknown",
    }
    for attempt in range(1, attempts + 1):
        snapshot = _process_tree_listener_snapshot(pid)
        observed_ports = sorted({int(port) for port in snapshot.get("observed_ports") or []})
        matches = sorted(declared & set(observed_ports))
        if matches:
            return {
                "state": "ok",
                "declared_ports": declared_ports,
                "observed_ports": observed_ports,
                "verified_port": matches[0],
                "verified_ports": matches,
                "pid": pid,
                "process_pids": snapshot.get("pids") or [pid],
                "listeners": snapshot.get("listeners") or [],
                "source": snapshot.get("source"),
                "attempts": attempt,
            }
        if attempt < attempts:
            time.sleep(sleep_seconds)

    return {
        "state": "mismatch",
        "declared_ports": declared_ports,
        "observed_ports": sorted({int(port) for port in snapshot.get("observed_ports") or []}),
        "pid": pid,
        "process_pids": snapshot.get("pids") or [pid],
        "listeners": snapshot.get("listeners") or [],
        "source": snapshot.get("source"),
        "attempts": attempts,
    }


def _port_mismatch_next_actions(service: dict[str, Any]) -> list[str]:
    sid = str(service.get("id") or "<service>")
    return [
        f"free the declared port and re-run `sbp up {sid}`",
        "fix the service command/env so it binds the declared registry port",
        "run `manage.py ports --resolve " + sid + " --format json` to inspect the contract",
    ]


def _port_mismatch_error(service: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    sid = str(service.get("id") or "<service>")
    declared = verification.get("declared_ports") or []
    observed = verification.get("observed_ports") or []
    next_actions = _port_mismatch_next_actions(service)
    err = local_runtime_error(
        LOCAL_RUNTIME_PORT_MISMATCH,
        (
            f"Service {sid!r} did not listen on its declared port(s) "
            f"{declared}; observed process-tree port(s): {observed or 'none'}."
        ),
        recoverable=True,
        blocked_services=[sid],
        next_action=next_actions[0],
    )
    err["error"].update({
        "service": sid,
        "declared_ports": declared,
        "observed_ports": observed,
        "process_pids": verification.get("process_pids") or [],
        "listeners": verification.get("listeners") or [],
        "next_actions": next_actions,
    })
    return err


def _copy_port_verification_fields(detail: dict[str, Any], verification: dict[str, Any]) -> None:
    if verification.get("state") in {"not-declared", "skipped"}:
        return
    detail["port_verification"] = {
        key: verification[key]
        for key in (
            "state",
            "declared_ports",
            "observed_ports",
            "verified_port",
            "verified_ports",
            "process_pids",
            "source",
            "attempts",
            "reason",
        )
        if key in verification
    }
    if verification.get("verified_port") is not None:
        detail["verified_port"] = verification["verified_port"]
    if verification.get("verified_ports"):
        detail["verified_ports"] = verification["verified_ports"]


def _unverified_reuse_mismatch(
    model: dict[str, Any],
    service: dict[str, Any],
    pid: int | None,
    reason: str,
) -> dict[str, Any] | None:
    declared_ports = _declared_ports_for_service(model, service)
    if not declared_ports or _service_port_guard_disabled():
        return None
    verification: dict[str, Any] = {
        "state": "mismatch",
        "reason": reason,
        "declared_ports": declared_ports,
        "observed_ports": [],
        "process_pids": [pid] if pid is not None else [],
        "listeners": [],
        "source": "unverified-reuse",
        "attempts": 0,
    }
    if pid is not None:
        verification["pid"] = pid
    return verification


def first_service_error_payload(service_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in service_results:
        error_block = entry.get("error")
        if not isinstance(error_block, dict):
            continue
        error_type = str(error_block.get("type") or "").strip()
        if error_type.startswith("LOCAL_RUNTIME_"):
            return {"error": copy.deepcopy(error_block)}
    return None


def _service_port_mismatch_result(
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    pid: int | None,
    verification: dict[str, Any],
    event_root: Path,
    *,
    remove_pid: bool,
) -> dict[str, Any]:
    stop_result: str | None = None
    signal_used: int | None = None
    if pid is not None:
        stop_result, signal_used = stop_process(pid, DEFAULT_SERVICE_STOP_WAIT_SECONDS)
    if remove_pid:
        remove_pid_file(paths["pid_file"])

    detail = result | {
        "result": "failed",
        "reason": "port_mismatch",
        "tail": tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES),
    }
    if pid is not None:
        detail["pid"] = pid
    if stop_result is not None:
        detail["stop_result"] = stop_result
    if signal_used is not None:
        detail["signal"] = signal_used
    _copy_port_verification_fields(detail, verification)
    detail.update(_port_mismatch_error(service, verification))
    log_runtime_event(
        "service.port_mismatch",
        service["id"],
        {
            "declared": verification.get("declared_ports") or [],
            "observed": verification.get("observed_ports") or [],
            "action": "stopped" if pid is not None else "blocked",
            "stop_result": stop_result,
        },
        root_dir=event_root,
    )
    _record_port_guard_telemetry(event_root, "post_bind_mismatches")
    return detail


def _port_conflict_blocked_result(
    service: dict[str, Any],
    result: dict[str, Any],
    host: str,
    port: int,
    owner: dict[str, Any] | None,
    event_root: Path,
) -> dict[str, Any]:
    detail = result | {
        "result": "blocked",
        "reason": "port_already_in_use",
        "host": host,
        "port": port,
    }
    if owner:
        detail["external_pid"] = owner.get("pid")
        if "command" in owner:
            detail["external_command"] = owner["command"]
        if "age" in owner:
            detail["external_age"] = owner["age"]
        hint_target = f"PID {owner['pid']}"
    else:
        hint_target = f"the process holding {host}:{port}"
    detail["actionable"] = (
        f"{host}:{port} is held by {hint_target}; free it (e.g. `kill {owner['pid']}`) "
        if owner
        else f"{host}:{port} is held by an unknown process; identify and stop it (try `lsof -nP -iTCP:{port} -sTCP:LISTEN`) "
    ) + f"before re-running `sbp up {service.get('id', '<service>')}`."
    log_runtime_event(
        "service.start_blocked",
        service["id"],
        {"reason": "port_already_in_use", "port": port, "external_pid": owner.get("pid") if owner else None},
        root_dir=event_root,
    )
    return detail


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

    def _absolute_without_resolve(path: Path) -> Path:
        expanded = path.expanduser()
        if expanded.is_absolute():
            return expanded
        return Path(os.path.abspath(str(expanded)))

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
                    _absolute_without_resolve(target_path).relative_to(
                        _absolute_without_resolve(repo_host)
                    )
                except ValueError:
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


def _local_runtime_available_profiles(model: dict[str, Any]) -> list[str]:
    return sorted({
        str(p).strip()
        for service in model.get("services") or []
        for p in service.get("profiles") or []
        if str(p).strip().startswith("local-")
    })


def _local_runtime_reconcile_result(profile: str) -> tuple[dict[str, Any], list[str]]:
    actions: list[str] = []
    return {
        "status": "ready",
        "profile": profile,
        "bridges": [],
        "env_files": [],
        "actions": actions,
    }, actions


def _block_local_runtime_result(
    result: dict[str, Any],
    error_type: str,
    detail: str,
    *,
    recoverable: bool,
    next_action: str = "",
    available_profiles: list[str] | None = None,
) -> dict[str, Any]:
    result["status"] = "blocked"
    result.update(local_runtime_error(
        error_type,
        detail,
        recoverable=recoverable,
        next_action=next_action,
        available_profiles=available_profiles,
    ))
    return result


def _block_unknown_local_runtime_profile(
    result: dict[str, Any],
    model: dict[str, Any],
    detail: str,
) -> dict[str, Any]:
    return _block_local_runtime_result(
        result,
        "LOCAL_RUNTIME_PROFILE_UNKNOWN",
        detail,
        recoverable=False,
        available_profiles=_local_runtime_available_profiles(model),
    )


def _local_runtime_selected_inputs(
    model: dict[str, Any],
    profile: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    bridges = select_local_runtime_bridges(model, profile)
    bridge_tasks = select_local_runtime_tasks_for_bridges(model, bridges)
    env_files = select_local_runtime_env_files(model, profile)
    return bridges, bridge_tasks, env_files


def _reconcile_local_runtime_bridges(
    *,
    result: dict[str, Any],
    actions: list[str],
    model: dict[str, Any],
    bridges: list[dict[str, Any]],
    bridge_tasks: list[dict[str, Any]],
    overlay_path: str | None,
    dry_run: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    needs_rerun, freshness_by_id = bridges_need_rerun(bridges, overlay_path)
    if needs_rerun and bridge_tasks:
        actions.append(
            f"bridge-rerun: stale or missing outputs for "
            f"{', '.join(sorted(freshness_by_id))}"
        )
        try:
            run_local_runtime_bridge_tasks(model, bridge_tasks, dry_run=dry_run)
        except RuntimeError as exc:
            failed = _block_local_runtime_result(
                result,
                "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
                str(exc),
                recoverable=True,
                next_action="Inspect sync.sh bridge logs and rerun focus.",
            )
            failed["bridges"] = [
                {
                    "id": str(bridge.get("id", "")),
                    "status": "failed",
                    "freshness": freshness_by_id.get(str(bridge.get("id", ""))),
                }
                for bridge in bridges
            ]
            return freshness_by_id, failed
    elif not bridges:
        actions.append("bridge-skip: no bridges declared for profile")
    else:
        actions.append("bridge-verify: all bridge outputs are fresh")
    return freshness_by_id, None


def _local_runtime_bridge_report(
    bridges: list[dict[str, Any]],
    freshness_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    bridge_report: list[dict[str, Any]] = []
    missing_outputs: list[str] = []
    for bridge in bridges:
        bid = str(bridge.get("id", "")).strip()
        state = bridge_outputs_state(bridge)
        ready = state["state"] == "ok"
        if not ready:
            missing_outputs.extend(state.get("missing", []))
        bridge_report.append({
            "id": bid,
            "status": "ready" if ready else state["state"],
            "freshness": freshness_by_id.get(bid),
            "outputs": state.get("outputs", []),
            "missing": state.get("missing", []),
        })
    return bridge_report, missing_outputs


def _local_runtime_env_file_report(env_files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
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
    return env_file_report, missing_sources


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
    result, actions = _local_runtime_reconcile_result(profile)

    # (0) Unknown / empty profile -> LOCAL_RUNTIME_PROFILE_UNKNOWN
    if not profile or not profile.strip():
        return _block_unknown_local_runtime_profile(
            result,
            model,
            "No local runtime profile selected.",
        )

    services = select_local_runtime_services(model, profile)
    if not services:
        return _block_unknown_local_runtime_profile(
            result,
            model,
            f"Profile {profile!r} has no declared local-runtime services.",
        )

    # (1) Resolve bridges + bridge-backed tasks + env files
    bridges, bridge_tasks, env_files = _local_runtime_selected_inputs(model, profile)

    # (2) Bridge freshness decision -- Flow 1 decision point
    freshness_by_id, blocked_result = _reconcile_local_runtime_bridges(
        result=result,
        actions=actions,
        model=model,
        bridges=bridges,
        bridge_tasks=bridge_tasks,
        overlay_path=overlay_path,
        dry_run=dry_run,
    )
    if blocked_result is not None:
        return blocked_result

    # Re-probe bridge state after any rerun.
    bridge_report, missing_outputs = _local_runtime_bridge_report(bridges, freshness_by_id)
    result["bridges"] = bridge_report

    if missing_outputs:
        return _block_local_runtime_result(
            result,
            "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            "Bridge outputs missing after reconciliation: "
            + ", ".join(missing_outputs),
            recoverable=True,
            next_action="Inspect sync.sh bridge and rerun focus.",
        )

    # (3) Validate each generated env source exists
    env_file_report, missing_sources = _local_runtime_env_file_report(env_files)
    result["env_files"] = env_file_report

    if missing_sources:
        return _block_local_runtime_result(
            result,
            "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            "Required env sources missing: " + ", ".join(missing_sources),
            recoverable=True,
            next_action="Inspect sync.sh bridge outputs and rerun focus.",
        )

    # (4) Validate each target_path matches the repo contract (Rule 4)
    target_violations = validate_env_file_target_paths(env_files, model)
    if target_violations:
        return _block_local_runtime_result(
            result,
            "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            "Env target path contract violated: " + "; ".join(target_violations),
            recoverable=False,
            next_action="Update overlay env_files target_path to match repo contract.",
        )

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
        return _bridge_task_success_state(model, bridge_id)
    if success_type == "path_exists":
        return _path_success_state(success)
    if success_type == "port_listening":
        return _port_success_state(success)

    bridge_id = str(task.get("bridge_id", "")).strip()
    if bridge_id and model:
        return _bridge_task_success_state(model, bridge_id)
    return {"state": "unknown"}


def _bridge_task_success_state(model: dict[str, Any], bridge_id: str) -> dict[str, Any]:
    if not bridge_id:
        return {"state": "unknown"}
    bridge = bridge_id_map(model).get(bridge_id)
    if not bridge:
        return {"state": "unknown", "target": f"bridge:{bridge_id} (not found)"}
    state = bridge_outputs_state(bridge)
    if state["state"] == "ok":
        return {"state": "ok", "target": f"bridge:{bridge_id}"}
    return {"state": "down", "target": f"bridge:{bridge_id}", "missing": state.get("missing", [])}


def _path_success_state(success: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(success["host_path"]))
    return {"state": "ok" if path.exists() else "down", "target": str(path)}


def _port_success_state(success: dict[str, Any]) -> dict[str, Any]:
    try:
        port = int(success["port"])
    except (KeyError, TypeError, ValueError):
        return {"state": "unknown"}
    host = str(success.get("host") or "127.0.0.1")
    result = _port_listening_state(port, host=host)
    return result | {"target": f"{host}:{port}"}


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
        except (urllib.error.URLError, TimeoutError, ValueError, OSError, http.client.HTTPException):
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

    if process.poll() is not None:
        return {"state": "failed", "exit_code": process.returncode}

    final_probe = service_healthcheck_state(service)
    if final_probe.get("state") == "ok":
        reused_existing = process.poll() is not None
        state = {"state": "ok"} | final_probe
        if reused_existing:
            state["reused_existing"] = True
            state["exit_code"] = process.returncode
        return state
    if process.poll() is not None:
        return {"state": "failed", "exit_code": process.returncode}

    return final_probe | {"state": "timeout"}


def tail_lines(path: Path, line_count: int) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if line_count <= 0:
        return []
    return lines[-line_count:]


def stop_process(pid: int, wait_seconds: float) -> tuple[str, int | None]:
    reap_started_service_processes()
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
        reap_started_service_processes()
        if not process_is_running(pid):
            return "stopped", signal.SIGTERM
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return "stopped", signal.SIGTERM

    deadline = time.monotonic() + 1.0
    while time.monotonic() <= deadline:
        reap_started_service_processes()
        if not process_is_running(pid):
            return "killed", signal.SIGKILL
        time.sleep(0.1)

    reap_started_service_processes()
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
        raise ValidationError(
            "runtime_error",
            "Unknown task id(s): "
            + ", ".join(unknown)
            + ". Available tasks: "
            + (", ".join(sorted(available)) or "(none)"),
            context={"unknown": unknown, "available": sorted(available)},
        )
    if not requested_ids:
        return list(model["tasks"])

    requested = set(requested_ids)
    return [
        task
        for task in model["tasks"]
        if str(task.get("id", "")).strip() in requested
    ]


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
        raise ValidationError(
            "runtime_error",
            "Unknown service id(s): "
            + ", ".join(unknown)
            + ". Available services: "
            + (", ".join(sorted(available)) or "(none)"),
            context={"unknown": unknown, "available": sorted(available)},
        )
    if not requested_ids:
        return list(model["services"])

    requested = set(requested_ids)
    return [
        service
        for service in model["services"]
        if str(service.get("id", "")).strip() in requested
    ]


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
        raise ValidationError(
            "missing_env_file",
            f"Required env files are not ready: {', '.join(unresolved)}",
            context={"unresolved": unresolved},
        )


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
    event_root = _runtime_event_root(model)
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
            raise StateConflictError(
                "runtime_error",
                f"Task {task['id']} is blocked by incomplete dependencies: {', '.join(blocked_on)}",
                context={"task": task["id"], "blocked_on": blocked_on},
            )

        command, env = translated_runtime_command(model, task)
        cwd = resolve_runtime_command_cwd(model, task)
        result["command"] = command
        result["cwd"] = str(cwd)

        ensure_directory(paths["log_dir"], dry_run)
        if dry_run:
            results.append(result | {"result": "dry-run"})
            continue

        log_runtime_event("task.started", task["id"], root_dir=event_root)
        task_timeout = float(task.get("timeout_seconds") or DEFAULT_TASK_TIMEOUT_SECONDS)
        with paths["log_file"].open("a", encoding="utf-8") as log_handle:
            # Run in a new session so the shell + every descendant share a
            # process group we can SIGKILL on timeout. subprocess.run() with
            # shell=True only kills the immediate /bin/sh child, leaving the
            # actual command's children orphaned.
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=True,
                text=True,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=task_timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                log_runtime_event("task.failed", task["id"], {"reason": "timeout"}, root_dir=event_root)
                raise RuntimeLifecycleError(
                    "runtime_error",
                    f"Task {task['id']} timed out after {task_timeout:.0f}s. "
                    "Increase 'timeout_seconds' on the task to allow more time.",
                    context={"task": task["id"], "timeout_seconds": task_timeout},
                )

        if returncode != 0:
            tail = tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)
            log_runtime_event("task.failed", task["id"], {"exit_code": returncode}, root_dir=event_root)
            raise RuntimeLifecycleError(
                "runtime_error",
                f"Task {task['id']} failed with exit code {returncode}."
                + (f" Recent logs: {' | '.join(tail)}" if tail else ""),
                context={"task": task["id"], "exit_code": returncode},
            )

        post_state = probe_task(model, task)
        if post_state["state"] != "ready":
            tail = tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)
            log_runtime_event(
                "task.failed",
                task["id"],
                {"reason": "success_check_unsatisfied"},
                root_dir=event_root,
            )
            raise RuntimeLifecycleError(
                "runtime_error",
                f"Task {task['id']} completed but did not satisfy its success check."
                + (f" Success target: {post_state['target']}." if post_state.get("target") else "")
                + (f" Recent logs: {' | '.join(tail)}" if tail else ""),
                context={"task": task["id"], "target": post_state.get("target")},
            )

        results.append(result | {"result": "completed", "target": post_state.get("target")})
        log_runtime_event("task.completed", task["id"], root_dir=event_root)
    return results


def _service_start_base_result(service: dict[str, Any], paths: dict[str, Path]) -> dict[str, Any]:
    return {
        "id": service["id"],
        "kind": service.get("kind", "service"),
        "log_file": str(paths["log_file"]),
        "pid_file": str(paths["pid_file"]),
    }


def _prelaunch_running_result(
    model: dict[str, Any],
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    event_root: Path,
) -> dict[str, Any] | None:
    prelaunch_health = service_healthcheck_state(service)
    if prelaunch_health.get("state") != "ok":
        return None
    unverified = _unverified_reuse_mismatch(
        model,
        service,
        None,
        "healthcheck was already healthy before runtime launched the service",
    )
    if unverified is not None:
        return _service_port_mismatch_result(
            service,
            result,
            paths,
            None,
            unverified,
            event_root,
            remove_pid=False,
        )
    reused_result = result | {"result": "already-running"}
    for key in ("url", "target", "port"):
        if key in prelaunch_health:
            reused_result[key] = prelaunch_health[key]
    log_runtime_event("service.reused", service["id"], root_dir=event_root)
    return reused_result


def _service_has_declared_healthcheck(service: dict[str, Any]) -> bool:
    healthcheck = service.get("healthcheck") or {}
    return bool(healthcheck.get("type"))


def _running_pid_service_result(
    model: dict[str, Any],
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    *,
    pid: int,
    dry_run: bool,
    event_root: Path,
) -> dict[str, Any] | None:
    if not _service_has_declared_healthcheck(service):
        return result | {"result": "already-running", "pid": pid}

    health_state = service_healthcheck_state(service)
    if health_state.get("state") == "ok":
        reused_result = result | {"result": "already-running", "pid": pid}
        _copy_health_fields(reused_result, health_state, ("url", "target", "port", "pattern"))
        verification = _verify_service_declared_ports(model, service, pid)
        if verification.get("state") == "mismatch":
            return _service_port_mismatch_result(
                service,
                result,
                paths,
                pid,
                verification,
                event_root,
                remove_pid=True,
            )
        _copy_port_verification_fields(reused_result, verification)
        log_runtime_event("service.reused", service["id"], root_dir=event_root)
        return reused_result

    if dry_run:
        detail = result | {
            "result": "would-restart",
            "pid": pid,
            "health_state": health_state.get("state", "unknown"),
        }
        _copy_health_fields(detail, health_state, ("url", "target", "port", "pattern"))
        return detail

    stop_result, signal_used = stop_process(pid, DEFAULT_SERVICE_STOP_WAIT_SECONDS)
    if stop_result not in {"stopped", "killed", "not-running"}:
        detail = result | {
            "result": "failed",
            "pid": pid,
            "reason": "already-running service is unhealthy and could not be stopped for restart",
            "stop_result": stop_result,
        }
        if signal_used is not None:
            detail["signal"] = signal_used
        _copy_health_fields(detail, health_state, ("url", "target", "port", "pattern"))
        log_runtime_event(
            "service.restart_blocked",
            service["id"],
            {"pid": pid, "health_state": health_state.get("state"), "stop_result": stop_result},
            root_dir=event_root,
        )
        return detail

    remove_pid_file(paths["pid_file"])
    result["previous_pid"] = pid
    result["recovered_from"] = "unhealthy-already-running"
    result["previous_health_state"] = health_state.get("state", "unknown")
    _copy_health_fields(result, health_state, ("url", "target", "port", "pattern"))
    log_runtime_event(
        "service.restarting_unhealthy",
        service["id"],
        {"previous_pid": pid, "health_state": health_state.get("state"), "stop_result": stop_result},
        root_dir=event_root,
    )
    return None


def _service_launch_context(
    model: dict[str, Any],
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    effective_mode: str | None,
) -> tuple[list[str], dict[str, str], Path, bool]:
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
    healthcheck = launch_service.get("healthcheck") or {}
    self_managed_pid_file = (
        healthcheck.get("type") == "path_exists"
        and str(healthcheck.get("host_path") or "") == str(paths["pid_file"])
    )
    return command, env, cwd, self_managed_pid_file


def _spawn_service_process(command: list[str], cwd: Path, env: dict[str, str], log_file: Path) -> subprocess.Popen[str]:
    with log_file.open("a", encoding="utf-8") as log_handle:
        return subprocess.Popen(
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


def _write_managed_service_pid(process: subprocess.Popen[str], pid_file: Path) -> None:
    try:
        tmp_pid = pid_file.with_suffix(pid_file.suffix + ".tmp")
        tmp_pid.write_text(f"{process.pid}\n", encoding="utf-8")
        os.replace(tmp_pid, pid_file)
    except OSError:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except OSError:
            pass
        raise


def _copy_health_fields(detail: dict[str, Any], health_state: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if key in health_state:
            detail[key] = health_state[key]


def _failed_service_start_result(
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    process: subprocess.Popen[str],
    health_state: dict[str, Any],
    self_managed_pid_file: bool,
    event_root: Path,
) -> dict[str, Any]:
    stop_process(process.pid, DEFAULT_SERVICE_STOP_WAIT_SECONDS)
    if not self_managed_pid_file:
        remove_pid_file(paths["pid_file"])
    detail = result | {"result": "failed", "tail": tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)}
    _copy_health_fields(detail, health_state, ("exit_code", "url", "target", "pattern"))
    log_runtime_event("service.start_failed", service["id"], {"state": "failed"}, root_dir=event_root)
    return detail


def _timeout_service_start_result(
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    process: subprocess.Popen[str],
    health_state: dict[str, Any],
    event_root: Path,
) -> dict[str, Any]:
    track_started_service_process(process)
    detail = result | {
        "result": "timeout",
        "pid": process.pid,
        "tail": tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES),
    }
    _copy_health_fields(detail, health_state, ("url", "target", "pattern"))
    log_runtime_event(
        "service.start_timeout",
        service["id"],
        {"state": "timeout", "pid": process.pid},
        root_dir=event_root,
    )
    return detail


def _self_managed_reused_result(
    model: dict[str, Any],
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    health_state: dict[str, Any],
    event_root: Path,
) -> dict[str, Any]:
    started_pid = live_service_pid(paths["pid_file"])
    if started_pid is None:
        detail = result | {"result": "failed", "tail": tail_lines(paths["log_file"], DEFAULT_LOG_TAIL_LINES)}
        _copy_health_fields(detail, health_state, ("exit_code", "target"))
        log_runtime_event("service.start_failed", service["id"], {"state": "failed"}, root_dir=event_root)
        return detail
    verification = _verify_service_declared_ports(model, service, started_pid)
    if verification.get("state") == "mismatch":
        return _service_port_mismatch_result(
            service,
            result,
            paths,
            started_pid,
            verification,
            event_root,
            remove_pid=True,
        )
    started_detail = result | {"result": "started", "pid": started_pid}
    _copy_health_fields(started_detail, health_state, ("target",))
    _copy_port_verification_fields(started_detail, verification)
    log_runtime_event("service.started", service["id"], {"pid": started_pid}, root_dir=event_root)
    return started_detail


def _reused_existing_service_result(
    model: dict[str, Any],
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    process: subprocess.Popen[str],
    health_state: dict[str, Any],
    self_managed_pid_file: bool,
    event_root: Path,
) -> dict[str, Any]:
    if self_managed_pid_file:
        return _self_managed_reused_result(model, service, result, paths, health_state, event_root)
    remove_pid_file(paths["pid_file"])
    unverified = _unverified_reuse_mismatch(
        model,
        service,
        process.pid,
        "launcher exited before owning a declared listener",
    )
    if unverified is not None:
        return _service_port_mismatch_result(
            service,
            result,
            paths,
            process.pid,
            unverified,
            event_root,
            remove_pid=True,
        )
    reused_result = result | {"result": "already-running"}
    _copy_health_fields(reused_result, health_state, ("url", "target"))
    log_runtime_event("service.reused", service["id"], root_dir=event_root)
    return reused_result


def _healthy_service_start_result(
    model: dict[str, Any],
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    process: subprocess.Popen[str],
    self_managed_pid_file: bool,
    event_root: Path,
) -> dict[str, Any]:
    started_pid = live_service_pid(paths["pid_file"]) if self_managed_pid_file else process.pid
    started_detail = result | {"result": "started"}
    if started_pid is not None:
        started_detail["pid"] = started_pid
    verification = _verify_service_declared_ports(model, service, started_pid)
    if verification.get("state") == "mismatch":
        return _service_port_mismatch_result(
            service,
            result,
            paths,
            started_pid,
            verification,
            event_root,
            remove_pid=not self_managed_pid_file,
        )
    _copy_port_verification_fields(started_detail, verification)
    log_runtime_event(
        "service.started",
        service["id"],
        {"pid": started_pid or process.pid},
        root_dir=event_root,
    )
    track_started_service_process(process)
    return started_detail


def _service_health_start_result(
    model: dict[str, Any],
    service: dict[str, Any],
    result: dict[str, Any],
    paths: dict[str, Path],
    process: subprocess.Popen[str],
    wait_seconds: float,
    self_managed_pid_file: bool,
    event_root: Path,
) -> dict[str, Any]:
    health_state = wait_for_service_health(service, process, wait_seconds)
    if health_state.get("state") == "failed":
        return _failed_service_start_result(
            service,
            result,
            paths,
            process,
            health_state,
            self_managed_pid_file,
            event_root,
        )
    if health_state.get("state") == "timeout":
        return _timeout_service_start_result(service, result, paths, process, health_state, event_root)
    if health_state.get("reused_existing"):
        return _reused_existing_service_result(
            model,
            service,
            result,
            paths,
            process,
            health_state,
            self_managed_pid_file,
            event_root,
        )
    return _healthy_service_start_result(model, service, result, paths, process, self_managed_pid_file, event_root)


def _start_service(
    model: dict[str, Any],
    service: dict[str, Any],
    *,
    dry_run: bool,
    wait_seconds: float,
    effective_mode: str | None,
) -> dict[str, Any]:
    event_root = _runtime_event_root(model)
    manageable, reason = service_supports_lifecycle(service, model)
    paths = service_paths(model, service)
    result = _service_start_base_result(service, paths)
    if not manageable:
        return result | {"result": "skipped", "reason": reason}

    pid = live_service_pid(paths["pid_file"])
    if pid is not None:
        running_result = _running_pid_service_result(
            model,
            service,
            result,
            paths,
            pid=pid,
            dry_run=dry_run,
            event_root=event_root,
        )
        if running_result is not None:
            return running_result

    prelaunch_result = _prelaunch_running_result(model, service, result, paths, event_root)
    if prelaunch_result is not None:
        return prelaunch_result

    declared = _service_declared_listen_port(service)
    if declared is not None:
        host, port = declared
        port_state = _port_listening_state(port, host=host)
        if port_state.get("state") == "ok":
            owner = _external_listener_owner(host, port)
            return _port_conflict_blocked_result(service, result, host, port, owner, event_root)

    command, env, cwd, self_managed_pid_file = _service_launch_context(
        model, service, result, paths, effective_mode,
    )
    ensure_directory(paths["log_dir"], dry_run)
    if dry_run:
        return result | {"result": "dry-run"}

    process = _spawn_service_process(command, cwd, env, paths["log_file"])
    if not self_managed_pid_file:
        _write_managed_service_pid(process, paths["pid_file"])
    return _service_health_start_result(
        model,
        service,
        result,
        paths,
        process,
        wait_seconds,
        self_managed_pid_file,
        event_root,
    )


def _start_result_allows_dependents(entry: dict[str, Any]) -> bool:
    return entry.get("result") in {"started", "already-running", "dry-run", "would-restart"}


def _blocked_service_start_result(
    service: dict[str, Any],
    model: dict[str, Any],
    blocked_on: list[str],
) -> dict[str, Any]:
    paths = service_paths(model, service)
    return _service_start_base_result(service, paths) | {
        "result": "blocked",
        "blocked_on": blocked_on,
        "reason": "dependency did not become healthy",
    }


def _already_running_short_circuit(
    model: dict[str, Any],
    service: dict[str, Any],
    blocked_on: list[str],
) -> dict[str, Any] | None:
    event_root = _runtime_event_root(model)
    manageable, _reason = service_supports_lifecycle(service, model)
    if not manageable:
        return None
    paths = service_paths(model, service)
    base = _service_start_base_result(service, paths)
    pid = live_service_pid(paths["pid_file"])
    if pid is None:
        prelaunch = _prelaunch_running_result(model, service, base, paths, event_root)
        return _annotate_degraded_deps(prelaunch, blocked_on)
    if not _service_has_declared_healthcheck(service):
        return _annotate_degraded_deps(base | {"result": "already-running", "pid": pid}, blocked_on)
    health_state = service_healthcheck_state(service)
    if health_state.get("state") != "ok":
        return None
    reused_result = base | {"result": "already-running", "pid": pid}
    _copy_health_fields(reused_result, health_state, ("url", "target", "port", "pattern"))
    verification = _verify_service_declared_ports(model, service, pid)
    if verification.get("state") == "mismatch":
        return _annotate_degraded_deps(
            _service_port_mismatch_result(
                service,
                base,
                paths,
                pid,
                verification,
                event_root,
                remove_pid=True,
            ),
            blocked_on,
        )
    _copy_port_verification_fields(reused_result, verification)
    log_runtime_event("service.reused", service["id"], root_dir=event_root)
    return _annotate_degraded_deps(reused_result, blocked_on)


def _annotate_degraded_deps(result: dict[str, Any] | None, blocked_on: list[str]) -> dict[str, Any] | None:
    if result is None:
        return None
    if blocked_on:
        result["dependency_unhealthy"] = list(blocked_on)
    return result


def start_services(
    model: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    dry_run: bool,
    wait_seconds: float,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    effective_mode = (mode or "").strip() or None
    results: list[dict[str, Any]] = []
    results_by_id: dict[str, dict[str, Any]] = {}
    for service in services:
        blocked_on = [
            dependency_id
            for dependency_id in service_dependency_ids(service)
            if dependency_id in results_by_id
            and not _start_result_allows_dependents(results_by_id[dependency_id])
        ]
        if blocked_on:
            already_running = _already_running_short_circuit(model, service, blocked_on)
            if already_running is not None:
                result = already_running
            else:
                result = _blocked_service_start_result(service, model, blocked_on)
        else:
            result = _start_service(
                model,
                service,
                dry_run=dry_run,
                wait_seconds=wait_seconds,
                effective_mode=effective_mode,
            )
        results.append(result)
        service_id = str(service.get("id", "")).strip()
        if service_id:
            results_by_id[service_id] = result
    return results


def stop_services(
    model: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    dry_run: bool,
    wait_seconds: float,
) -> list[dict[str, Any]]:
    event_root = _runtime_event_root(model)
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
        log_runtime_event("service.stopped", service["id"], {"signal": signal_used}, root_dir=event_root)
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
    elif (
        pid is None
        and manager_state.get("managed") is False
        and str(manager_state.get("manager_reason") or "").startswith("optional artifact ")
    ):
        state = "not-configured"
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


def _live_repo_state(repo: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(repo["host_path"]))
    item: dict[str, Any] = {
        "id": repo["id"],
        "path": str(repo["path"]),
        "present": path.exists(),
    }
    if repo.get("project_kind"):
        item["project_kind"] = repo.get("project_kind")
    if repo.get("command_lanes"):
        item["command_lanes"] = list((repo.get("command_lanes") or {}).keys())
    if not path.exists() or not path.is_dir():
        return item

    git_state = git_repo_state(path)
    item.update(git_state)
    if git_state.get("git"):
        log_result = run_command(["git", "log", "--oneline", "-1"], cwd=path)
        if log_result.returncode == 0:
            item["last_commit"] = log_result.stdout.strip()
    return item


def _live_repo_states(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _live_repo_state(repo)
        for repo in model.get("repos") or []
    ]


def _live_service_state(model: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    probe = probe_service(model, service)
    return {
        "id": service["id"],
        "kind": service.get("kind", "service"),
        "state": probe.get("state", "declared"),
        "pid": probe.get("pid"),
        "healthy": probe.get("state") == "running",
    }


def _live_service_states(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _live_service_state(model, service)
        for service in model.get("services") or []
    ]


def _live_check_state(check: dict[str, Any]) -> dict[str, Any]:
    item = {"id": check["id"], "type": check["type"], "ok": False}
    if check["type"] == "path_exists":
        item["ok"] = Path(str(check["host_path"])).exists()
    return item


def _live_check_states(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _live_check_state(check)
        for check in model.get("checks") or []
    ]


def _recent_log_errors(path: Path) -> tuple[list[str], list[str]]:
    log_files = sorted(
        (file_path for file_path in path.rglob("*") if file_path.is_file()),
        key=lambda file_path: file_path.stat().st_mtime,
        reverse=True,
    )
    errors: list[str] = []
    scanned_files: list[str] = []
    for log_file in log_files[:5]:
        scanned_files.append(str(log_file.name))
        lines = tail_lines(log_file, 100)
        errors.extend(line for line in lines if FOCUS_ERROR_PATTERNS.search(line))
    return errors[-5:], scanned_files


def _live_log_state(log_item: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(log_item["host_path"]))
    item = {
        "id": log_item["id"],
        "path": str(log_item["path"]),
        "present": path.exists(),
        "recent_errors": [],
    }
    if not path.exists():
        return item

    errors, scanned_files = _recent_log_errors(path)
    item["recent_errors"] = errors
    if scanned_files:
        item["scanned_files"] = scanned_files
        item["scanned_file"] = scanned_files[0]
    return item


def _live_log_states(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _live_log_state(log_item)
        for log_item in model.get("logs") or []
    ]


def _active_session_client_ids(model: dict[str, Any]) -> list[str]:
    return [
        str(client_id)
        for client_id in model.get("active_clients") or []
        if str(client_id).strip()
    ]


def _live_session_states(model: dict[str, Any], root_dir: Path) -> list[dict[str, Any]]:
    session_states: list[dict[str, Any]] = []
    for client_id in _active_session_client_ids(model):
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
    session_states.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    return session_states


def _live_bridge_state(bridge: dict[str, Any]) -> dict[str, Any]:
    state = bridge_outputs_state(bridge)
    return {
        "id": bridge["id"],
        "env_tier": bridge.get("env_tier", "local"),
        "targets": bridge.get("legacy_targets", []),
        "state": state["state"],
        "missing": state.get("missing", []),
    }


def _live_bridge_states(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _live_bridge_state(bridge)
        for bridge in model.get("bridges") or []
    ]


def collect_live_state(
    model: dict[str, Any],
    root_dir: Path = DEFAULT_ROOT_DIR,
) -> dict[str, Any]:
    """Snapshot volatile runtime state: git branches, service health, check results, recent errors."""
    return {
        "collected_at": time.time(),
        "repos": _live_repo_states(model),
        "services": _live_service_states(model),
        "checks": _live_check_states(model),
        "logs": _live_log_states(model),
        "sessions": _live_session_states(model, root_dir),
        "bridges": _live_bridge_states(model),
        "pressure_advisory": runtime_pressure_advisory(root_dir),
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


def _runtime_repo_statuses(model: dict[str, Any]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for repo in model["repos"]:
        path = Path(str(repo["host_path"]))
        item = {
            "id": repo["id"],
            "kind": repo.get("kind", "repo"),
            "project_kind": repo.get("project_kind", ""),
            "command_lanes": list((repo.get("command_lanes") or {}).keys()),
            "path": str(repo["path"]),
            "host_path": str(path),
            "present": path.exists(),
            "profiles": repo.get("profiles") or [],
        }
        if path.exists() and path.is_dir():
            item.update(git_repo_state(path))
        statuses.append(item)
    return statuses


def _runtime_skill_status(skillset: dict[str, Any]) -> dict[str, Any]:
    if skillset.get("kind") == "skill-repo-set":
        return _collect_skill_repo_status(skillset)
    inventory = collect_skill_inventory(skillset)
    return {
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


def _runtime_task_statuses(model: dict[str, Any]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
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
        statuses.append(item)
    return statuses


def _runtime_service_statuses(model: dict[str, Any]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
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
        statuses.append(item)
    return statuses


def _runtime_blocked_services(service_statuses: list[dict[str, Any]]) -> list[str]:
    return [
        str(entry.get("id", ""))
        for entry in service_statuses
        if entry.get("state") not in {"running", "ok", "idle", "not-configured"}
        and str(entry.get("id", "")).strip()
    ]


def _runtime_log_statuses(model: dict[str, Any]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for log_item in model["logs"]:
        path = Path(str(log_item["host_path"]))
        item = {
            "id": log_item["id"],
            "path": str(log_item["path"]),
            "host_path": str(path),
        }
        item.update(log_directory_state(path))
        statuses.append(item)
    return statuses


def _runtime_check_statuses(model: dict[str, Any]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
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
        statuses.append(item)
    return statuses


def _runtime_ingress_payload(model: dict[str, Any]) -> dict[str, Any]:
    ingress_paths = ingress_config_paths(model) if has_ingress_runtime(model) else {}
    return {
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


def _annotate_runtime_service_exposure(
    model: dict[str, Any],
    service_statuses: list[dict[str, Any]],
    box_access: dict[str, Any],
) -> list[str]:
    try:
        from .endpoints import annotate_service_rows
    except Exception:
        return []
    try:
        return annotate_service_rows(model, service_statuses, box_access=box_access)
    except Exception:
        return []


def runtime_status(model: dict[str, Any]) -> dict[str, Any]:
    service_statuses = _runtime_service_statuses(model)
    box_access = runtime_box_access_from_env(model.get("env") or {})
    warnings = _annotate_runtime_service_exposure(model, service_statuses, box_access)
    root_dir = Path(str(model.get("root_dir") or DEFAULT_ROOT_DIR))
    return {
        "box_access": box_access,
        "clients": copy.deepcopy(model.get("clients") or []),
        "active_clients": model.get("active_clients") or [],
        "default_client": (model.get("selection") or {}).get("default_client"),
        "active_profiles": model.get("active_profiles") or [],
        "distributors": _runtime_status_distributors(model),
        "storage": copy.deepcopy(model.get("storage") or {}),
        "repos": _runtime_repo_statuses(model),
        "artifacts": [artifact_state(artifact) for artifact in model["artifacts"]],
        "env_files": [env_file_state(env_file) for env_file in model["env_files"]],
        "skills": [_runtime_skill_status(skillset) for skillset in model["skills"]],
        "tasks": _runtime_task_statuses(model),
        "services": service_statuses,
        "blocked_services": _runtime_blocked_services(service_statuses),
        "logs": _runtime_log_statuses(model),
        "checks": _runtime_check_statuses(model),
        "ingress": _runtime_ingress_payload(model),
        "warnings": warnings,
        "pressure_advisory": runtime_pressure_advisory(root_dir),
        "parity_ledger": {
            "covered_surfaces": parity_ledger_covered_surfaces(model),
            "deferred_surfaces": parity_ledger_deferred_surfaces(model),
        },
    }


def _pressure_warning_messages(advisory: dict[str, Any]) -> list[str]:
    # Delegates to the shared formatter in shared.py so status, context, pulse,
    # stewardship, and evidence surfaces all build identical advisory warnings.
    return pressure_advisory_warning_messages(advisory)


def runtime_pressure_advisory(root_dir: Path, *, home: Path | None = None) -> dict[str, Any]:
    """Collect a compact read-only pressure/offload policy packet for agent-facing surfaces."""
    try:
        pressure = collect_pressure_report(root_dir, home=home, scan_candidate_sizes=False)
        rch = collect_rch_report(root_dir, run_probes=False)
        sbh = collect_sbh_report(root_dir, home=home, run_probes=False)
    except Exception as exc:  # pragma: no cover - defensive surface guard
        return {
            "ok": False,
            "mode": "read_only",
            "mutates": False,
            "error": str(exc),
            "safe_first_commands": [
                "python3 .env-manager/manage.py pressure-report --format json",
                "python3 .env-manager/manage.py rch-report --format json",
                "python3 .env-manager/manage.py sbh-report --format json",
            ],
            "warnings": ["Pressure advisory could not be collected; run pressure-report directly."],
        }

    local_disk = pressure.get("local_disk") or {}
    box = pressure.get("box") or {}
    rch_posture = rch.get("posture") or {}
    sbh_posture = sbh.get("posture") or {}
    sbh_policy = sbh.get("policy") or {}
    advisory = {
        "ok": True,
        "mode": "read_only",
        "mutates": False,
        "local_disk": {
            "path": local_disk.get("path"),
            "free_gib": local_disk.get("free_gib"),
            "total_gib": local_disk.get("total_gib"),
            "free_percent": local_disk.get("free_percent"),
            "pressure_level": local_disk.get("pressure_level"),
        },
        "target_worker": {
            "id": box.get("target_box"),
            "found": bool(box.get("found")),
            "state": box.get("state"),
            "tailscale_hostname": box.get("tailscale_hostname"),
            "tailscale_ip": box.get("tailscale_ip"),
            "state_root": box.get("state_root"),
            "min_free_gib": box.get("min_free_gib"),
            "excluded_box_ids": box.get("excluded_box_ids") or [],
        },
        "rch": {
            "binary_present": bool((rch.get("binary") or {}).get("present")),
            "state": rch_posture.get("state"),
            "worker_state": rch_posture.get("worker_state"),
            "fail_open_expected": bool(rch_posture.get("fail_open_expected")),
            "hook_install_allowed": bool((rch.get("global_hook_install") or {}).get("allowed")),
            "safe_first_commands": [
                "python3 .env-manager/manage.py rch-report --format json",
                *list(rch.get("safe_probe_commands") or []),
            ],
        },
        "sbh": {
            "binary_present": bool((sbh.get("binary") or {}).get("present")),
            "state": sbh_posture.get("state"),
            "daemon_state": sbh_posture.get("daemon_state"),
            "rollout_mode": sbh_policy.get("rollout_mode"),
            "auto_delete_allowed": bool(sbh_policy.get("auto_delete_allowed")),
            "ballast_mutation_allowed": bool(sbh_policy.get("ballast_mutation_allowed")),
            "safe_first_commands": [
                "python3 .env-manager/manage.py sbh-report --format json",
                *list(sbh.get("safe_probe_commands") or []),
            ],
            "blocked_mutation_commands": sbh.get("blocked_mutation_commands") or [],
            "release_caveats": sbh.get("release_caveats") or [],
        },
        "protected_paths": [
            {
                "id": entry.get("id"),
                "path": entry.get("display_path") or entry.get("path"),
                "policy": entry.get("policy"),
            }
            for entry in pressure.get("protected_buckets") or []
        ],
        "review_only_paths": [
            {
                "id": entry.get("id"),
                "path": entry.get("display_path") or entry.get("path"),
                "class": entry.get("class"),
                "policy": entry.get("policy"),
            }
            for entry in pressure.get("review_only_candidates") or []
        ],
        "safe_first_commands": [
            "python3 .env-manager/manage.py pressure-report --format json",
            "python3 .env-manager/manage.py rch-report --format json",
            "python3 .env-manager/manage.py sbh-report --format json",
        ],
    }
    advisory["warnings"] = _pressure_warning_messages(advisory)
    return advisory


def _runtime_env_value(env_values: dict[str, Any] | None, key: str, default: str = "") -> str:
    raw = os.environ.get(key)
    if raw is None and env_values is not None:
        raw = env_values.get(key)
    if raw is None:
        raw = default
    return str(raw).strip()


def _tailscale_status_payload() -> dict[str, Any]:
    if not shutil.which("tailscale"):
        return {}
    result = run_command(["tailscale", "status", "--json"])
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _first_ipv4(values: list[Any]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and "." in text and ":" not in text:
            return text
    return ""


def _tailscale_box_access_fallback() -> dict[str, Any]:
    payload = _tailscale_status_payload()
    if not payload:
        return {}
    self_node = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    ips = payload.get("TailscaleIPs") or self_node.get("TailscaleIPs") or []
    dns_name = str(self_node.get("DNSName") or "").strip().rstrip(".")
    hostname = str(self_node.get("HostName") or "").strip()
    return {
        "box_id": hostname or None,
        "tailscale_ip": _first_ipv4(list(ips)) or None,
        "tailscale_hostname": dns_name or hostname or None,
        "tailscale_available": True,
        "tailscale_state": str(payload.get("BackendState") or "").strip() or None,
        "tailnet": (payload.get("CurrentTailnet") or {}).get("Name"),
        "source": "tailscale",
    }


def runtime_box_access_from_env(env_values: dict[str, Any] | None = None) -> dict[str, Any]:
    fallback = _tailscale_box_access_fallback()
    box_id = _runtime_env_value(env_values, "SKILLBOX_BOX_ID") or str(fallback.get("box_id") or "").strip()
    tailnet_ip = (
        _runtime_env_value(env_values, "SKILLBOX_BOX_TAILSCALE_IP")
        or str(fallback.get("tailscale_ip") or "").strip()
    )
    magicdns = (
        _runtime_env_value(env_values, "SKILLBOX_BOX_TAILSCALE_HOSTNAME")
        or str(fallback.get("tailscale_hostname") or "").strip()
    )
    port = _runtime_env_value(env_values, "SKILLBOX_SWIMMERS_PORT", "3210") or "3210"
    phone_url = f"http://{tailnet_ip}:{port}/" if tailnet_ip else None
    magicdns_url = f"http://{magicdns}:{port}/" if magicdns else None
    return {
        "self": _runtime_env_value(env_values, "SKILLBOX_BOX_SELF").lower() == "true" or bool(fallback),
        "box_id": box_id or None,
        "tailscale_ip": tailnet_ip or None,
        "tailscale_hostname": magicdns or None,
        "tailscale_available": bool(fallback),
        "tailscale_state": fallback.get("tailscale_state"),
        "tailnet": fallback.get("tailnet"),
        "source": "env" if _runtime_env_value(env_values, "SKILLBOX_BOX_TAILSCALE_IP") else fallback.get("source") or "env",
        "phone_url": phone_url,
        "browser_url": phone_url,
        "magicdns_url": magicdns_url,
    }


def _compact_client_ids(status_payload: dict[str, Any]) -> list[str]:
    return [
        str(client.get("id"))
        for client in status_payload.get("clients") or []
        if client.get("id")
    ]


def _compact_status_skill(skillset: dict[str, Any]) -> dict[str, Any]:
    targets = [
        target
        for skill_entry in skillset.get("skills") or []
        for target in skill_entry.get("targets") or []
    ]
    return {
        "id": skillset.get("id"),
        "kind": skillset.get("kind"),
        "lock_present": bool(skillset.get("lock_present")),
        "lock_error": skillset.get("lock_error"),
        "skill_count": len(skillset.get("skills") or []),
        "healthy_targets": sum(1 for target in targets if target.get("state") == "ok"),
        "total_targets": len(targets),
    }


def _compact_status_skills(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _compact_status_skill(skillset)
        for skillset in status_payload.get("skills") or []
    ]


def _compact_status_storage(status_payload: dict[str, Any]) -> dict[str, Any]:
    storage = status_payload.get("storage") or {}
    return {
        "provider": storage.get("provider"),
        "state_root": storage.get("state_root"),
        "required": bool(storage.get("required")),
    }


def _compact_status_repos(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": repo.get("id"),
            "present": bool(repo.get("present")),
            "project_kind": repo.get("project_kind") or "",
            "command_lanes": repo.get("command_lanes") or [],
            "branch": repo.get("branch"),
            "dirty": repo.get("dirty"),
            "untracked": repo.get("untracked"),
        }
        for repo in status_payload.get("repos") or []
    ]


def _compact_status_artifacts(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": artifact.get("id"),
            "state": artifact.get("state"),
            "source_kind": artifact.get("source_kind"),
            "required": bool(artifact.get("required")),
        }
        for artifact in status_payload.get("artifacts") or []
    ]


def _compact_status_env_files(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": env_file.get("id"),
            "state": env_file.get("state"),
            "source_kind": env_file.get("source_kind"),
            "required": bool(env_file.get("required")),
        }
        for env_file in status_payload.get("env_files") or []
    ]


def _compact_status_tasks(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": task.get("id"),
            "state": task.get("state"),
            "depends_on": task.get("depends_on") or [],
        }
        for task in status_payload.get("tasks") or []
    ]


def _compact_status_services(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": service.get("id"),
            "state": service.get("state"),
            "pid": service.get("pid"),
            "managed": service.get("managed"),
            "manager_reason": service.get("manager_reason"),
            "ownership_state": service.get("ownership_state"),
            "depends_on": service.get("depends_on") or [],
            "bootstrap_tasks": service.get("bootstrap_tasks") or [],
            "exposure": service.get("exposure"),
            "endpoint": service.get("endpoint") or {},
            "endpoint_url": service.get("endpoint_url"),
            "viewable_from_tailnet": service.get("viewable_from_tailnet"),
        }
        for service in status_payload.get("services") or []
    ]


def _compact_status_logs(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": log_item.get("id"),
            "present": bool(log_item.get("present")),
            "files": log_item.get("files"),
            "bytes": log_item.get("bytes"),
        }
        for log_item in status_payload.get("logs") or []
    ]


def _compact_status_checks(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": check.get("id"),
            "type": check.get("type"),
            "ok": check.get("ok"),
        }
        for check in status_payload.get("checks") or []
    ]


def _compact_status_ingress(status_payload: dict[str, Any]) -> dict[str, Any]:
    ingress = status_payload.get("ingress") or {}
    return {
        "route_file_present": bool(ingress.get("route_file_present")),
        "nginx_config_present": bool(ingress.get("nginx_config_present")),
        "route_count": len(ingress.get("routes") or []),
    }


def _compact_status_parity_ledger(status_payload: dict[str, Any]) -> dict[str, Any]:
    parity_ledger = status_payload.get("parity_ledger") or {}
    return {
        "covered_count": len(parity_ledger.get("covered_surfaces") or []),
        "deferred_surfaces": parity_ledger.get("deferred_surfaces") or [],
    }


def compact_runtime_status(status_payload: dict[str, Any]) -> dict[str, Any]:
    """Return the agent-facing status summary without heavyweight raw config."""
    return {
        "client_ids": _compact_client_ids(status_payload),
        "active_clients": status_payload.get("active_clients") or [],
        "default_client": status_payload.get("default_client"),
        "active_profiles": status_payload.get("active_profiles") or [],
        "box_access": status_payload.get("box_access") or {},
        "distributors": status_payload.get("distributors") or [],
        "storage": _compact_status_storage(status_payload),
        "repos": _compact_status_repos(status_payload),
        "artifacts": _compact_status_artifacts(status_payload),
        "env_files": _compact_status_env_files(status_payload),
        "skills": _compact_status_skills(status_payload),
        "tasks": _compact_status_tasks(status_payload),
        "services": _compact_status_services(status_payload),
        "blocked_services": status_payload.get("blocked_services") or [],
        "warnings": status_payload.get("warnings") or [],
        "logs": _compact_status_logs(status_payload),
        "checks": _compact_status_checks(status_payload),
        "ingress": _compact_status_ingress(status_payload),
        "pressure_advisory": status_payload.get("pressure_advisory") or {},
        "parity_ledger": _compact_status_parity_ledger(status_payload),
        "next_actions": status_payload.get("next_actions") or [],
    }
