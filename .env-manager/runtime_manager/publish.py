from __future__ import annotations

from .shared import *
from .context_rendering import *
from .runtime_ops import *

def stable_json_digest(value: Any) -> str:
    return digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))


def bundle_runtime_model(bundle: dict[str, Any]) -> dict[str, Any]:
    bundle_dir = Path(str(bundle["bundle_dir"]))
    runtime_model_rel = PurePosixPath(str(bundle["runtime_model_rel"]))
    return load_json_file(bundle_dir / Path(*runtime_model_rel.parts))


def diff_string_values(current_values: list[Any], candidate_values: list[Any]) -> dict[str, Any]:
    current = sorted({str(value).strip() for value in current_values if str(value).strip()})
    candidate = sorted({str(value).strip() for value in candidate_values if str(value).strip()})
    current_set = set(current)
    candidate_set = set(candidate)
    return {
        "added": sorted(candidate_set - current_set),
        "removed": sorted(current_set - candidate_set),
        "unchanged": len(current_set & candidate_set),
    }


def diff_named_entries(
    current_map: dict[str, str],
    candidate_map: dict[str, str],
) -> dict[str, Any]:
    current_ids = set(current_map)
    candidate_ids = set(candidate_map)
    shared_ids = sorted(current_ids & candidate_ids)
    changed = [item_id for item_id in shared_ids if current_map[item_id] != candidate_map[item_id]]
    return {
        "added": sorted(candidate_ids - current_ids),
        "removed": sorted(current_ids - candidate_ids),
        "changed": changed,
        "unchanged": len(shared_ids) - len(changed),
    }


def diff_file_entries(
    current_entries: list[tuple[str, str]],
    candidate_entries: list[tuple[str, str]],
) -> dict[str, Any]:
    current_map = dict(current_entries)
    candidate_map = dict(candidate_entries)
    current_paths = set(current_map)
    candidate_paths = set(candidate_map)
    shared_paths = sorted(current_paths & candidate_paths)
    changed = [
        {
            "path": rel_path,
            "current_sha256": current_map[rel_path],
            "candidate_sha256": candidate_map[rel_path],
        }
        for rel_path in shared_paths
        if current_map[rel_path] != candidate_map[rel_path]
    ]
    unchanged = len(shared_paths) - len(changed)
    added = sorted(candidate_paths - current_paths)
    removed = sorted(current_paths - candidate_paths)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": unchanged,
        },
    }


def runtime_section_digest_map(model: dict[str, Any], section: str) -> dict[str, str]:
    digest_map: dict[str, str] = {}
    raw_items = model.get(section) or []
    if not isinstance(raw_items, list):
        return digest_map
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        digest_map[item_id] = stable_json_digest(item)
    return digest_map


def diff_runtime_models(current_model: dict[str, Any], candidate_model: dict[str, Any]) -> dict[str, Any]:
    section_changes: dict[str, Any] = {}
    changed_sections: list[str] = []
    for section in CLIENT_RUNTIME_DIFF_SECTIONS:
        section_change = diff_named_entries(
            runtime_section_digest_map(current_model, section),
            runtime_section_digest_map(candidate_model, section),
        )
        section_changes[section] = section_change
        if section_change["added"] or section_change["removed"] or section_change["changed"]:
            changed_sections.append(section)
    return {
        "active_profiles": diff_string_values(
            current_model.get("active_profiles") or [],
            candidate_model.get("active_profiles") or [],
        ),
        "active_clients": diff_string_values(
            current_model.get("active_clients") or [],
            candidate_model.get("active_clients") or [],
        ),
        "sections": section_changes,
        "changed_sections": changed_sections,
    }


def diff_projection_metadata(
    current_projection: dict[str, Any] | None,
    candidate_projection: dict[str, Any],
) -> dict[str, Any]:
    current = current_projection or {}
    return {
        "current_present": current_projection is not None,
        "overlay_mode": {
            "current": current.get("overlay_mode"),
            "candidate": candidate_projection.get("overlay_mode"),
            "changed": current.get("overlay_mode") != candidate_projection.get("overlay_mode"),
        },
        "default_client": {
            "current": current.get("default_client"),
            "candidate": candidate_projection.get("default_client"),
            "changed": current.get("default_client") != candidate_projection.get("default_client"),
        },
        "active_profiles": diff_string_values(
            current.get("active_profiles") or [],
            candidate_projection.get("active_profiles") or [],
        ),
        "active_clients": diff_string_values(
            current.get("active_clients") or [],
            candidate_projection.get("active_clients") or [],
        ),
    }


def diff_publish_metadata(
    actual_payload: dict[str, Any] | None,
    expected_payload: dict[str, Any],
) -> dict[str, Any]:
    changed_fields: list[str] = []
    if actual_payload is None:
        changed_fields = list(CLIENT_PUBLISH_METADATA_COMPARE_FIELDS)
    else:
        for field in CLIENT_PUBLISH_METADATA_COMPARE_FIELDS:
            if actual_payload.get(field) != expected_payload.get(field):
                changed_fields.append(field)

    return {
        "present": actual_payload is not None,
        "matches_candidate": not changed_fields,
        "changed_fields": changed_fields,
        "published_at": actual_payload.get("published_at") if actual_payload else None,
    }


def summarize_acceptance_metadata(acceptance_payload: dict[str, Any] | None) -> dict[str, Any]:
    if acceptance_payload is None:
        return {
            "present": False,
            "accepted_at": None,
            "source_commit": None,
            "active_profiles": [],
            "services": [],
            "mcp_servers": [],
        }
    return {
        "present": True,
        "accepted_at": acceptance_payload.get("accepted_at"),
        "source_commit": acceptance_payload.get("source_commit"),
        "active_profiles": acceptance_payload.get("active_profiles") or [],
        "services": acceptance_payload.get("services") or [],
        "mcp_servers": acceptance_payload.get("mcp_servers") or [],
    }


def acceptance_metadata_matches(
    actual_payload: dict[str, Any] | None,
    expected_payload: dict[str, Any],
) -> bool:
    if actual_payload is None:
        return False
    for field in CLIENT_ACCEPTANCE_MATCH_FIELDS:
        if actual_payload.get(field) != expected_payload.get(field):
            return False
    return True


def build_client_acceptance_metadata(
    bundle: dict[str, Any],
    acceptance_payload: dict[str, Any],
    *,
    client_id: str,
    source_commit: str | None,
) -> dict[str, Any]:
    steps = {
        str(step.get("step")): step
        for step in acceptance_payload.get("steps") or []
        if isinstance(step, dict) and str(step.get("step", "")).strip()
    }
    focus_detail = steps.get("focus", {}).get("detail") or {}
    mcp_detail = steps.get("mcp-smoke", {}).get("detail") or {}

    services = sorted(
        {
            str(service).strip()
            for service in focus_detail.get("services") or []
            if str(service).strip()
        }
    )
    mcp_servers = sorted(
        {
            str(server).strip()
            for server in mcp_detail.get("servers_ok") or []
            if str(server).strip()
        }
    )

    return {
        "version": CLIENT_ACCEPTANCE_VERSION,
        "client_id": client_id,
        "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_commit": source_commit,
        "payload_tree_sha256": bundle["payload_tree_sha256"],
        "active_profiles": acceptance_payload.get("active_profiles") or [],
        "ready": bool(acceptance_payload.get("ready")),
        "doctor_pre": steps.get("doctor-pre", {}).get("status"),
        "doctor_post": steps.get("doctor-post", {}).get("status"),
        "services": services,
        "mcp_servers": mcp_servers,
        "summary": acceptance_payload.get("summary") or {},
    }


CLIENT_DEPLOY_MATCH_FIELDS = (
    "version",
    "client_id",
    "source_commit",
    "payload_tree_sha256",
    "active_profiles",
    "archive",
    "archive_sha256",
)


def build_client_deploy_metadata(
    bundle: dict[str, Any],
    *,
    client_id: str,
    source_commit: str,
    archive_rel: str,
    archive_sha256: str,
) -> dict[str, Any]:
    projection_payload = bundle["projection"]
    return {
        "version": CLIENT_DEPLOY_VERSION,
        "client_id": client_id,
        "source_commit": source_commit,
        "payload_tree_sha256": bundle["payload_tree_sha256"],
        "active_profiles": projection_payload.get("active_profiles", []),
        "archive": archive_rel,
        "archive_sha256": archive_sha256,
    }


def deploy_metadata_matches(
    actual_payload: dict[str, Any] | None,
    expected_payload: dict[str, Any],
) -> bool:
    if actual_payload is None:
        return False
    for field in CLIENT_DEPLOY_MATCH_FIELDS:
        if actual_payload.get(field) != expected_payload.get(field):
            return False
    return True


def deploy_metadata_summary(
    target_dir: Path,
    client_id: str,
    deploy_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "present": deploy_payload is not None,
        "manifest": None,
        "archive": None,
        "archive_sha256": None,
        "source_commit": None,
        "payload_tree_sha256": None,
    }
    if deploy_payload is None:
        return summary

    client_root = target_dir / CLIENT_PUBLISH_ROOT_REL / client_id
    archive_rel = str(deploy_payload.get("archive") or "").strip()
    archive_path = client_root / Path(*PurePosixPath(archive_rel).parts) if archive_rel else None
    summary.update({
        "manifest": (CLIENT_PUBLISH_ROOT_REL / client_id / CLIENT_DEPLOY_METADATA_REL).as_posix(),
        "archive": archive_path.relative_to(target_dir).as_posix() if archive_path is not None else None,
        "archive_sha256": deploy_payload.get("archive_sha256"),
        "source_commit": deploy_payload.get("source_commit"),
        "payload_tree_sha256": deploy_payload.get("payload_tree_sha256"),
    })
    return summary


def write_client_source_archive(
    root_dir: Path,
    archive_path: Path,
    *,
    source_commit: str,
) -> None:
    ensure_directory(archive_path.parent, dry_run=False)
    result = run_command(
        [
            "git",
            "archive",
            "--format=tar.gz",
            "--prefix=skillbox/",
            f"--output={archive_path}",
            source_commit,
        ],
        cwd=root_dir,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git archive failed")


def client_publish_paths(target_dir: Path, client_id: str) -> tuple[Path, Path, Path, Path, Path, Path]:
    client_root = target_dir / CLIENT_PUBLISH_ROOT_REL / client_id
    current_dir = client_root / CLIENT_PUBLISH_CURRENT_REL
    publish_metadata_path = client_root / CLIENT_PUBLISH_METADATA_REL
    acceptance_metadata_path = client_root / CLIENT_ACCEPTANCE_METADATA_REL
    deploy_metadata_path = client_root / CLIENT_DEPLOY_METADATA_REL
    deploy_artifacts_dir = client_root / CLIENT_DEPLOY_ARTIFACTS_REL
    return (
        client_root,
        current_dir,
        publish_metadata_path,
        acceptance_metadata_path,
        deploy_metadata_path,
        deploy_artifacts_dir,
    )


def bundle_matches_publish_target(
    bundle: dict[str, Any],
    current_dir: Path,
    publish_metadata_path: Path,
) -> bool:
    if not current_dir.is_dir() or not publish_metadata_path.is_file():
        return False

    try:
        publish_payload = load_json_file(publish_metadata_path)
    except RuntimeError:
        return False

    if str(publish_payload.get("client_id") or "").strip() != str(bundle["client_id"]):
        return False
    if str(publish_payload.get("payload_tree_sha256") or "").strip().lower() != str(bundle["payload_tree_sha256"]):
        return False

    current_entries = directory_file_entries(current_dir)
    return current_entries == bundle["all_entries"]


def stage_bundle_for_publish(bundle_dir: Path, current_dir: Path) -> None:
    replace_directory_from_bundle(bundle_dir, current_dir, temp_prefix=".skillbox-client-publish-")


def replace_directory_from_bundle(
    bundle_dir: Path,
    target_dir: Path,
    *,
    temp_prefix: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix=temp_prefix) as tmpdir:
        staging_target = Path(tmpdir) / "target"
        shutil.copytree(bundle_dir, staging_target)
        ensure_directory(target_dir.parent, dry_run=False)
        remove_path(target_dir)
        shutil.move(str(staging_target), str(target_dir))


def build_client_publish_metadata(
    bundle: dict[str, Any],
    *,
    client_id: str,
    source_commit: str | None,
    acceptance_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection_payload = bundle["projection"]
    current_rel = CLIENT_PUBLISH_ROOT_REL / client_id / CLIENT_PUBLISH_CURRENT_REL
    acceptance_rel = CLIENT_PUBLISH_ROOT_REL / client_id / CLIENT_ACCEPTANCE_METADATA_REL
    published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    return {
        "version": CLIENT_PUBLISH_VERSION,
        "client_id": client_id,
        "published_at": published_at,
        "source_commit": source_commit,
        "projection_version": int(projection_payload.get("version", CLIENT_PROJECTION_VERSION)),
        "overlay_mode": projection_payload.get("overlay_mode"),
        "active_profiles": projection_payload.get("active_profiles", []),
        "active_clients": projection_payload.get("active_clients", []),
        "default_client": str(projection_payload.get("default_client") or client_id),
        "payload_tree_sha256": bundle["payload_tree_sha256"],
        "file_count": len(bundle["all_entries"]),
        "current_dir": current_rel.as_posix(),
        "projection": (current_rel / CLIENT_PROJECTION_METADATA_REL).as_posix(),
        "runtime_manifest": (current_rel / bundle["runtime_manifest_rel"]).as_posix(),
        "runtime_model": (current_rel / bundle["runtime_model_rel"]).as_posix(),
        "acceptance": acceptance_rel.as_posix() if acceptance_payload is not None else None,
        "acceptance_present": acceptance_payload is not None,
        "accepted_at": acceptance_payload.get("accepted_at") if acceptance_payload is not None else None,
        "acceptance_source_commit": acceptance_payload.get("source_commit") if acceptance_payload is not None else None,
        "acceptance_profiles": acceptance_payload.get("active_profiles") if acceptance_payload is not None else [],
    }


def commit_client_publish(target_dir: Path, client_id: str) -> str:
    client_rel = (CLIENT_PUBLISH_ROOT_REL / client_id).as_posix()
    add_result = run_command(["git", "add", "-A", "--", client_rel], cwd=target_dir)
    if add_result.returncode != 0:
        raise RuntimeError(add_result.stderr.strip() or add_result.stdout.strip() or "git add failed")

    diff_result = run_command(["git", "diff", "--cached", "--quiet"], cwd=target_dir)
    if diff_result.returncode == 0:
        return ""
    if diff_result.returncode != 1:
        raise RuntimeError(diff_result.stderr.strip() or diff_result.stdout.strip() or "git diff failed")

    message = f"chore(client-publish): publish {client_id} bundle"
    commit_result = run_command(["git", "commit", "-m", message], cwd=target_dir)
    if commit_result.returncode != 0:
        raise RuntimeError(commit_result.stderr.strip() or commit_result.stdout.strip() or "git commit failed")

    commit_hash = git_head_commit(target_dir)
    if not commit_hash:
        raise RuntimeError("git commit succeeded but HEAD could not be resolved")
    return commit_hash


def _validate_publish_target(target_dir: Path) -> None:
    target_state = git_repo_state(target_dir)
    if not target_state.get("git"):
        raise RuntimeError(f"client-publish target must be a git repo: {target_dir}")
    blocked_dirty = [
        rel for rel in git_dirty_paths(target_dir)
        if not rel.startswith(f"{CLIENT_PUBLISH_ROOT_REL.as_posix()}/")
    ]
    if blocked_dirty:
        raise RuntimeError(f"client-publish target repo has a dirty working tree: {target_dir}")


def _validate_publish_args(
    from_bundle_arg: str | None, profiles: list[str] | None, require_acceptance: bool,
) -> None:
    if from_bundle_arg and profiles:
        raise RuntimeError("client-publish cannot combine --from-bundle with --profile.")
    if from_bundle_arg and require_acceptance:
        raise RuntimeError("client-publish cannot combine --from-bundle with --acceptance.")


def _run_acceptance_for_publish(
    root_dir: Path, cid: str, profiles: list[str] | None,
) -> dict[str, Any]:
    from .workflows import run_manage_json_command
    profile_args = [arg for profile in profiles or [] for arg in ("--profile", profile)]
    acceptance_args = ["acceptance", cid, *profile_args, "--format", "json"]
    acceptance_code, acceptance_payload = run_manage_json_command(root_dir, acceptance_args)
    if acceptance_code != EXIT_OK or not acceptance_payload.get("ready"):
        error_payload = acceptance_payload.get("error") or {}
        error_message = str(error_payload.get("message") or "").strip() or (
            f"Acceptance failed during client-publish for {cid}."
        )
        raise RuntimeError(error_message)
    return acceptance_payload


def _resolve_publish_bundle(
    root_dir: Path, cid: str, from_bundle_arg: str | None, profiles: list[str] | None,
) -> tuple[Path, str, "tempfile.TemporaryDirectory[str] | None"]:
    if from_bundle_arg:
        bundle_dir = resolve_client_publish_bundle_dir(root_dir, from_bundle_arg)
        return bundle_dir, f"use-bundle: {repo_rel(root_dir, bundle_dir)}", None
    temp_bundle = tempfile.TemporaryDirectory(prefix=f".skillbox-client-publish-{cid}-")
    bundle_dir = Path(temp_bundle.name) / "bundle"
    project_client_bundle(
        root_dir, cid, profiles=profiles, output_dir_arg=str(bundle_dir),
        dry_run=False, force=True,
    )
    return bundle_dir, f"build-bundle: {cid}", temp_bundle


def _compute_publish_change_state(
    bundle: dict[str, Any],
    current_dir: Path,
    publish_metadata_path: Path,
    acceptance_metadata: dict[str, Any] | None,
    current_acceptance_metadata: dict[str, Any] | None,
    acceptance_metadata_path: Path,
) -> tuple[bool, bool, bool]:
    payload_changed = not bundle_matches_publish_target(bundle, current_dir, publish_metadata_path)
    acceptance_changed = (
        acceptance_metadata is not None
        and not acceptance_metadata_matches(current_acceptance_metadata, acceptance_metadata)
    )
    acceptance_removed = (
        payload_changed
        and acceptance_metadata is None
        and acceptance_metadata_path.is_file()
    )
    return payload_changed, acceptance_changed, acceptance_removed


def _resolve_publish_deploy_state(
    *,
    write_deploy_artifact: bool,
    source_commit: str | None,
    bundle: dict[str, Any],
    cid: str,
    root_dir: Path,
    deploy_artifacts_dir: Path,
    current_deploy_metadata: dict[str, Any] | None,
    client_root: Path,
) -> tuple[dict[str, Any] | None, bool, bool]:
    if write_deploy_artifact:
        return _resolve_publish_deploy_write_state(
            source_commit=source_commit,
            bundle=bundle,
            cid=cid,
            root_dir=root_dir,
            deploy_artifacts_dir=deploy_artifacts_dir,
            current_deploy_metadata=current_deploy_metadata,
        )
    if current_deploy_metadata is None:
        return None, False, False
    return None, False, _publish_deploy_metadata_stale(
        current_deploy_metadata=current_deploy_metadata,
        client_root=client_root,
        bundle=bundle,
        source_commit=source_commit,
    )


def _publish_deploy_archive_paths(source_commit: str) -> tuple[str, Path]:
    archive_name = f"skillbox-{source_commit[:12]}.tar.gz"
    return (CLIENT_DEPLOY_ARTIFACTS_REL / archive_name).as_posix(), Path(archive_name)


def _resolve_publish_deploy_write_state(
    *,
    source_commit: str | None,
    bundle: dict[str, Any],
    cid: str,
    root_dir: Path,
    deploy_artifacts_dir: Path,
    current_deploy_metadata: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, bool, bool]:
    if not source_commit:
        raise RuntimeError(
            "client-publish --deploy-artifact requires a git-backed source checkout with a resolvable HEAD commit."
        )
    archive_rel, archive_name = _publish_deploy_archive_paths(source_commit)
    archive_path = deploy_artifacts_dir / archive_name
    archive_sha256 = file_sha256(archive_path) if archive_path.is_file() else ""
    candidate_deploy_metadata = build_client_deploy_metadata(
        bundle, client_id=cid, source_commit=source_commit,
        archive_rel=archive_rel, archive_sha256=archive_sha256,
    )
    if archive_path.is_file() and deploy_metadata_matches(current_deploy_metadata, candidate_deploy_metadata):
        return candidate_deploy_metadata, False, False
    remove_path(deploy_artifacts_dir)
    write_client_source_archive(root_dir, archive_path, source_commit=source_commit)
    deploy_metadata = build_client_deploy_metadata(
        bundle, client_id=cid, source_commit=source_commit,
        archive_rel=archive_rel, archive_sha256=file_sha256(archive_path),
    )
    return deploy_metadata, True, False


def _publish_deploy_metadata_stale(
    *,
    current_deploy_metadata: dict[str, Any],
    client_root: Path,
    bundle: dict[str, Any],
    source_commit: str | None,
) -> bool:
    current_archive_rel = str(current_deploy_metadata.get("archive") or "").strip()
    current_archive_path = (
        client_root / Path(*PurePosixPath(current_archive_rel).parts)
        if current_archive_rel else None
    )
    return (
        str(current_deploy_metadata.get("payload_tree_sha256") or "").strip().lower()
        != str(bundle["payload_tree_sha256"]).strip().lower()
        or str(current_deploy_metadata.get("source_commit") or "").strip() != str(source_commit or "").strip()
        or current_archive_path is None
        or not current_archive_path.is_file()
    )


def _apply_publish_changes(
    *,
    payload_changed: bool,
    acceptance_metadata: dict[str, Any] | None,
    acceptance_removed: bool,
    deploy_metadata: dict[str, Any] | None,
    deploy_removed: bool,
    bundle: dict[str, Any],
    bundle_dir: Path,
    current_dir: Path,
    publish_metadata_path: Path,
    acceptance_metadata_path: Path,
    deploy_metadata_path: Path,
    deploy_artifacts_dir: Path,
    client_root: Path,
    target_dir: Path,
    cid: str,
    source_commit: str | None,
    commit: bool,
    actions: list[str],
) -> str | None:
    """Apply staging + writes + optional commit. Returns commit_hash or None."""
    if payload_changed:
        stage_bundle_for_publish(bundle_dir, current_dir)
    if acceptance_metadata is not None:
        write_json_file(acceptance_metadata_path, acceptance_metadata)
        actions.append(f"write-file: {repo_rel(target_dir, acceptance_metadata_path)}")
    elif acceptance_removed:
        remove_path(acceptance_metadata_path)
        actions.append(f"remove-file: {repo_rel(target_dir, acceptance_metadata_path)}")
    publish_payload = build_client_publish_metadata(
        bundle, client_id=cid, source_commit=source_commit,
        acceptance_payload=acceptance_metadata,
    )
    write_json_file(publish_metadata_path, publish_payload)
    actions.append(f"publish-current: {repo_rel(target_dir, current_dir)}")
    actions.append(f"write-file: {repo_rel(target_dir, publish_metadata_path)}")

    if deploy_metadata is not None:
        write_json_file(deploy_metadata_path, deploy_metadata)
        archive_path = client_root / Path(*PurePosixPath(str(deploy_metadata["archive"])).parts)
        actions.append(f"write-file: {repo_rel(target_dir, archive_path)}")
        actions.append(f"write-file: {repo_rel(target_dir, deploy_metadata_path)}")
    elif deploy_removed:
        remove_path(deploy_artifacts_dir)
        remove_path(deploy_metadata_path)
        actions.append(f"remove-path: {repo_rel(target_dir, deploy_artifacts_dir)}")
        actions.append(f"remove-file: {repo_rel(target_dir, deploy_metadata_path)}")

    if not commit:
        return None
    committed = commit_client_publish(target_dir, cid)
    if committed:
        actions.append(f"git-commit: {committed}")
        return committed
    return None


def publish_client_bundle(
    root_dir: Path,
    client_id: str,
    *,
    target_dir_arg: str | None,
    from_bundle_arg: str | None = None,
    profiles: list[str] | None = None,
    require_acceptance: bool = False,
    write_deploy_artifact: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    cid = validate_client_id(client_id)
    target_dir = resolve_client_publish_target_dir(root_dir, target_dir_arg)
    _validate_publish_target(target_dir)
    _validate_publish_args(from_bundle_arg, profiles, require_acceptance)

    actions: list[str] = []
    source_commit = git_head_commit(root_dir)
    temp_bundle: tempfile.TemporaryDirectory[str] | None = None

    try:
        acceptance_run_payload = _publish_acceptance_payload(
            root_dir, cid, profiles, require_acceptance, actions,
        )
        bundle_dir, bundle_action, temp_bundle = _resolve_publish_bundle(
            root_dir, cid, from_bundle_arg, profiles,
        )
        actions.append(bundle_action)
        bundle = load_client_projection_bundle(bundle_dir, expected_client_id=cid)
        acceptance_metadata = _publish_acceptance_metadata(bundle, acceptance_run_payload, cid, source_commit)
        paths = _publish_paths_payload(target_dir, cid)
        current_acceptance_metadata = _optional_json(paths["acceptance_metadata_path"])
        current_deploy_metadata = _optional_json(paths["deploy_metadata_path"])

        payload_changed, acceptance_changed, acceptance_removed = _compute_publish_change_state(
            bundle, paths["current_dir"], paths["publish_metadata_path"],
            acceptance_metadata, current_acceptance_metadata, paths["acceptance_metadata_path"],
        )
        deploy_metadata, deploy_changed, deploy_removed = _resolve_publish_deploy_state(
            write_deploy_artifact=write_deploy_artifact,
            source_commit=source_commit,
            bundle=bundle,
            cid=cid,
            root_dir=root_dir,
            deploy_artifacts_dir=paths["deploy_artifacts_dir"],
            current_deploy_metadata=current_deploy_metadata,
            client_root=paths["client_root"],
        )
        changed = payload_changed or acceptance_changed or acceptance_removed or deploy_changed or deploy_removed

        commit_hash: str | None = None
        if changed:
            commit_hash = _apply_publish_changes(
                payload_changed=payload_changed,
                acceptance_metadata=acceptance_metadata,
                acceptance_removed=acceptance_removed,
                deploy_metadata=deploy_metadata,
                deploy_removed=deploy_removed,
                bundle=bundle,
                bundle_dir=bundle_dir,
                current_dir=paths["current_dir"],
                publish_metadata_path=paths["publish_metadata_path"],
                acceptance_metadata_path=paths["acceptance_metadata_path"],
                deploy_metadata_path=paths["deploy_metadata_path"],
                deploy_artifacts_dir=paths["deploy_artifacts_dir"],
                client_root=paths["client_root"],
                target_dir=target_dir,
                cid=cid,
                source_commit=source_commit,
                commit=commit,
                actions=actions,
            )
        else:
            actions.append(f"publish-noop: {repo_rel(target_dir, paths['current_dir'])}")

        final_acceptance_metadata = acceptance_metadata
        if final_acceptance_metadata is None and not payload_changed:
            final_acceptance_metadata = current_acceptance_metadata
        final_deploy_metadata = deploy_metadata
        if final_deploy_metadata is None and not deploy_removed:
            final_deploy_metadata = current_deploy_metadata

        return _publish_result_payload(
            cid=cid,
            target_dir=target_dir,
            bundle_dir=bundle_dir,
            changed=changed,
            commit_hash=commit_hash,
            source_commit=source_commit,
            bundle=bundle,
            final_acceptance_metadata=final_acceptance_metadata,
            final_deploy_metadata=final_deploy_metadata,
            actions=actions,
        )
    finally:
        if temp_bundle is not None:
            temp_bundle.cleanup()


def _publish_acceptance_payload(
    root_dir: Path,
    cid: str,
    profiles: list[str] | None,
    require_acceptance: bool,
    actions: list[str],
) -> dict[str, Any] | None:
    if not require_acceptance:
        return None
    acceptance_payload = _run_acceptance_for_publish(root_dir, cid, profiles)
    actions.append(f"run-acceptance: {cid}")
    return acceptance_payload


def _publish_acceptance_metadata(
    bundle: dict[str, Any],
    acceptance_run_payload: dict[str, Any] | None,
    cid: str,
    source_commit: str | None,
) -> dict[str, Any] | None:
    if acceptance_run_payload is None:
        return None
    return build_client_acceptance_metadata(
        bundle, acceptance_run_payload, client_id=cid, source_commit=source_commit,
    )


def _publish_paths_payload(target_dir: Path, cid: str) -> dict[str, Path]:
    (
        client_root,
        current_dir,
        publish_metadata_path,
        acceptance_metadata_path,
        deploy_metadata_path,
        deploy_artifacts_dir,
    ) = client_publish_paths(target_dir, cid)
    return {
        "client_root": client_root,
        "current_dir": current_dir,
        "publish_metadata_path": publish_metadata_path,
        "acceptance_metadata_path": acceptance_metadata_path,
        "deploy_metadata_path": deploy_metadata_path,
        "deploy_artifacts_dir": deploy_artifacts_dir,
    }


def _optional_json(path: Path) -> dict[str, Any] | None:
    return load_json_file(path) if path.is_file() else None


def _publish_result_payload(
    *,
    cid: str,
    target_dir: Path,
    bundle_dir: Path,
    changed: bool,
    commit_hash: str | None,
    source_commit: str | None,
    bundle: dict[str, Any],
    final_acceptance_metadata: dict[str, Any] | None,
    final_deploy_metadata: dict[str, Any] | None,
    actions: list[str],
) -> dict[str, Any]:
    return {
        "client_id": cid,
        "target_dir": str(target_dir),
        "bundle_dir": str(bundle_dir),
        "changed": changed,
        "committed": commit_hash is not None,
        "commit_hash": commit_hash,
        "source_commit": source_commit,
        "active_profiles": bundle["projection"].get("active_profiles", []),
        "payload_tree_sha256": bundle["payload_tree_sha256"],
        "file_count": len(bundle["all_entries"]),
        "acceptance": summarize_acceptance_metadata(final_acceptance_metadata),
        "deploy": deploy_metadata_summary(target_dir, cid, final_deploy_metadata),
        "actions": actions,
        "next_actions": next_actions_for_client_publish(cid),
    }


def diff_client_bundle(
    root_dir: Path,
    client_id: str,
    *,
    target_dir_arg: str | None,
    from_bundle_arg: str | None = None,
    profiles: list[str] | None = None,
) -> dict[str, Any]:
    cid = validate_client_id(client_id)
    target_dir = resolve_client_publish_target_dir(root_dir, target_dir_arg)
    target_state = git_repo_state(target_dir)
    if not target_state.get("git"):
        raise RuntimeError(f"client-diff target must be a git repo: {target_dir}")

    if from_bundle_arg and profiles:
        raise RuntimeError("client-diff cannot combine --from-bundle with --profile.")

    actions: list[str] = []
    source_commit = git_head_commit(root_dir)
    temp_bundle: tempfile.TemporaryDirectory[str] | None = None

    try:
        bundle_dir, temp_bundle = _resolve_diff_bundle(
            root_dir, cid, from_bundle_arg, profiles, actions,
        )
        candidate_bundle = load_client_projection_bundle(bundle_dir, expected_client_id=cid)
        candidate_runtime_model = bundle_runtime_model(candidate_bundle)
        paths = _publish_paths_payload(target_dir, cid)
        actions.append(f"compare-current: {repo_rel(target_dir, paths['current_dir'])}")
        current_bundle, current_runtime_model = _load_current_diff_bundle(paths["current_dir"], cid)
        file_changes, projection_changes, runtime_changes = _diff_candidate_against_current(
            current_bundle, current_runtime_model, candidate_bundle, candidate_runtime_model,
        )
        actual_publish_metadata = _optional_json(paths["publish_metadata_path"])
        actual_acceptance_metadata = _optional_json(paths["acceptance_metadata_path"])
        expected_publish_metadata = build_client_publish_metadata(
            candidate_bundle,
            client_id=cid,
            source_commit=source_commit,
        )
        publish_metadata = diff_publish_metadata(actual_publish_metadata, expected_publish_metadata)
        changed = not bundle_matches_publish_target(candidate_bundle, paths["current_dir"], paths["publish_metadata_path"])

        return _client_diff_payload(
            cid=cid,
            target_dir=target_dir,
            client_root=paths["client_root"],
            current_dir=paths["current_dir"],
            bundle_dir=bundle_dir,
            changed=changed,
            source_commit=source_commit,
            candidate_bundle=candidate_bundle,
            current_bundle=current_bundle,
            file_changes=file_changes,
            actual_acceptance_metadata=actual_acceptance_metadata,
            projection_changes=projection_changes,
            runtime_changes=runtime_changes,
            publish_metadata=publish_metadata,
            actions=actions,
        )
    finally:
        if temp_bundle is not None:
            temp_bundle.cleanup()


def _resolve_diff_bundle(
    root_dir: Path,
    cid: str,
    from_bundle_arg: str | None,
    profiles: list[str] | None,
    actions: list[str],
) -> tuple[Path, "tempfile.TemporaryDirectory[str] | None"]:
    if from_bundle_arg:
        bundle_dir = resolve_client_publish_bundle_dir(root_dir, from_bundle_arg)
        actions.append(f"use-bundle: {repo_rel(root_dir, bundle_dir)}")
        return bundle_dir, None
    temp_bundle = tempfile.TemporaryDirectory(prefix=f".skillbox-client-diff-{cid}-")
    bundle_dir = Path(temp_bundle.name) / "bundle"
    project_client_bundle(
        root_dir,
        cid,
        profiles=profiles,
        output_dir_arg=str(bundle_dir),
        dry_run=False,
        force=True,
    )
    actions.append(f"build-bundle: {cid}")
    return bundle_dir, temp_bundle


def _load_current_diff_bundle(current_dir: Path, cid: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not current_dir.is_dir():
        return None, {}
    current_bundle = load_client_projection_bundle(current_dir, expected_client_id=cid)
    return current_bundle, bundle_runtime_model(current_bundle)


def _diff_candidate_against_current(
    current_bundle: dict[str, Any] | None,
    current_runtime_model: dict[str, Any],
    candidate_bundle: dict[str, Any],
    candidate_runtime_model: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    current_entries = current_bundle["all_entries"] if current_bundle is not None else []
    file_changes = diff_file_entries(current_entries, candidate_bundle["all_entries"])
    projection_changes = diff_projection_metadata(
        current_bundle["projection"] if current_bundle is not None else None,
        candidate_bundle["projection"],
    )
    runtime_changes = diff_runtime_models(current_runtime_model, candidate_runtime_model)
    return file_changes, projection_changes, runtime_changes


def _client_diff_payload(
    *,
    cid: str,
    target_dir: Path,
    client_root: Path,
    current_dir: Path,
    bundle_dir: Path,
    changed: bool,
    source_commit: str | None,
    candidate_bundle: dict[str, Any],
    current_bundle: dict[str, Any] | None,
    file_changes: dict[str, Any],
    actual_acceptance_metadata: dict[str, Any] | None,
    projection_changes: dict[str, Any],
    runtime_changes: dict[str, Any],
    publish_metadata: dict[str, Any],
    actions: list[str],
) -> dict[str, Any]:
    return {
        "client_id": cid,
        "target_dir": str(target_dir),
        "client_root": str(client_root),
        "current_dir": str(current_dir),
        "bundle_dir": str(bundle_dir),
        "changed": changed,
        "source_commit": source_commit,
        "candidate": _diff_bundle_summary(candidate_bundle, present=True),
        "current": _diff_bundle_summary(current_bundle, present=current_bundle is not None),
        "summary": file_changes["summary"],
        "files": {
            "added": file_changes["added"],
            "removed": file_changes["removed"],
            "changed": file_changes["changed"],
        },
        "acceptance": summarize_acceptance_metadata(actual_acceptance_metadata),
        "projection_changes": projection_changes,
        "runtime_changes": runtime_changes,
        "publish_metadata": publish_metadata,
        "actions": actions,
        "next_actions": next_actions_for_client_diff(cid, target_dir),
    }


def _diff_bundle_summary(bundle: dict[str, Any] | None, *, present: bool) -> dict[str, Any]:
    if bundle is None:
        return {
            "present": present,
            "payload_tree_sha256": None,
            "file_count": 0,
            "active_profiles": [],
            "overlay_mode": None,
        }
    return {
        "present": present,
        "payload_tree_sha256": bundle["payload_tree_sha256"],
        "file_count": len(bundle["all_entries"]),
        "active_profiles": bundle["projection"].get("active_profiles", []),
        "overlay_mode": bundle["projection"].get("overlay_mode"),
    }


def _ensure_client_open_bundle_is_separate(bundle_dir: Path, output_dir: Path) -> None:
    for source_path, target_path in ((bundle_dir, output_dir), (output_dir, bundle_dir)):
        try:
            target_path.relative_to(source_path)
            raise RuntimeError(
                "client-open --from-bundle requires an output directory separate from the bundle directory."
            )
        except ValueError:
            pass


def _write_client_open_mcp_config(
    root_dir: Path,
    output_dir: Path,
    selected_mcp_configs: dict[str, Any],
) -> str:
    mcp_config_path = output_dir / MCP_CONFIG_REL
    mcp_changed = write_json_file(mcp_config_path, {"mcpServers": selected_mcp_configs})
    return f"{'write-file' if mcp_changed else 'keep-file'}: {repo_rel(root_dir, mcp_config_path)}"


def _open_client_surface_from_bundle(
    root_dir: Path,
    cid: str,
    output_dir: Path,
    from_bundle_arg: str,
) -> tuple[dict[str, Any], int]:
    from .workflows import selected_mcp_server_configs

    bundle_dir = resolve_client_publish_bundle_dir(root_dir, from_bundle_arg)
    bundle = load_client_projection_bundle(bundle_dir, expected_client_id=cid)
    _ensure_client_open_bundle_is_separate(bundle_dir, output_dir)

    actions = [f"use-bundle: {repo_rel(root_dir, bundle_dir)}"]
    actions.extend(
        prepare_client_projection_output_dir(
            root_dir,
            output_dir,
            dry_run=False,
            force=True,
        )
    )
    replace_directory_from_bundle(bundle_dir, output_dir, temp_prefix=".skillbox-client-open-")
    actions.append(f"materialize-bundle: {repo_rel(root_dir, output_dir)}")

    filtered_model = bundle_runtime_model(bundle)
    actions.extend(generate_skill_context(filtered_model, root_dir, dry_run=False, output_dir=output_dir))
    actions.extend(sync_context(filtered_model, root_dir, dry_run=False, context_dir=output_dir))
    selected_mcp_configs, mcp_servers = selected_mcp_server_configs(root_dir, filtered_model)
    actions.append(_write_client_open_mcp_config(root_dir, output_dir, selected_mcp_configs))

    payload = {
        "client_id": cid,
        "output_dir": str(output_dir),
        "active_profiles": filtered_model.get("active_profiles", []),
        "active_clients": filtered_model.get("active_clients", []),
        "payload_tree_sha256": bundle["payload_tree_sha256"],
        "file_count": len(bundle["payload_entries"]),
        "mcp_servers": mcp_servers,
        "focus": {
            "status": "skip",
            "step_names": [],
            "summary": {"mode": "bundle", "bundle_dir": str(bundle_dir)},
        },
        "actions": actions,
        "next_actions": next_actions_for_client_open(cid),
    }
    return payload, EXIT_OK


def _client_open_focus_args(cid: str, profiles: list[str] | None, output_dir: Path) -> list[str]:
    profile_args = [arg for profile in profiles or [] for arg in ("--profile", profile)]
    return [
        "focus",
        cid,
        *profile_args,
        "--context-dir",
        str(output_dir),
        "--format",
        "json",
    ]


def _client_open_filtered_model(
    root_dir: Path,
    cid: str,
    profiles: list[str] | None,
) -> dict[str, Any]:
    model = build_runtime_model(root_dir)
    return filter_model(
        model,
        normalize_active_profiles(profiles or []),
        normalize_active_clients(model, [cid]),
    )


def _client_open_focus_actions(focus_payload: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    for step in focus_payload.get("steps") or []:
        detail = step.get("detail") or {}
        step_actions = detail.get("actions")
        if isinstance(step_actions, list):
            actions.extend(str(item) for item in step_actions if str(item).strip())
    return actions


def _open_client_surface_projected(
    root_dir: Path,
    cid: str,
    output_dir: Path,
    profiles: list[str] | None,
) -> tuple[dict[str, Any], int]:
    from .workflows import run_manage_json_command, selected_mcp_server_configs

    project_payload = project_client_bundle(
        root_dir,
        cid,
        profiles=profiles,
        output_dir_arg=str(output_dir),
        dry_run=False,
        force=True,
    )

    focus_code, focus_payload = run_manage_json_command(root_dir, _client_open_focus_args(cid, profiles, output_dir))
    if focus_code not in (EXIT_OK, EXIT_DRIFT):
        error_payload = focus_payload.get("error") or {}
        message = str(error_payload.get("message") or "").strip() or f"client-open focus failed for {cid}"
        raise RuntimeError(message)

    filtered_model = _client_open_filtered_model(root_dir, cid, profiles)
    sandbox_context_actions = generate_skill_context(filtered_model, root_dir, dry_run=False, output_dir=output_dir)
    selected_mcp_configs, mcp_servers = selected_mcp_server_configs(root_dir, filtered_model)

    actions = list(project_payload.get("actions") or [])
    actions.extend(_client_open_focus_actions(focus_payload))
    actions.extend(sandbox_context_actions)
    actions.append(_write_client_open_mcp_config(root_dir, output_dir, selected_mcp_configs))

    payload = {
        "client_id": cid,
        "output_dir": str(output_dir),
        "active_profiles": filtered_model.get("active_profiles", []),
        "active_clients": filtered_model.get("active_clients", []),
        "payload_tree_sha256": project_payload["payload_tree_sha256"],
        "file_count": project_payload["file_count"],
        "mcp_servers": mcp_servers,
        "focus": {
            "status": "warn" if focus_code == EXIT_DRIFT else "ok",
            "step_names": [str(step.get("step")) for step in focus_payload.get("steps") or []],
            "summary": focus_payload.get("summary") or {},
        },
        "actions": actions,
        "next_actions": next_actions_for_client_open(cid),
    }
    return payload, focus_code


def open_client_surface(
    root_dir: Path,
    client_id: str,
    *,
    profiles: list[str] | None = None,
    output_dir_arg: str | None = None,
    from_bundle_arg: str | None = None,
) -> tuple[dict[str, Any], int]:
    cid = validate_client_id(client_id)
    output_dir = resolve_client_open_output_dir(root_dir, cid, output_dir_arg)
    if from_bundle_arg and profiles:
        raise RuntimeError("client-open cannot combine --from-bundle with --profile.")
    if from_bundle_arg:
        return _open_client_surface_from_bundle(root_dir, cid, output_dir, from_bundle_arg)
    return _open_client_surface_projected(root_dir, cid, output_dir, profiles)


def extract_bundle_to_target(bundle_path: Path, target_root: Path, skill_name: str) -> str:
    ensure_directory(target_root, dry_run=False)
    install_dir = target_root / skill_name

    bundle_members(bundle_path, expected_skill_name=skill_name)
    with tempfile.TemporaryDirectory(prefix=f".skillbox-{skill_name}-", dir=target_root) as tmpdir:
        temp_root = Path(tmpdir)
        with zipfile.ZipFile(bundle_path, "r") as archive:
            archive.extractall(temp_root)

        extracted_dir = temp_root / skill_name
        if not extracted_dir.is_dir():
            raise RuntimeError(f"Bundle {bundle_path} did not create {skill_name}/ after extraction")

        remove_path(install_dir)
        shutil.move(str(extracted_dir), str(install_dir))

    tree_sha = directory_tree_sha256(install_dir)
    if tree_sha is None:
        raise RuntimeError(f"Failed to hash installed skill directory {install_dir}")
    return tree_sha
