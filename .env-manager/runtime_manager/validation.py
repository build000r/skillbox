from __future__ import annotations

from datetime import datetime as DateTime, timezone

from .shared import *

# Stable error codes from the local_runtime_core_cutover shared contract
# (shared.md:245-254). Re-exported here so consumers that already pull in
# the validation module get the full set without also importing
# scripts.lib.runtime_model.
from lib.runtime_model import (  # noqa: E402
    LOCAL_RUNTIME_ENV_BRIDGE_FAILED,
    LOCAL_RUNTIME_ENV_OUTPUT_MISSING,
    LOCAL_RUNTIME_PROFILE_UNKNOWN,
    LOCAL_RUNTIME_START_BLOCKED,
    LOCAL_RUNTIME_SERVICE_DEFERRED,
    LOCAL_RUNTIME_MODE_UNSUPPORTED,
    LOCAL_RUNTIME_COVERAGE_GAP,
    LOCAL_RUNTIME_ERROR_CODES,
    LOCAL_RUNTIME_START_MODES,
    PARITY_LEDGER_ACTIONS,
    PARITY_OWNERSHIP_STATES,
    CANONICAL_RUNTIME_RECORDS,
    LocalRuntimeContractError,
    PROJECT_KIND_IOS,
    VALID_PROJECT_KINDS,
    IOS_COMMAND_LANES,
    # Runtime-id slug grammar (security: path-join footgun). Re-exported here
    # so consumers that already pull in the validation module get the stable
    # RUNTIME_ID_INVALID code + validator without also importing
    # scripts.lib.runtime_model directly. Enforcement runs inside
    # build_runtime_model; this re-export is the shared seam.
    RUNTIME_ID_INVALID,
    RUNTIME_ID_PATTERN,
    RUNTIME_ID_PATTERN_TEXT,
    RuntimeIdValidationError,
    validate_runtime_id,
)

VALID_INGRESS_ROUTE_LISTENERS = {"public", "private"}
VALID_INGRESS_ROUTE_MATCHES = {"exact", "prefix"}
CLIENT_SHARED_SKILLS_REL = "../_shared/skills"
VENDORED_SHARED_SKILLS_ESCAPE_HATCH = "allow_vendored_shared_skills"
SKILL_FORGE_HOOK_MISSING = "SKILL_FORGE_HOOK_MISSING"
SKILL_FORGE_STALE = "SKILL_FORGE_STALE"
SKILL_FORGE_PENDING = "SKILL_FORGE_PENDING"
SKILL_FORGE_UNSCORED = "SKILL_FORGE_UNSCORED"
SKILL_FORGE_UNSCORED_THRESHOLD = 3


def _looks_like_ingress_origin(raw_value: Any) -> bool:
    value = str(raw_value or "").strip()
    if not value:
        return False
    parsed = urllib.parse.urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_active_profiles(raw_profiles: list[str] | None) -> set[str]:
    active_profiles = {value.strip() for value in raw_profiles or [] if value and value.strip()}
    active_profiles.add("core")
    return active_profiles


def _is_int_port(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_active_clients(model: dict[str, Any], raw_clients: list[str] | None) -> set[str]:
    requested_clients = {value.strip() for value in raw_clients or [] if value and value.strip()}
    available_clients = {
        str(client.get("id", "")).strip()
        for client in model.get("clients") or []
        if str(client.get("id", "")).strip()
    }
    default_client = str((model.get("selection") or {}).get("default_client") or "").strip()
    if not requested_clients and default_client:
        requested_clients.add(default_client)

    unknown_clients = sorted(requested_clients - available_clients)
    if unknown_clients:
        raise ValidationError(
            "unknown_client",
            "Unknown runtime client(s): "
            + ", ".join(unknown_clients)
            + ". Available clients: "
            + (", ".join(sorted(available_clients)) or "(none)"),
            context={
                "unknown": unknown_clients,
                "available": sorted(available_clients),
            },
        )

    return requested_clients


def item_matches_profiles(item: dict[str, Any], active_profiles: set[str]) -> bool:
    item_profiles = {
        str(value).strip()
        for value in item.get("profiles") or []
        if str(value).strip()
    }
    if not item_profiles:
        return True
    if not item_profiles.isdisjoint(active_profiles):
        return True
    if "local-all" in item_profiles and any(profile.startswith("local-") for profile in active_profiles):
        return True
    if "local-all" in active_profiles and any(profile.startswith("local-") for profile in item_profiles):
        return True
    return False


def parity_ledger_item_matches_profiles(item: dict[str, Any], active_profiles: set[str]) -> bool:
    """Scope parity rows by explicit profiles or intended_profiles.

    Client overlays usually leave ``profiles`` empty on parity-ledger rows and
    instead declare ``intended_profiles``. Scoped doctor/status/focus runs need
    that field to behave like profile scope; otherwise unrelated client/profile
    rows leak into filtered models and produce false coverage gaps.
    """
    item_profiles = {
        str(value).strip()
        for value in item.get("profiles") or []
        if str(value).strip()
    }
    if not item_profiles:
        item_profiles = {
            str(value).strip()
            for value in item.get("intended_profiles") or []
            if str(value).strip()
        }
    if not item_profiles:
        return True
    if not item_profiles.isdisjoint(active_profiles):
        return True
    if "local-all" in item_profiles and any(profile.startswith("local-") for profile in active_profiles):
        return True
    if "local-all" in active_profiles and any(profile.startswith("local-") for profile in item_profiles):
        return True
    return False


def item_matches_clients(item: dict[str, Any], active_clients: set[str]) -> bool:
    item_client = str(item.get("client", "")).strip()
    if not item_client:
        return True
    return item_client in active_clients


FILTER_MODEL_SCOPED_SECTIONS = (
    "repos",
    "artifacts",
    "env_files",
    "skills",
    "tasks",
    "services",
    "logs",
    "checks",
    "bridges",
    "service_mode_commands",
    "ingress_routes",
)


def _model_item_id(item: dict[str, Any]) -> str:
    return str(item.get("id", "")).strip()


def raw_task_dependency_ids(task: dict[str, Any]) -> list[str]:
    return unique_string_field_values(task, "depends_on")


def raw_service_bootstrap_task_ids(service: dict[str, Any]) -> list[str]:
    return unique_string_field_values(service, "bootstrap_tasks")


def _model_item_in_scope(item: dict[str, Any], active_profiles: set[str], active_clients: set[str]) -> bool:
    return item_matches_profiles(item, active_profiles) and item_matches_clients(item, active_clients)


def _scoped_model_items(
    model: dict[str, Any],
    section: str,
    active_profiles: set[str],
    active_clients: set[str],
) -> list[dict[str, Any]]:
    return [
        copy.deepcopy(item)
        for item in model.get(section) or []
        if _model_item_in_scope(item, active_profiles, active_clients)
    ]


def _scoped_parity_ledger(
    model: dict[str, Any],
    active_profiles: set[str],
    active_clients: set[str],
) -> list[dict[str, Any]]:
    return [
        copy.deepcopy(item)
        for item in model.get("parity_ledger") or []
        if parity_ledger_item_matches_profiles(item, active_profiles)
        and item_matches_clients(item, active_clients)
    ]


def _initial_filtered_model(
    model: dict[str, Any],
    active_profiles: set[str],
    active_clients: set[str],
) -> dict[str, Any]:
    filtered_model = dict(model)
    filtered_model["active_profiles"] = sorted(active_profiles)
    filtered_model["active_clients"] = sorted(active_clients)
    filtered_model["clients"] = [
        copy.deepcopy(client)
        for client in model.get("clients") or []
        if not active_clients or _model_item_id(client) in active_clients
    ]
    for section in FILTER_MODEL_SCOPED_SECTIONS:
        filtered_model[section] = _scoped_model_items(model, section, active_profiles, active_clients)
    filtered_model["parity_ledger"] = _scoped_parity_ledger(model, active_profiles, active_clients)
    return filtered_model


def _tasks_by_id(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_model_item_id(task): task for task in model.get("tasks") or [] if _model_item_id(task)}


def _include_filtered_task(
    *,
    task_id: str,
    tasks_by_id: dict[str, dict[str, Any]],
    filtered_model: dict[str, Any],
    included_task_ids: set[str],
    active_profiles: set[str],
    active_clients: set[str],
    visiting: set[str],
) -> None:
    task = tasks_by_id.get(task_id)
    if task is None or task_id in visiting:
        return
    if not _model_item_in_scope(task, active_profiles, active_clients):
        return
    visiting.add(task_id)
    for dependency_id in raw_task_dependency_ids(task):
        _include_filtered_task(
            task_id=dependency_id,
            tasks_by_id=tasks_by_id,
            filtered_model=filtered_model,
            included_task_ids=included_task_ids,
            active_profiles=active_profiles,
            active_clients=active_clients,
            visiting=visiting,
        )
    visiting.discard(task_id)
    if task_id not in included_task_ids:
        filtered_model["tasks"].append(copy.deepcopy(task))
        included_task_ids.add(task_id)


def _include_filtered_task_graph(
    model: dict[str, Any],
    filtered_model: dict[str, Any],
    active_profiles: set[str],
    active_clients: set[str],
) -> set[str]:
    included_task_ids = {_model_item_id(task) for task in filtered_model["tasks"] if _model_item_id(task)}
    tasks_by_id = _tasks_by_id(model)
    include_kwargs = {
        "tasks_by_id": tasks_by_id,
        "filtered_model": filtered_model,
        "included_task_ids": included_task_ids,
        "active_profiles": active_profiles,
        "active_clients": active_clients,
    }
    for service in filtered_model["services"]:
        for task_id in raw_service_bootstrap_task_ids(service):
            _include_filtered_task(task_id=task_id, visiting=set(), **include_kwargs)
    for task in list(filtered_model["tasks"]):
        for dependency_id in raw_task_dependency_ids(task):
            _include_filtered_task(task_id=dependency_id, visiting=set(), **include_kwargs)
    return included_task_ids


def _prune_filtered_task_references(filtered_model: dict[str, Any], included_task_ids: set[str]) -> None:
    for service in filtered_model["services"]:
        service["bootstrap_tasks"] = [
            task_id for task_id in raw_service_bootstrap_task_ids(service)
            if task_id in included_task_ids
        ]
    for task in filtered_model["tasks"]:
        task["depends_on"] = [
            dependency_id for dependency_id in raw_task_dependency_ids(task)
            if dependency_id in included_task_ids
        ]


def _required_reference_ids(items: list[dict[str, Any]], field: str) -> set[str]:
    return {str(item[field]) for item in items if item.get(field)}


def _filtered_required_refs(filtered_model: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    required_repo_ids = (
        _required_reference_ids(filtered_model["services"], "repo")
        | _required_reference_ids(filtered_model["tasks"], "repo")
    )
    required_artifact_ids = _required_reference_ids(filtered_model["services"], "artifact")
    required_log_ids = (
        _required_reference_ids(filtered_model["services"], "log")
        | _required_reference_ids(filtered_model["tasks"], "log")
    )
    return required_repo_ids, required_artifact_ids, required_log_ids


def _append_required_model_items(
    *,
    model: dict[str, Any],
    filtered_model: dict[str, Any],
    section: str,
    required_ids: set[str],
) -> None:
    included_ids = {_model_item_id(item) for item in filtered_model[section] if _model_item_id(item)}
    for item in model.get(section) or []:
        item_id = _model_item_id(item)
        if item_id and item_id in required_ids and item_id not in included_ids:
            filtered_model[section].append(copy.deepcopy(item))
            included_ids.add(item_id)


def _include_required_runtime_refs(model: dict[str, Any], filtered_model: dict[str, Any]) -> None:
    required_repo_ids, required_artifact_ids, required_log_ids = _filtered_required_refs(filtered_model)
    _append_required_model_items(
        model=model, filtered_model=filtered_model, section="repos", required_ids=required_repo_ids,
    )
    _append_required_model_items(
        model=model, filtered_model=filtered_model, section="artifacts", required_ids=required_artifact_ids,
    )
    _append_required_model_items(
        model=model, filtered_model=filtered_model, section="logs", required_ids=required_log_ids,
    )


def filter_model(model: dict[str, Any], active_profiles: set[str], active_clients: set[str]) -> dict[str, Any]:
    if not active_profiles and not active_clients:
        return model
    filtered_model = _initial_filtered_model(model, active_profiles, active_clients)
    included_task_ids = _include_filtered_task_graph(
        model, filtered_model, active_profiles, active_clients,
    )
    _prune_filtered_task_references(filtered_model, included_task_ids)
    _include_required_runtime_refs(model, filtered_model)
    return filtered_model


def _lockfile_skill_entries(lock_payload: dict[str, Any]) -> list[dict[str, Any]]:
    skills = lock_payload.get("skills") or []
    if not isinstance(skills, list):
        raise ValidationError("runtime_error", "Lockfile field 'skills' must be a list")
    for item in skills:
        if not isinstance(item, dict):
            raise ValidationError("runtime_error", "Lockfile skill entries must be objects")
    return skills


def _lockfile_skill_name(item: dict[str, Any], mapping: dict[str, dict[str, Any]]) -> str:
    name = str(item.get("name", "")).strip()
    if not name:
        raise ValidationError("runtime_error", "Lockfile skill entries must include a non-empty name")
    if name in mapping:
        raise ValidationError(
            "runtime_error",
            f"Lockfile contains duplicate skill entry {name!r}",
            context={"skill": name},
        )
    return name


def _lockfile_targets_by_id(name: str, item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    targets = item.get("targets") or []
    if not isinstance(targets, list):
        raise ValidationError(
            "runtime_error",
            f"Lockfile skill {name!r} has a non-list targets field",
            context={"skill": name},
        )

    targets_by_id: dict[str, dict[str, Any]] = {}
    for target in targets:
        if not isinstance(target, dict):
            raise ValidationError(
                "runtime_error",
                f"Lockfile skill {name!r} contains a non-object target entry",
                context={"skill": name},
            )
        target_id = str(target.get("id", "")).strip()
        if not target_id:
            raise ValidationError(
                "runtime_error",
                f"Lockfile skill {name!r} contains a target without an id",
                context={"skill": name},
            )
        if target_id in targets_by_id:
            raise ValidationError(
                "runtime_error",
                f"Lockfile skill {name!r} contains duplicate target {target_id!r}",
                context={"skill": name, "target": target_id},
            )
        targets_by_id[target_id] = target
    return targets_by_id


def lock_skill_map(lock_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for item in _lockfile_skill_entries(lock_payload):
        name = _lockfile_skill_name(item, mapping)
        targets_by_id = _lockfile_targets_by_id(name, item)
        mapping[name] = item | {"targets_by_id": targets_by_id}

    return mapping


def _collect_bundle_records(bundle_dir: Path) -> dict[str, dict[str, Any]]:
    bundles: dict[str, dict[str, Any]] = {}
    if not bundle_dir.is_dir():
        return bundles
    for bundle_path in sorted(bundle_dir.glob("*.skill")):
        bundles[bundle_path.stem] = bundle_metadata(bundle_path, expected_skill_name=bundle_path.stem)
    return bundles


def _load_skill_lock(lock_path: Path) -> tuple[dict[str, Any] | None, str | None, dict[str, dict[str, Any]]]:
    lock_payload: dict[str, Any] | None = None
    lock_error: str | None = None
    lock_skills: dict[str, dict[str, Any]] = {}
    if lock_path.exists():
        try:
            lock_payload = load_json_file(lock_path)
            lock_skills = lock_skill_map(lock_payload)
        except RuntimeError as exc:
            lock_error = str(exc)
    return lock_payload, lock_error, lock_skills


def _inventory_skill_names(
    expected_skills: list[str],
    bundles: dict[str, dict[str, Any]],
    lock_skills: dict[str, dict[str, Any]],
) -> list[str]:
    skill_names = list(expected_skills)
    for extra_name in sorted(set(bundles) - set(skill_names)):
        skill_names.append(extra_name)
    for lock_name in sorted(set(lock_skills) - set(skill_names)):
        skill_names.append(lock_name)
    return skill_names


def _target_states(skillset: dict[str, Any]) -> list[dict[str, Any]]:
    target_states: list[dict[str, Any]] = []
    for target in skillset.get("install_targets") or []:
        target_root = Path(str(target["host_path"]))
        target_states.append(
            {
                "id": target["id"],
                "path": str(target["path"]),
                "host_path": str(target_root),
                "present": target_root.exists(),
            }
        )
    return target_states


def _bundle_state(
    bundle_record: dict[str, Any] | None,
    lock_record: dict[str, Any] | None,
    lock_payload: dict[str, Any] | None,
) -> str:
    if bundle_record is None:
        return "missing"
    if bundle_record and lock_record:
        if (
            lock_record.get("bundle_sha256") == bundle_record["bundle_sha256"]
            and lock_record.get("bundle_tree_sha256") == bundle_record["bundle_tree_sha256"]
        ):
            return "ok"
        return "drift"
    if lock_payload:
        return "untracked"
    return "present"


def _target_state(
    install_dir: Path,
    install_tree_sha: str | None,
    target_lock: dict[str, Any] | None,
    lock_payload: dict[str, Any] | None,
) -> str:
    target_state = "present" if install_dir.exists() else "missing"
    if target_lock:
        if install_tree_sha is None:
            return "missing"
        if target_lock.get("tree_sha256") == install_tree_sha:
            return "ok"
        return "drift"
    if install_tree_sha is not None and lock_payload:
        return "untracked"
    return target_state


def _skill_target_entries(
    skill_name: str,
    target_states: list[dict[str, Any]],
    lock_record: dict[str, Any] | None,
    lock_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for target in target_states:
        install_dir = Path(target["host_path"]) / skill_name
        install_tree_sha = directory_tree_sha256(install_dir)
        target_lock = lock_record.get("targets_by_id", {}).get(target["id"]) if lock_record else None
        entries.append(
            {
                "id": target["id"],
                "path": str(target["path"]),
                "host_path": str(install_dir),
                "present": install_dir.exists(),
                "tree_sha256": install_tree_sha,
                "state": _target_state(install_dir, install_tree_sha, target_lock, lock_payload),
            }
        )
    return entries


def _skill_inventory_entry(
    skill_name: str,
    bundle_record: dict[str, Any] | None,
    lock_record: dict[str, Any] | None,
    lock_payload: dict[str, Any] | None,
    target_states: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "name": skill_name,
        "bundle_present": bundle_record is not None,
        "bundle_state": _bundle_state(bundle_record, lock_record, lock_payload),
        "bundle_sha256": bundle_record.get("bundle_sha256") if bundle_record else None,
        "bundle_tree_sha256": bundle_record.get("bundle_tree_sha256") if bundle_record else None,
        "targets": _skill_target_entries(skill_name, target_states, lock_record, lock_payload),
    }


def _skill_inventory_entries(
    skill_names: list[str],
    bundles: dict[str, dict[str, Any]],
    lock_skills: dict[str, dict[str, Any]],
    lock_payload: dict[str, Any] | None,
    target_states: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    for skill_name in skill_names:
        skills.append(
            _skill_inventory_entry(
                skill_name,
                bundles.get(skill_name),
                lock_skills.get(skill_name),
                lock_payload,
                target_states,
            )
        )
    return skills


def collect_skill_inventory(skillset: dict[str, Any]) -> dict[str, Any]:
    bundle_dir = Path(str(skillset["bundle_dir_host_path"]))
    manifest_path = Path(str(skillset["manifest_host_path"]))
    sources_config_path = Path(str(skillset["sources_config_host_path"]))
    lock_path = Path(str(skillset["lock_path_host_path"]))

    manifest_exists = manifest_path.is_file()
    sources_exists = sources_config_path.is_file()
    bundle_dir_exists = bundle_dir.is_dir()
    expected_skills = read_manifest_skills(manifest_path) if manifest_exists else []
    bundles = _collect_bundle_records(bundle_dir)
    missing_bundles = sorted(name for name in expected_skills if name not in bundles)
    extra_bundles = sorted(name for name in bundles if name not in expected_skills)
    lock_payload, lock_error, lock_skills = _load_skill_lock(lock_path)
    skill_names = _inventory_skill_names(expected_skills, bundles, lock_skills)
    target_states = _target_states(skillset)
    skills = _skill_inventory_entries(skill_names, bundles, lock_skills, lock_payload, target_states)

    return {
        "id": skillset["id"],
        "kind": skillset.get("kind", "packaged-skill-set"),
        "bundle_dir": str(skillset["bundle_dir"]),
        "bundle_dir_host_path": str(bundle_dir),
        "bundle_dir_exists": bundle_dir_exists,
        "manifest": str(skillset["manifest"]),
        "manifest_host_path": str(manifest_path),
        "manifest_exists": manifest_exists,
        "manifest_sha256": file_sha256(manifest_path) if manifest_exists else None,
        "sources_config": str(skillset["sources_config"]),
        "sources_config_host_path": str(sources_config_path),
        "sources_config_exists": sources_exists,
        "sources_config_sha256": file_sha256(sources_config_path) if sources_exists else None,
        "lock_path": str(skillset["lock_path"]),
        "lock_path_host_path": str(lock_path),
        "lock_present": lock_path.exists(),
        "lock_payload": lock_payload,
        "lock_error": lock_error,
        "expected_skills": expected_skills,
        "bundles": bundles,
        "missing_bundles": missing_bundles,
        "extra_bundles": extra_bundles,
        "install_targets": target_states,
        "skills": skills,
    }


def build_skill_lock(
    skillset: dict[str, Any],
    inventory: dict[str, Any],
    install_hashes: dict[str, dict[str, str]],
) -> dict[str, Any]:
    skills_payload: list[dict[str, Any]] = []
    for skill_name in inventory["expected_skills"]:
        bundle_record = inventory["bundles"][skill_name]
        target_payloads: list[dict[str, Any]] = []
        for target in skillset.get("install_targets") or []:
            install_dir = f"{str(target['path']).rstrip('/')}/{skill_name}"
            target_payloads.append(
                {
                    "id": target["id"],
                    "path": install_dir,
                    "tree_sha256": install_hashes[skill_name][target["id"]],
                }
            )

        skills_payload.append(
            {
                "name": skill_name,
                "bundle_file": bundle_record["filename"],
                "bundle_path": f"{str(skillset['bundle_dir']).rstrip('/')}/{bundle_record['filename']}",
                "bundle_sha256": bundle_record["bundle_sha256"],
                "bundle_tree_sha256": bundle_record["bundle_tree_sha256"],
                "targets": target_payloads,
            }
        )

    return {
        "version": LOCKFILE_VERSION,
        "id": skillset["id"],
        "kind": skillset.get("kind", "packaged-skill-set"),
        "bundle_dir": str(skillset["bundle_dir"]),
        "manifest": str(skillset["manifest"]),
        "manifest_sha256": inventory["manifest_sha256"],
        "sources_config": str(skillset["sources_config"]),
        "sources_config_sha256": inventory["sources_config_sha256"],
        "skills": skills_payload,
    }


def sync_skill_sets(model: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []
    for skillset in model["skills"]:
        if skillset.get("kind") == "skill-repo-set":
            continue
        inventory = collect_skill_inventory(skillset)
        actions.extend(_sync_packaged_skill_set(skillset, inventory, dry_run))
    return actions


def _sync_packaged_skill_set(
    skillset: dict[str, Any],
    inventory: dict[str, Any],
    dry_run: bool,
) -> list[str]:
    _ensure_skillset_sync_inputs(skillset, inventory)
    actions = _skillset_extra_bundle_actions(skillset, inventory)
    actions.extend(_ensure_skillset_target_dirs(skillset, dry_run))
    install_hashes, install_actions = _install_skillset_bundles(skillset, inventory, dry_run)
    actions.extend(install_actions)
    actions.append(_sync_skillset_lockfile(skillset, inventory, install_hashes, dry_run))
    return actions


def _ensure_skillset_sync_inputs(skillset: dict[str, Any], inventory: dict[str, Any]) -> None:
    missing_inputs = [
        field for field, present in (
            ("bundle_dir", inventory["bundle_dir_exists"]),
            ("manifest", inventory["manifest_exists"]),
            ("sources_config", inventory["sources_config_exists"]),
        )
        if not present
    ]
    if missing_inputs:
        raise ValidationError(
            "runtime_error",
            f"Skill set {skillset['id']} is missing required files: {', '.join(missing_inputs)}",
            context={"skillset": skillset["id"], "missing": missing_inputs},
        )
    if inventory["missing_bundles"]:
        raise ValidationError(
            "runtime_error",
            f"Skill set {skillset['id']} is missing bundles for: {', '.join(inventory['missing_bundles'])}",
            context={"skillset": skillset["id"], "missing_bundles": inventory["missing_bundles"]},
        )


def _skillset_extra_bundle_actions(skillset: dict[str, Any], inventory: dict[str, Any]) -> list[str]:
    if not inventory["extra_bundles"]:
        return []
    return [f"ignore-extra-bundles: {skillset['id']} -> {', '.join(inventory['extra_bundles'])}"]


def _ensure_skillset_target_dirs(skillset: dict[str, Any], dry_run: bool) -> list[str]:
    actions: list[str] = []
    for target in skillset.get("install_targets") or []:
        target_root = Path(str(target["host_path"]))
        ensure_directory(target_root, dry_run)
        actions.append(f"ensure-directory: {target_root}")
    return actions


def _install_skillset_bundles(
    skillset: dict[str, Any],
    inventory: dict[str, Any],
    dry_run: bool,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    from .publish import extract_bundle_to_target

    actions: list[str] = []
    install_hashes: dict[str, dict[str, str]] = {}
    for skill_name in inventory["expected_skills"]:
        install_hashes[skill_name] = {}
        bundle_record = inventory["bundles"][skill_name]
        bundle_path = Path(str(bundle_record["host_path"]))
        for target in skillset.get("install_targets") or []:
            target_root = Path(str(target["host_path"]))
            install_dir = target_root / skill_name
            if dry_run:
                actions.append(f"install-skill: {bundle_path} -> {install_dir}")
                continue
            install_hashes[skill_name][target["id"]] = extract_bundle_to_target(
                bundle_path=bundle_path,
                target_root=target_root,
                skill_name=skill_name,
            )
            actions.append(f"install-skill: {bundle_path} -> {install_dir}")
    return install_hashes, actions


def _sync_skillset_lockfile(
    skillset: dict[str, Any],
    inventory: dict[str, Any],
    install_hashes: dict[str, dict[str, str]],
    dry_run: bool,
) -> str:
    lock_path = Path(str(skillset["lock_path_host_path"]))
    if dry_run:
        return f"write-lockfile: {lock_path}"
    lock_payload = build_skill_lock(skillset, inventory, install_hashes)
    changed = write_json_file(lock_path, lock_payload)
    return f"{'write-lockfile' if changed else 'lockfile-unchanged'}: {lock_path}"


def _check_skillset_required_bundles(skillset: dict[str, Any], inventory: dict[str, Any]) -> list[str]:
    required_missing: list[str] = []
    for label, present, display_path in (
        ("bundle_dir", inventory["bundle_dir_exists"], inventory["bundle_dir_host_path"]),
        ("manifest", inventory["manifest_exists"], inventory["manifest_host_path"]),
        ("sources_config", inventory["sources_config_exists"], inventory["sources_config_host_path"]),
    ):
        if not present:
            required_missing.append(f"{skillset['id']}: missing {label} at {display_path}")
    return required_missing


def _check_skillset_bundle_drift(skillset: dict[str, Any], inventory: dict[str, Any]) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    if inventory["missing_bundles"]:
        failures.append(f"{skillset['id']}: missing bundles for {', '.join(inventory['missing_bundles'])}")
    if inventory["extra_bundles"]:
        warnings.append(f"{skillset['id']}: extra bundles present for {', '.join(inventory['extra_bundles'])}")
    return failures, warnings


def _check_skillset_lockfile(skillset: dict[str, Any], inventory: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Returns (lock_failures, lock_warnings)."""
    if inventory["lock_error"]:
        return [f"{skillset['id']}: {inventory['lock_error']}"], []
    if not inventory["lock_present"]:
        return [], [f"{skillset['id']}: lockfile missing at {inventory['lock_path_host_path']}"]

    lock_payload = inventory["lock_payload"] or {}
    failures = _lockfile_header_failures(skillset, inventory, lock_payload)

    indexed_lock = lock_skill_map(lock_payload)
    failures.extend(_lockfile_extra_skill_failures(skillset, inventory, indexed_lock))
    failures.extend(_lockfile_skill_record_failures(skillset, inventory, indexed_lock))
    return failures, []


def _lockfile_header_failures(
    skillset: dict[str, Any],
    inventory: dict[str, Any],
    lock_payload: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if lock_payload.get("version") != LOCKFILE_VERSION:
        failures.append(
            f"{skillset['id']}: lockfile version {lock_payload.get('version')!r} does not match {LOCKFILE_VERSION}"
        )
    if lock_payload.get("id") != skillset["id"]:
        failures.append(f"{skillset['id']}: lockfile id does not match the skill set id")
    if lock_payload.get("manifest_sha256") != inventory["manifest_sha256"]:
        failures.append(f"{skillset['id']}: lockfile manifest digest is stale")
    if lock_payload.get("sources_config_sha256") != inventory["sources_config_sha256"]:
        failures.append(f"{skillset['id']}: lockfile sources config digest is stale")
    return failures


def _lockfile_extra_skill_failures(
    skillset: dict[str, Any],
    inventory: dict[str, Any],
    indexed_lock: dict[str, Any],
) -> list[str]:
    expected_skill_names = set(inventory["expected_skills"])
    extras = sorted(set(indexed_lock) - expected_skill_names)
    if extras:
        return [f"{skillset['id']}: lockfile contains extra skills: {', '.join(extras)}"]
    return []


def _lockfile_skill_record_failures(
    skillset: dict[str, Any],
    inventory: dict[str, Any],
    indexed_lock: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    configured_targets = {target["id"] for target in skillset.get("install_targets") or []}
    for skill_name in inventory["expected_skills"]:
        lock_record = indexed_lock.get(skill_name)
        if lock_record is None:
            failures.append(f"{skillset['id']}: lockfile is missing skill {skill_name}")
            continue
        failures.extend(_lockfile_bundle_record_failures(skillset, inventory, lock_record, skill_name))
        failures.extend(_lockfile_target_failures(skillset, configured_targets, lock_record, skill_name))
    return failures


def _lockfile_bundle_record_failures(
    skillset: dict[str, Any],
    inventory: dict[str, Any],
    lock_record: dict[str, Any],
    skill_name: str,
) -> list[str]:
    bundle_record = inventory["bundles"].get(skill_name)
    if bundle_record is None:
        return []
    failures: list[str] = []
    if lock_record.get("bundle_sha256") != bundle_record["bundle_sha256"]:
        failures.append(f"{skillset['id']}: lockfile bundle digest is stale for {skill_name}")
    if lock_record.get("bundle_tree_sha256") != bundle_record["bundle_tree_sha256"]:
        failures.append(f"{skillset['id']}: lockfile bundle tree digest is stale for {skill_name}")
    return failures


def _lockfile_target_failures(
    skillset: dict[str, Any],
    configured_targets: set[str],
    lock_record: dict[str, Any],
    skill_name: str,
) -> list[str]:
    failures: list[str] = []
    lock_targets = lock_record.get("targets_by_id", {})
    target_extras = sorted(set(lock_targets) - configured_targets)
    if target_extras:
        failures.append(
            f"{skillset['id']}: lockfile contains unexpected targets for {skill_name}: {', '.join(target_extras)}"
        )
    missing_targets = sorted(configured_targets - set(lock_targets))
    if missing_targets:
        failures.append(
            f"{skillset['id']}: lockfile is missing targets for {skill_name}: {', '.join(missing_targets)}"
        )
    return failures


def _check_skillset_install_state(skillset: dict[str, Any], inventory: dict[str, Any]) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    for skill_entry in inventory["skills"]:
        bundle_state = skill_entry["bundle_state"]
        if bundle_state == "drift":
            failures.append(f"{skillset['id']}: bundle digest drift detected for {skill_entry['name']}")
        elif bundle_state == "untracked" and inventory["lock_present"]:
            failures.append(f"{skillset['id']}: bundle {skill_entry['name']} is not represented in the lockfile")
        for target in skill_entry["targets"]:
            if target["state"] == "drift":
                failures.append(f"{skillset['id']}: installed drift for {skill_entry['name']} in {target['id']}")
            elif target["state"] == "untracked":
                failures.append(f"{skillset['id']}: unmanaged install for {skill_entry['name']} in {target['id']}")
            elif target["state"] == "missing":
                warnings.append(f"{skillset['id']}: missing install for {skill_entry['name']} in {target['id']}")
    return failures, warnings


def validate_skill_locks_and_state(model: dict[str, Any]) -> list[CheckResult]:
    if not model["skills"]:
        return []

    bundle_failures: list[str] = []
    bundle_warnings: list[str] = []
    lock_failures: list[str] = []
    lock_warnings: list[str] = []
    install_failures: list[str] = []
    install_warnings: list[str] = []

    for skillset in model["skills"]:
        if skillset.get("kind") == "skill-repo-set":
            continue
        inventory = collect_skill_inventory(skillset)

        required_missing = _check_skillset_required_bundles(skillset, inventory)
        if required_missing:
            bundle_failures.extend(required_missing)
            continue

        bf, bw = _check_skillset_bundle_drift(skillset, inventory)
        bundle_failures.extend(bf)
        bundle_warnings.extend(bw)

        lf, lw = _check_skillset_lockfile(skillset, inventory)
        lock_failures.extend(lf)
        lock_warnings.extend(lw)

        if_, iw = _check_skillset_install_state(skillset, inventory)
        install_failures.extend(if_)
        install_warnings.extend(iw)

    return [
        _bucketed_check_result(
            "skill-bundle-state",
            "managed skill bundles do not satisfy the declared manifest",
            "managed skill bundle directory contains undeclared bundles",
            "managed skill bundle directories satisfy the declared manifests",
            bundle_failures, bundle_warnings,
        ),
        _bucketed_check_result(
            "skill-lock-state",
            "managed skill lockfiles are invalid or stale",
            "managed skill lockfiles have not been generated yet",
            "managed skill lockfiles match the current bundle and source manifests",
            lock_failures, lock_warnings,
        ),
        _bucketed_check_result(
            "skill-install-state",
            "installed skill directories drifted from the managed bundles",
            "managed skill installs are missing and can be created by sync",
            "managed skill installs match the lockfile and bundle contents",
            install_failures, install_warnings,
        ),
    ]


def _declared_skill_names(config: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for entry in config.get("skill_repos") or []:
        pick = entry.get("pick")
        if pick:
            names.update(str(item) for item in pick if str(item).strip())
            continue
        repo = str(entry.get("repo") or "").strip()
        if repo:
            names.add(repo.split("/")[-1] if "/" in repo else repo)
    return names


def _protected_planning_picks(entry: dict[str, Any], protected_names: set[str]) -> list[str]:
    picked_names = {
        str(item).strip()
        for item in entry.get("pick") or []
        if str(item).strip()
    }
    return sorted(picked_names & protected_names)


def _resolved_skill_repo_source_path(overlay_dir: Path, local_path: str) -> Path:
    source_path = Path(local_path).expanduser()
    if not source_path.is_absolute():
        source_path = overlay_dir / source_path
    return source_path


def _shared_client_planning_source_failure(
    *,
    client_id: str,
    index: int,
    protected_picks: list[str],
    source_path: Path,
    expected_shared_source: Path,
) -> str | None:
    protected_pick_label = ", ".join(protected_picks)
    if not source_path.exists() and not source_path.is_symlink():
        return (
            f"{client_id}: skill_repos[{index}] references missing local path "
            f"{repo_rel(DEFAULT_ROOT_DIR, source_path)} for protected planning skills "
            f"{protected_pick_label}; point it at {CLIENT_SHARED_SKILLS_REL} "
            f"or set {VENDORED_SHARED_SKILLS_ESCAPE_HATCH}: true when divergence is intentional"
        )

    if source_path.resolve() != expected_shared_source:
        return (
            f"{client_id}: skill_repos[{index}] sources protected planning skills "
            f"{protected_pick_label} from {repo_rel(DEFAULT_ROOT_DIR, source_path)}; "
            f"point it at {CLIENT_SHARED_SKILLS_REL} or set "
            f"{VENDORED_SHARED_SKILLS_ESCAPE_HATCH}: true when divergence is intentional"
        )

    return None


def _validate_shared_client_planning_sources(
    skillset: dict[str, Any],
    config_path: Path,
    config: dict[str, Any],
) -> list[str]:
    client_id = str(skillset.get("client", "")).strip()
    if not client_id:
        return []

    overlay_dir = config_path.parent
    expected_shared_source = (overlay_dir / CLIENT_SHARED_SKILLS_REL).resolve()
    protected_names = set(HARDENED_CLIENT_PLANNING_SKILLS)
    failures: list[str] = []

    for index, entry in enumerate(config.get("skill_repos") or []):
        local_path = str(entry.get("path") or "").strip()
        if not local_path:
            continue

        protected_picks = _protected_planning_picks(entry, protected_names)
        if not protected_picks:
            continue

        if bool(entry.get(VENDORED_SHARED_SKILLS_ESCAPE_HATCH)):
            continue

        failure = _shared_client_planning_source_failure(
            client_id=client_id,
            index=index,
            protected_picks=protected_picks,
            source_path=_resolved_skill_repo_source_path(overlay_dir, local_path),
            expected_shared_source=expected_shared_source,
        )
        if failure:
            failures.append(failure)

    return failures


def _build_effective_skill_owners(
    model: dict[str, Any],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """First pass: collect declared-skill names per skillset and the effective owner of each name."""
    declared_skills_by_skillset: dict[str, set[str]] = {}
    effective_skill_owner: dict[str, str] = {}
    for skillset in model["skills"]:
        if skillset.get("kind") != "skill-repo-set":
            continue
        sid = skillset["id"]
        config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
        if not config_path.is_file():
            continue
        try:
            config = load_skill_repos_config(config_path)
        except RuntimeError:
            continue
        declared = _declared_skill_names(config)
        declared_skills_by_skillset[sid] = declared
        for skill_name in declared:
            effective_skill_owner[skill_name] = sid
    return declared_skills_by_skillset, effective_skill_owner


def _collect_all_declared_skill_names(model: dict[str, Any]) -> set[str]:
    """Union of declared skill names across every skill-repo-set skillset."""
    all_declared: set[str] = set()
    for skillset in model["skills"]:
        if skillset.get("kind") != "skill-repo-set":
            continue
        config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
        if not config_path.is_file():
            continue
        try:
            config = load_skill_repos_config(config_path)
        except RuntimeError:
            continue
        all_declared.update(_declared_skill_names(config))
    return all_declared


def _validate_skill_install_targets(
    skillset: dict[str, Any],
    declared_skill_names_for_set: set[str],
    lock_skills_by_name: dict[str, dict[str, Any]],
    effective_skill_owner: dict[str, str],
) -> list[str]:
    sid = skillset["id"]
    install_failures: list[str] = []
    for skill_name in sorted(declared_skill_names_for_set):
        lock_record = lock_skills_by_name.get(skill_name)
        if lock_record is None:
            install_failures.append(f"{sid}: SKILL_NOT_INSTALLED: {skill_name} not in lockfile")
            continue
        if effective_skill_owner.get(skill_name) != sid:
            continue
        for target in skillset.get("install_targets") or []:
            install_dir = Path(str(target["host_path"])) / skill_name
            if not install_dir.is_dir():
                install_failures.append(
                    f"{sid}: SKILL_NOT_INSTALLED: {skill_name} missing in {target['id']}"
                )
                continue
            installed_sha = directory_tree_sha256(install_dir)
            lock_sha = lock_record.get("install_tree_sha")
            if lock_sha and installed_sha != lock_sha:
                install_failures.append(
                    f"{sid}: SKILL_INSTALL_STALE: {skill_name} in {target['id']} "
                    f"(installed tree differs from lock)"
                )
    return install_failures


def _collect_extra_installed_skills(
    skillset: dict[str, Any], all_declared: set[str],
) -> list[str]:
    sid = skillset["id"]
    installed_skill_names: set[str] = set()
    for target in skillset.get("install_targets") or []:
        target_root = Path(str(target["host_path"]))
        if target_root.is_dir():
            for child in target_root.iterdir():
                if child.is_dir() and (child / "SKILL.md").is_file():
                    installed_skill_names.add(child.name)
        break
    return [
        f"{sid}: SKILL_EXTRA_INSTALLED: {extra_name} is installed but not declared"
        for extra_name in sorted(installed_skill_names - all_declared)
    ]


def _validate_skillset_locks_and_installs(
    skillset: dict[str, Any],
    declared_skills_by_skillset: dict[str, set[str]],
    effective_skill_owner: dict[str, str],
    all_declared: set[str],
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Per-skillset validation; returns (config_failures, shared_source_failures, lock_failures,
    lock_warnings, install_failures, install_warnings) — but as 5 lists since shared_source is
    accumulated separately. Returns (config_failures, shared_source_failures, lock_failures,
    lock_warnings_or_install_failures, install_warnings)."""
    sid = skillset["id"]
    config_failures: list[str] = []
    shared_source_failures: list[str] = []
    lock_failures: list[str] = []
    lock_warnings: list[str] = []
    install_failures: list[str] = []
    install_warnings: list[str] = []

    config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
    lock_path = Path(str(skillset.get("lock_path_host_path", "")))

    if not config_path.is_file():
        config_failures.append(f"{sid}: skill_repos config missing at {config_path}")
        return config_failures, shared_source_failures, lock_failures, lock_warnings, install_failures, install_warnings

    try:
        config = load_skill_repos_config(config_path)
    except RuntimeError as exc:
        config_failures.append(f"{sid}: {exc}")
        return config_failures, shared_source_failures, lock_failures, lock_warnings, install_failures, install_warnings

    shared_source_failures.extend(
        _validate_shared_client_planning_sources(skillset, config_path, config)
    )

    config_sha = file_sha256(config_path)
    declared_skill_names_for_set = declared_skills_by_skillset.get(sid, set())

    if not lock_path.is_file():
        lock_warnings.append(f"{sid}: lockfile missing at {lock_path} — run sync")
        return config_failures, shared_source_failures, lock_failures, lock_warnings, install_failures, install_warnings

    try:
        lock_payload = load_json_file(lock_path)
    except RuntimeError as exc:
        lock_failures.append(f"{sid}: {exc}")
        return config_failures, shared_source_failures, lock_failures, lock_warnings, install_failures, install_warnings

    if lock_payload.get("version") != SKILL_REPOS_LOCKFILE_VERSION:
        lock_failures.append(
            f"{sid}: lockfile version {lock_payload.get('version')!r} "
            f"does not match {SKILL_REPOS_LOCKFILE_VERSION}"
        )
        return config_failures, shared_source_failures, lock_failures, lock_warnings, install_failures, install_warnings

    if lock_payload.get("config_sha") != config_sha:
        lock_failures.append(f"{sid}: config changed since last sync (config_sha mismatch)")

    lock_skills_by_name: dict[str, dict[str, Any]] = {}
    for skill_entry in lock_payload.get("skills") or []:
        name = str(skill_entry.get("name", ""))
        if name:
            lock_skills_by_name[name] = skill_entry

    install_failures.extend(_validate_skill_install_targets(
        skillset, declared_skill_names_for_set, lock_skills_by_name, effective_skill_owner,
    ))
    install_warnings.extend(_collect_extra_installed_skills(skillset, all_declared))
    return config_failures, shared_source_failures, lock_failures, lock_warnings, install_failures, install_warnings


def _bucketed_check_result(
    code: str,
    fail_msg: str,
    warn_msg: str,
    pass_msg: str,
    failures: list[str],
    warnings: list[str] | None = None,
) -> CheckResult:
    if failures:
        return CheckResult(status="fail", code=code, message=fail_msg, details={"issues": failures})
    if warnings:
        return CheckResult(status="warn", code=code, message=warn_msg, details={"issues": warnings})
    return CheckResult(status="pass", code=code, message=pass_msg)


def _build_skill_repo_results(
    config_failures: list[str],
    shared_source_failures: list[str],
    lock_failures: list[str],
    lock_warnings: list[str],
    install_failures: list[str],
    install_warnings: list[str],
) -> list[CheckResult]:
    return [
        _bucketed_check_result(
            "skill-repo-config",
            "skill repo config is invalid or missing",
            "skill repo configs are valid",
            "skill repo configs are valid",
            config_failures,
        ),
        _bucketed_check_result(
            "skill-repo-shared-source",
            "client planning skill bundles must use the shared source contract",
            "client planning skill bundles honor the shared source contract",
            "client planning skill bundles honor the shared source contract",
            shared_source_failures,
        ),
        _bucketed_check_result(
            "skill-repo-lock",
            "skill repo lockfiles are invalid or stale",
            "skill repo lockfiles need generation",
            "skill repo lockfiles match the current config",
            lock_failures,
            lock_warnings,
        ),
        _bucketed_check_result(
            "skill-repo-install",
            "installed skills drifted from declared repos",
            "extra skills installed that are not declared",
            "installed skills match declared repo state",
            install_failures,
            install_warnings,
        ),
    ]


def validate_skill_repo_sets(model: dict[str, Any]) -> list[CheckResult]:
    """Validate skill-repo-set skillsets using 2-layer drift detection."""
    from .distribution.doctor import validate_distribution_doctor_checks

    distribution_results = validate_distribution_doctor_checks(model)
    has_repo_sets = any(s.get("kind") == "skill-repo-set" for s in model["skills"])
    if not has_repo_sets:
        return distribution_results + validate_forge_health(model)

    declared_skills_by_skillset, effective_skill_owner = _build_effective_skill_owners(model)
    all_declared = _collect_all_declared_skill_names(model)

    config_failures: list[str] = []
    shared_source_failures: list[str] = []
    lock_failures: list[str] = []
    lock_warnings: list[str] = []
    install_failures: list[str] = []
    install_warnings: list[str] = []

    for skillset in model["skills"]:
        if skillset.get("kind") != "skill-repo-set":
            continue
        cf, ssf, lf, lw, if_, iw = _validate_skillset_locks_and_installs(
            skillset, declared_skills_by_skillset, effective_skill_owner, all_declared,
        )
        config_failures.extend(cf)
        shared_source_failures.extend(ssf)
        lock_failures.extend(lf)
        lock_warnings.extend(lw)
        install_failures.extend(if_)
        install_warnings.extend(iw)

    results = _build_skill_repo_results(
        config_failures, shared_source_failures, lock_failures,
        lock_warnings, install_failures, install_warnings,
    )
    return results + distribution_results + validate_forge_health(model)


GLOBAL_SKILL_CONTRACT_CODE = "global-skill-contract"


def _skill_scope_policy_path() -> Path:
    """Canonical location of the operator skill-scope policy.

    skill-scope.yaml lives in the private config repo (``skillbox-config``),
    located *relative to* the runtime root rather than at a hard-coded absolute
    (mirrors how machines.py resolves machines.yaml). We honour an explicit
    ``SKILLBOX_SKILL_SCOPE_FILE`` override, then fall back to the
    ``<runtime_root>/../skillbox-config`` and ``<repos_root>/skillbox-config``
    devbox layouts.
    """
    override = str(os.environ.get("SKILLBOX_SKILL_SCOPE_FILE") or "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    runtime_root = DEFAULT_ROOT_DIR
    for config_root in (
        runtime_root.parent / "skillbox-config",
        runtime_root.parent.parent / "skillbox-config",
    ):
        candidate = config_root / "skill-scope.yaml"
        if candidate.is_file():
            return candidate
    return runtime_root.parent / "skillbox-config" / "skill-scope.yaml"


def _rule_skill_names(rule: dict[str, Any]) -> list[str]:
    """The skills a rule declares, via the SAME accessor the runtime evaluates.

    The runtime reads a rule's skill list through
    ``skill_visibility._scope_rule_patterns``: ``skills`` OR ``patterns`` OR
    ``names`` (first non-empty wins), stripped of blanks. A rule authored with
    ``patterns:`` / ``names:`` instead of ``skills:`` is therefore fully live, so
    every lint that enumerates a rule's skills MUST read the same synonyms or it
    goes blind to those rules (false contract/precedence/overlay findings).

    We reuse the runtime's own accessor (lazy-imported to keep validation
    import-cycle-free, like ``_registry_id_resolved_paths``) so this can never
    diverge from what the evaluator sees. Falls back to the inline triple-OR if
    ``skill_visibility`` is somehow unavailable.
    """
    if not isinstance(rule, dict):
        return []
    try:
        from . import skill_visibility as sv  # noqa: PLC0415

        return sv._scope_rule_patterns(rule)
    except Exception:  # pragma: no cover - defensive: never break the lint
        raw = rule.get("skills") or rule.get("patterns") or rule.get("names") or []
        return [str(item).strip() for item in raw if str(item).strip()]


def _scope_allow_global_union(policy: dict[str, Any]) -> set[str]:
    """Union of every skill named by a rule with ``allow_global: true``."""
    union: set[str] = set()
    for rule in policy.get("rules") or []:
        if not isinstance(rule, dict) or not bool(rule.get("allow_global", False)):
            continue
        for name in _rule_skill_names(rule):
            if name:
                union.add(name)
    return union


def _scope_global_allowlist(policy: dict[str, Any]) -> set[str]:
    return {
        str(skill).strip()
        for skill in policy.get("global_allowlist") or []
        if str(skill).strip()
    }


def validate_global_skill_contract(
    policy: dict[str, Any],
    *,
    policy_path: str | None = None,
) -> list[CheckResult]:
    """Assert the global skill contract is internally consistent.

    The operator's ``skill-scope.yaml`` declares the always-global surface in
    two hand-synced places: the ``global_allowlist`` list (gates install-path
    patterns) and every rule carrying ``allow_global: true`` (carries the
    per-rule scope rationale). These are intentionally kept as separate lists --
    one is a flat allowlist consumed by the install planner, the other is the
    structured, commented rule set -- but they describe the *same* canonical
    global set and can silently drift apart.

    DECISION: rather than collapse to one list (which would lose the per-rule
    rationale the rules carry and that ``sbp`` reads), this lint keeps both lists
    and makes drift impossible by asserting they are EQUAL:

        global_allowlist  ==  union(skills of rules where allow_global: true)

    On drift it names exactly which skills are in one list but not the other and
    states the fix (edit the relevant ``allow_global`` rule and
    ``global_allowlist`` together). Mirrors the three sources reconciled in the
    sbp dispatcher contract: dispatcher core + named operator exceptions +
    mode-pack overlays, where the always-global set is exactly this union.
    """
    if not isinstance(policy, dict):
        return [
            CheckResult(
                status="pass",
                code=GLOBAL_SKILL_CONTRACT_CODE,
                message="no skill-scope policy to validate",
            )
        ]

    allowlist = _scope_global_allowlist(policy)
    allow_global_union = _scope_allow_global_union(policy)

    # An empty policy (no allowlist and no allow_global rules) is not drift; it
    # just means this policy does not declare a global surface.
    if not allowlist and not allow_global_union:
        return [
            CheckResult(
                status="pass",
                code=GLOBAL_SKILL_CONTRACT_CODE,
                message="skill-scope policy declares no global skill surface",
            )
        ]

    in_allowlist_only = sorted(allowlist - allow_global_union)
    in_rules_only = sorted(allow_global_union - allowlist)

    if in_allowlist_only or in_rules_only:
        issues: list[str] = []
        if in_allowlist_only:
            issues.append(
                "in global_allowlist but no allow_global rule grants them: "
                + ", ".join(in_allowlist_only)
            )
        if in_rules_only:
            issues.append(
                "granted by an allow_global rule but missing from global_allowlist: "
                + ", ".join(in_rules_only)
            )
        location = f" in {policy_path}" if policy_path else ""
        return [
            CheckResult(
                status="fail",
                code=GLOBAL_SKILL_CONTRACT_CODE,
                message=(
                    f"global skill contract drift{location}: global_allowlist must equal "
                    "the union of skills from all rules with allow_global: true. "
                    "Fix: edit the relevant allow_global rule and global_allowlist together "
                    "so they list the same skills."
                ),
                details={
                    "issues": issues,
                    "global_allowlist": sorted(allowlist),
                    "allow_global_union": sorted(allow_global_union),
                    "in_allowlist_only": in_allowlist_only,
                    "in_rules_only": in_rules_only,
                },
            )
        ]

    return [
        CheckResult(
            status="pass",
            code=GLOBAL_SKILL_CONTRACT_CODE,
            message=(
                "global_allowlist equals the union of allow_global rules "
                f"({len(allowlist)} operator skills)"
            ),
            details={"global_skills": sorted(allowlist)},
        )
    ]


def validate_global_skill_contract_file(
    policy_path: Path | str | None = None,
) -> list[CheckResult]:
    """Load skill-scope.yaml and run :func:`validate_global_skill_contract`.

    Convenience wrapper for doctor / CLI callers: resolves the canonical
    ``skill-scope.yaml`` (or an explicit path), parses it, and runs the
    consistency lint. A missing policy file is a pass (nothing to enforce); a
    parse failure is a fail.
    """
    resolved = Path(policy_path) if policy_path else _skill_scope_policy_path()
    if not resolved.is_file():
        return [
            CheckResult(
                status="pass",
                code=GLOBAL_SKILL_CONTRACT_CODE,
                message=f"no skill-scope policy found at {resolved}",
            )
        ]
    try:
        policy = load_yaml(resolved)
    except RuntimeError as exc:
        return [
            CheckResult(
                status="fail",
                code=GLOBAL_SKILL_CONTRACT_CODE,
                message=f"could not parse skill-scope policy at {resolved}: {exc}",
            )
        ]
    if not isinstance(policy, dict):
        return [
            CheckResult(
                status="fail",
                code=GLOBAL_SKILL_CONTRACT_CODE,
                message=f"skill-scope policy at {resolved} is not a mapping",
            )
        ]
    return validate_global_skill_contract(policy, policy_path=str(resolved))


OVERLAY_DECLARATION_CODE = "overlay-declaration"


def _policy_declared_overlays(policy: dict[str, Any]) -> set[str]:
    """Overlay names declared by a policy's top-level ``overlays:`` block.

    Accepts a list of mappings (``- name: marketing``), a list of bare strings
    (``- marketing``), or a mapping (``marketing: {description: ...}``) so the
    declaration shape stays forgiving while the validation stays strict.
    """
    declared: set[str] = set()
    raw = policy.get("overlays")
    if isinstance(raw, dict):
        for key in raw:
            name = str(key).strip()
            if name:
                declared.add(name)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item).strip()
            if name:
                declared.add(name)
    return declared


def _policy_rule_overlay_tags(policy: dict[str, Any]) -> dict[str, list[str]]:
    """Map each ``overlay:`` tag found in rules -> the rule ids carrying it."""
    tags: dict[str, list[str]] = {}
    for rule in policy.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        tag = str(rule.get("overlay") or "").strip()
        if not tag:
            continue
        rule_id = str(rule.get("id") or "").strip() or "(unnamed rule)"
        tags.setdefault(tag, []).append(rule_id)
    return tags


def validate_overlay_declarations(
    policy: dict[str, Any],
    *,
    policy_path: str | None = None,
) -> list[CheckResult]:
    """Assert every rule ``overlay:`` tag references a DECLARED overlay.

    Mode-pack overlays (layer 3 of the global skill contract) are now declared in
    a single top-level ``overlays:`` registry. Before this registry existed,
    overlay tags were enumerated from rules with no declaration point and no
    validation, so a typo (``overlay: marketng``) silently created a ghost
    overlay: a tag that no ``sbp overlay on`` could meaningfully match and that
    filtered nothing, failing closed without a word.

    This lint makes that drift loud. It compares the registry against the tags
    the rules actually carry:

        every rule overlay tag  in  declared overlays(`overlays:` block)

    On a tag with no declaration it FAILS, naming the offending tag, the rule(s)
    carrying it, and the declared list (the fix). An empty policy (no declared
    overlays and no rule overlay tags) is a PASS -- there is no overlay surface to
    enforce, mirroring ``validate_global_skill_contract``'s empty-policy posture.
    Declared-but-unused overlays are NOT a failure (a registry entry may be
    declared ahead of its rules); they are reported as advisory detail only.
    """
    if not isinstance(policy, dict):
        return [
            CheckResult(
                status="pass",
                code=OVERLAY_DECLARATION_CODE,
                message="no skill-scope policy to validate",
            )
        ]

    declared = _policy_declared_overlays(policy)
    rule_tags = _policy_rule_overlay_tags(policy)

    if not declared and not rule_tags:
        return [
            CheckResult(
                status="pass",
                code=OVERLAY_DECLARATION_CODE,
                message="skill-scope policy declares no overlay surface",
            )
        ]

    undeclared = sorted(tag for tag in rule_tags if tag not in declared)
    unused = sorted(name for name in declared if name not in rule_tags)

    if undeclared:
        offenders = {tag: rule_tags[tag] for tag in undeclared}
        offender_text = "; ".join(
            f"{tag} (rule(s): {', '.join(rule_ids)})"
            for tag, rule_ids in offenders.items()
        )
        location = f" in {policy_path}" if policy_path else ""
        return [
            CheckResult(
                status="fail",
                code=OVERLAY_DECLARATION_CODE,
                message=(
                    f"undeclared overlay tag(s){location}: {offender_text}. "
                    "Every rule overlay: tag must reference an overlay declared in the "
                    "skill-scope.yaml `overlays:` block. "
                    f"Declared overlays: {', '.join(sorted(declared)) or '(none)'}. "
                    "Fix: declare the overlay in `overlays:` or correct the rule's overlay tag."
                ),
                details={
                    "undeclared": undeclared,
                    "offending_rules": offenders,
                    "declared": sorted(declared),
                    "rule_overlay_tags": sorted(rule_tags),
                    "declared_but_unused": unused,
                },
            )
        ]

    message = (
        f"every rule overlay tag is declared ({len(rule_tags)} tag(s) over "
        f"{len(declared)} declared overlay(s))"
    )
    if unused:
        message += f"; declared-but-unused (advisory): {', '.join(unused)}"
    return [
        CheckResult(
            status="pass",
            code=OVERLAY_DECLARATION_CODE,
            message=message,
            details={
                "declared": sorted(declared),
                "rule_overlay_tags": sorted(rule_tags),
                "declared_but_unused": unused,
            },
        )
    ]


def validate_overlay_declarations_file(
    policy_path: Path | str | None = None,
) -> list[CheckResult]:
    """Load skill-scope.yaml and run :func:`validate_overlay_declarations`.

    Convenience wrapper for doctor / CLI callers, mirroring
    :func:`validate_global_skill_contract_file`: resolves the canonical
    ``skill-scope.yaml`` (or an explicit path), parses it, and runs the
    overlay-declaration lint. A missing policy file is a pass (nothing to
    enforce); a parse failure is a fail.
    """
    resolved = Path(policy_path) if policy_path else _skill_scope_policy_path()
    if not resolved.is_file():
        return [
            CheckResult(
                status="pass",
                code=OVERLAY_DECLARATION_CODE,
                message=f"no skill-scope policy found at {resolved}",
            )
        ]
    try:
        policy = load_yaml(resolved)
    except RuntimeError as exc:
        return [
            CheckResult(
                status="fail",
                code=OVERLAY_DECLARATION_CODE,
                message=f"could not parse skill-scope policy at {resolved}: {exc}",
            )
        ]
    if not isinstance(policy, dict):
        return [
            CheckResult(
                status="fail",
                code=OVERLAY_DECLARATION_CODE,
                message=f"skill-scope policy at {resolved} is not a mapping",
            )
        ]
    return validate_overlay_declarations(policy, policy_path=str(resolved))


GLOBAL_OVERLAY_PRECEDENCE_CODE = "global-overlay-precedence"


def _policy_always_global_skills(policy: dict[str, Any]) -> set[str]:
    """The always-global skill set: ``global_allowlist`` ∪ allow_global rules.

    These are linked into every repo unconditionally (layer 1+2 of the global
    skill contract). Both sources are unioned so the precedence lint stays
    correct even mid-edit, before ``validate_global_skill_contract`` has been
    re-reconciled (that lint owns the *equality* of the two; this one only needs
    membership).
    """
    return _scope_global_allowlist(policy) | _scope_allow_global_union(policy)


def _policy_overlay_gated_skills(policy: dict[str, Any]) -> dict[str, list[str]]:
    """Map each overlay-gated skill -> the rule ids (with overlay tag) gating it.

    A rule is overlay-gated when it carries a non-empty ``overlay:`` tag. The
    skill is recorded with a ``<rule_id> (overlay: <tag>)`` provenance label so a
    precedence failure can point at the exact offending rule + overlay.
    """
    gated: dict[str, list[str]] = {}
    for rule in policy.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        tag = str(rule.get("overlay") or "").strip()
        if not tag:
            continue
        rule_id = str(rule.get("id") or "").strip() or "(unnamed rule)"
        label = f"{rule_id} (overlay: {tag})"
        for name in _rule_skill_names(rule):
            if name:
                gated.setdefault(name, []).append(label)
    return gated


_GLOB_CHARS = ("*", "?", "[")


def _always_global_covers(always_global: set[str], gated_skill: str) -> bool:
    """Is an overlay-gated literal skill ALSO always-global, glob-aware?

    The runtime's ``_global_install_allowed`` decides "is this skill always-global"
    via ``fnmatch`` of the skill name against every always-global pattern, so an
    ``allow_global`` rule granting ``beads-*`` makes the literal ``beads-br``
    always-global on EVERY box. An overlay rule that then names ``beads-br`` IS the
    global-vs-overlay contradiction this lint exists to catch, but a plain
    exact-string set intersection misses it because ``beads-br != beads-*``.

    So we match like the runtime: exact membership first (the common case), then
    fnmatch the gated literal name against any always-global entry that carries a
    glob metacharacter. Glob-vs-glob is left to exact membership (two glob patterns
    are "the same global grant" only when identical), mirroring the install path
    which fnmatches concrete skill names, not patterns, against the allowlist.
    """
    import fnmatch  # noqa: PLC0415

    if gated_skill in always_global:
        return True
    for entry in always_global:
        if any(ch in entry for ch in _GLOB_CHARS) and fnmatch.fnmatchcase(gated_skill, entry):
            return True
    return False


def validate_global_overlay_precedence(
    policy: dict[str, Any],
    *,
    policy_path: str | None = None,
) -> list[CheckResult]:
    """Assert no skill is BOTH always-global AND overlay-gated.

    The global skill contract has a strict precedence (repos-sbp-policy-estate-
    oh1.2): an always-global skill -- one granted by an ``allow_global: true``
    rule (the dispatcher core + ``operator-global-exceptions``, e.g.
    ``divide-and-conquer``) or listed in ``global_allowlist`` -- is linked into
    EVERY repo unconditionally. Flipping a mode-pack overlay can neither add nor
    remove it. **Global wins.**

    Therefore an overlay rule only meaningfully adds NON-global skills. Naming an
    already-global skill in an overlay rule is ambiguous noise: it implies the
    overlay gates a skill it cannot actually gate (the global layer already
    provides it everywhere, overlay on or off), and it invites a reader to think
    toggling the overlay changes that skill's availability. It does not.

    This lint makes that contradiction loud. It intersects the always-global set
    with the overlay-gated set:

        always_global(policy)  ∩  overlay_gated(policy)  ==  ∅

    On any overlap it FAILS, naming each double-declared skill, the overlay
    rule(s) that gate it, and the fix (drop it from the overlay rule; the global
    layer already provides it). An empty policy (no global surface and no
    overlay-gated rules) is a PASS, mirroring the sibling lints' empty-policy
    posture. Declared-but-distinct sets are a PASS.
    """
    if not isinstance(policy, dict):
        return [
            CheckResult(
                status="pass",
                code=GLOBAL_OVERLAY_PRECEDENCE_CODE,
                message="no skill-scope policy to validate",
            )
        ]

    always_global = _policy_always_global_skills(policy)
    overlay_gated = _policy_overlay_gated_skills(policy)

    if not always_global and not overlay_gated:
        return [
            CheckResult(
                status="pass",
                code=GLOBAL_OVERLAY_PRECEDENCE_CODE,
                message="skill-scope policy declares no global/overlay surface",
            )
        ]

    conflicts = sorted(
        name for name in overlay_gated if _always_global_covers(always_global, name)
    )

    if conflicts:
        offenders = {name: overlay_gated[name] for name in conflicts}
        offender_text = "; ".join(
            f"{name} (gated by {', '.join(rule_labels)})"
            for name, rule_labels in offenders.items()
        )
        location = f" in {policy_path}" if policy_path else ""
        return [
            CheckResult(
                status="fail",
                code=GLOBAL_OVERLAY_PRECEDENCE_CODE,
                message=(
                    f"global-vs-overlay precedence conflict{location}: {offender_text}. "
                    "A skill that is always-global (granted by an allow_global rule or "
                    "global_allowlist) is linked into every repo unconditionally; an "
                    "overlay cannot add or remove it (global wins). Naming it in an "
                    "overlay rule is ambiguous. "
                    "Fix: drop the skill from the overlay rule (the global layer already "
                    "provides it everywhere), or remove its global grant if it should be "
                    "overlay-gated instead."
                ),
                details={
                    "conflicts": conflicts,
                    "offending_overlay_rules": offenders,
                    "always_global": sorted(always_global),
                    "overlay_gated": sorted(overlay_gated),
                },
            )
        ]

    return [
        CheckResult(
            status="pass",
            code=GLOBAL_OVERLAY_PRECEDENCE_CODE,
            message=(
                "no global/overlay precedence conflict "
                f"({len(always_global)} always-global skill(s), "
                f"{len(overlay_gated)} overlay-gated skill(s), disjoint)"
            ),
            details={
                "always_global": sorted(always_global),
                "overlay_gated": sorted(overlay_gated),
            },
        )
    ]


def validate_global_overlay_precedence_file(
    policy_path: Path | str | None = None,
) -> list[CheckResult]:
    """Load skill-scope.yaml and run :func:`validate_global_overlay_precedence`.

    Convenience wrapper for doctor / CLI callers, mirroring
    :func:`validate_overlay_declarations_file`: resolves the canonical
    ``skill-scope.yaml`` (or an explicit path), parses it, and runs the
    precedence lint. A missing policy file is a pass (nothing to enforce); a
    parse failure is a fail.
    """
    resolved = Path(policy_path) if policy_path else _skill_scope_policy_path()
    if not resolved.is_file():
        return [
            CheckResult(
                status="pass",
                code=GLOBAL_OVERLAY_PRECEDENCE_CODE,
                message=f"no skill-scope policy found at {resolved}",
            )
        ]
    try:
        policy = load_yaml(resolved)
    except RuntimeError as exc:
        return [
            CheckResult(
                status="fail",
                code=GLOBAL_OVERLAY_PRECEDENCE_CODE,
                message=f"could not parse skill-scope policy at {resolved}: {exc}",
            )
        ]
    if not isinstance(policy, dict):
        return [
            CheckResult(
                status="fail",
                code=GLOBAL_OVERLAY_PRECEDENCE_CODE,
                message=f"skill-scope policy at {resolved} is not a mapping",
            )
        ]
    return validate_global_overlay_precedence(policy, policy_path=str(resolved))


REGISTRY_PATH_DUPLICATION_CODE = "registry-path-duplication"


def _registry_id_resolved_paths() -> dict[str, set[str]]:
    """Map each registry id -> the FULL set of resolved spellings it expands to.

    This is the equality basis for the duplication lint (bug y8w-fix). A literal
    ``paths:`` entry is only a no-op-replaceable duplicate of ``repos: [<id>]``
    when the rule's literals enumerate the WHOLE resolved set of that id; a single
    home-form spelling like ``~/repos/buildooor`` is a strict SUBSET of the id's
    resolved set (which on a machine with ``~/repos`` repo roots also includes the
    ``/srv/.../buildooor`` re-rooting), so swapping it for ``repos:`` would WIDEN
    the match set — a real behavior change the lint must NOT recommend.

    Reuses ``skill_visibility``'s registry loader + the SAME machine-aware
    id->path resolution the evaluator uses, so this lint can never disagree with
    what ``repos:`` would resolve to. Returns ``{}`` when the registry is
    unreadable (lazy import keeps validation import-cycle-free). Lazy-imported
    inside the function because ``skill_visibility`` imports back through this
    package's ``shared`` surface.
    """
    from . import skill_visibility as sv  # noqa: PLC0415

    by_id: dict[str, set[str]] = {}
    try:
        entries = sv._load_registry_entries()
    except Exception:  # pragma: no cover - defensive: registry never breaks the lint
        return {}
    for entry in entries:
        repo_id = str(entry.get("id") or "").strip()
        declared = str(entry.get("path") or "").strip()
        if not repo_id or not declared:
            continue
        try:
            resolved_paths = sv._resolve_registry_path(declared)
        except Exception:  # pragma: no cover - defensive
            continue
        resolved_set = {p for p in resolved_paths if p}
        if resolved_set:
            by_id.setdefault(repo_id, set()).update(resolved_set)
    return by_id


def _resolve_literal_scope_path(raw_path: str) -> str:
    from . import skill_visibility as sv  # noqa: PLC0415

    return sv._expand_policy_path(raw_path)


def validate_registry_path_duplication(
    policy: dict[str, Any],
    *,
    policy_path: str | None = None,
) -> list[CheckResult]:
    """Flag raw ``paths:`` that a registry id covers EXACTLY (no-op replaceable).

    Raw ``paths:`` stay fully supported for back-compat — this lint does NOT fail
    them; it WARNS so a duplication a registry id makes redundant is visibly
    discouraged, not silently allowed. BUT it only warns when migrating to
    ``repos: [<id>]`` would be BEHAVIOR-PRESERVING.

    The subtlety (the y8w fix): a registry id resolves to a SET of spellings on
    the current machine — its home-relative form PLUS every re-rooting under the
    machine's repo roots (e.g. ``buildooor`` -> ``~/repos/buildooor`` AND
    ``/srv/.../buildooor`` on a ``~/repos``-rooted box). A literal path like
    ``~/repos/buildooor`` matches only ONE of those spellings. Replacing that one
    literal with ``repos: [buildooor]`` would WIDEN the rule's match set to the
    whole superset — a real behavior change. So a literal that is a strict SUBSET
    of an id's resolved set is INTENTIONALLY narrower and must NOT be flagged.

    Therefore the lint groups each rule's resolved literal ``paths:`` by the
    registry id whose resolved set contains them, and warns for a ``(rule, id)``
    pair ONLY when the rule's literals under that id ENUMERATE the id's FULL
    resolved set (set equality, not membership). At equality the swap is a no-op on
    every machine, so naming the repo once via ``repos: [<id>]`` is purely
    redundant — exactly what we want to discourage.

    Machine-aware: a path written in another machine's canonical form (which does
    not resolve to a registry path on THIS box) simply will not match any id's
    resolved set, so the lint never nags about known-foreign spellings. An
    empty/unreadable registry yields a PASS (nothing to compare against).
    """
    if not isinstance(policy, dict):
        return [
            CheckResult(
                status="pass",
                code=REGISTRY_PATH_DUPLICATION_CODE,
                message="no skill-scope policy to validate",
            )
        ]

    id_resolved = _registry_id_resolved_paths()
    if not id_resolved:
        return [
            CheckResult(
                status="pass",
                code=REGISTRY_PATH_DUPLICATION_CODE,
                message="no registry id->path index available on this machine",
            )
        ]

    redundant: list[dict[str, str]] = []
    for rule in policy.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rule_id = str(rule.get("id") or "(unnamed rule)").strip()
        raw_paths = rule.get("paths") or rule.get("allowed_paths") or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        # Map each resolved literal back to its original text, so the report can
        # name the exact literal(s) a `repos:` swap would replace.
        resolved_to_text: dict[str, str] = {}
        for raw_path in raw_paths:
            text = str(raw_path).strip()
            if not text:
                continue
            resolved_to_text.setdefault(_resolve_literal_scope_path(text), text)
        resolved_literals = set(resolved_to_text)
        if not resolved_literals:
            continue
        for registry_id, resolved_set in sorted(id_resolved.items()):
            covered = resolved_literals & resolved_set
            # Only a no-op swap warrants a warning: the rule's literals must
            # enumerate the id's FULL resolved set. A strict subset is the
            # deliberately-narrower case (e.g. home-form-only) and is left alone.
            if covered and covered == resolved_set:
                paths_text = ", ".join(
                    resolved_to_text[resolved] for resolved in sorted(covered)
                )
                redundant.append(
                    {
                        "rule": rule_id,
                        "path": paths_text,
                        "registry_id": registry_id,
                    }
                )

    location = f" in {policy_path}" if policy_path else ""
    if redundant:
        offender_text = "; ".join(
            f"rule {item['rule']!r} path {item['path']!r} is covered by registry id "
            f"{item['registry_id']!r}"
            for item in redundant
        )
        return [
            CheckResult(
                status="warn",
                code=REGISTRY_PATH_DUPLICATION_CODE,
                message=(
                    f"raw path(s) duplicate a registry id{location}: {offender_text}. "
                    "Fix: replace the literal path with `repos: [<id>]` so the repo is "
                    "named once and its per-machine path is derived from "
                    "registry/repos.yaml + machines.yaml (bead y8w.3)."
                ),
                details={"redundant": redundant},
            )
        ]

    return [
        CheckResult(
            status="pass",
            code=REGISTRY_PATH_DUPLICATION_CODE,
            message="no raw path duplicates a registry id",
        )
    ]


def validate_registry_path_duplication_file(
    policy_path: Path | str | None = None,
) -> list[CheckResult]:
    """Load skill-scope.yaml and run :func:`validate_registry_path_duplication`.

    Mirrors :func:`validate_global_skill_contract_file`: resolves the canonical
    ``skill-scope.yaml`` (or an explicit path), parses it, and runs the
    registry-path-duplication lint. A missing policy file is a pass; a parse
    failure is a fail.
    """
    resolved = Path(policy_path) if policy_path else _skill_scope_policy_path()
    if not resolved.is_file():
        return [
            CheckResult(
                status="pass",
                code=REGISTRY_PATH_DUPLICATION_CODE,
                message=f"no skill-scope policy found at {resolved}",
            )
        ]
    try:
        policy = load_yaml(resolved)
    except RuntimeError as exc:
        return [
            CheckResult(
                status="fail",
                code=REGISTRY_PATH_DUPLICATION_CODE,
                message=f"could not parse skill-scope policy at {resolved}: {exc}",
            )
        ]
    if not isinstance(policy, dict):
        return [
            CheckResult(
                status="fail",
                code=REGISTRY_PATH_DUPLICATION_CODE,
                message=f"skill-scope policy at {resolved} is not a mapping",
            )
        ]
    return validate_registry_path_duplication(policy, policy_path=str(resolved))


def _forge_root_dir(model: dict[str, Any]) -> Path:
    root_dir = str(model.get("root_dir") or "").strip()
    if root_dir:
        return Path(root_dir)
    for skillset in model.get("skills") or []:
        clone_root = str(skillset.get("clone_root_host_path") or "").strip()
        if clone_root:
            clone_path = Path(clone_root)
            if len(clone_path.parents) > 1:
                return clone_path.parents[1]
    return DEFAULT_ROOT_DIR


def _forge_latest_score_time(home: Path) -> DateTime:
    from .forge import _parse_datetime, _record_scored_at, load_review_history

    latest = DateTime.min.replace(tzinfo=timezone.utc)
    for record in load_review_history(home=home):
        scored_at = _parse_datetime(_record_scored_at(record))
        if scored_at > latest:
            latest = scored_at
    return latest


def _forge_transcript_paths(home: Path) -> list[Path]:
    roots = (
        home / ".claude" / "projects",
        home / ".codex" / "sessions",
    )
    paths: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        paths.extend(path for path in root.rglob("*.jsonl") if path.is_file())
    return paths


def _forge_unscored_transcript_count(home: Path) -> int:
    latest_score = _forge_latest_score_time(home)
    count = 0
    for transcript in _forge_transcript_paths(home):
        try:
            modified = DateTime.fromtimestamp(transcript.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if modified > latest_score:
            count += 1
    return count


def _check_forge_hook(home: Path, scoring_script: Path) -> list[CheckResult]:
    from .forge import scoring_hook_installed

    if scoring_hook_installed(home=home, scoring_script=scoring_script):
        return []
    return [
        CheckResult(
            status="warn",
            code=SKILL_FORGE_HOOK_MISSING,
            message="Forge scoring hook not installed. Run `manage.py forge init`.",
            details={
                "settings_path": str(home / ".claude" / "settings.json"),
                "scoring_script": str(scoring_script),
            },
        )
    ]


def _check_forge_pending(root_dir: Path) -> list[CheckResult]:
    from .forge import pending_forge_skills

    results: list[CheckResult] = []
    for skill_name in sorted(pending_forge_skills(root_dir)):
        results.append(
            CheckResult(
                status="info",
                code=SKILL_FORGE_PENDING,
                message=(
                    f"Pending forge proposal for '{skill_name}'. "
                    f"Review with `manage.py forge accept/reject {skill_name}`."
                ),
                details={"skill": skill_name},
            )
        )
    return results


def _stale_forge_metric(skill_status: dict[str, Any]) -> str:
    for label in skill_status.get("thresholds_crossed") or []:
        text = str(label).strip()
        if text:
            return text.split(" ", 1)[0]
    return "metrics"


def _check_forge_stale(status_payload: dict[str, Any]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for skill_status in status_payload.get("skills") or []:
        if int(skill_status.get("sessions_scored") or 0) < 3:
            continue
        if skill_status.get("trend") != "declining":
            continue
        if not skill_status.get("thresholds_crossed"):
            continue
        if bool(skill_status.get("proposal_pending")):
            continue
        skill_name = str(skill_status.get("name") or "").strip()
        if not skill_name:
            continue
        metric = _stale_forge_metric(skill_status)
        results.append(
            CheckResult(
                status="info",
                code=SKILL_FORGE_STALE,
                message=(
                    f"Skill '{skill_name}' has declining {metric}. "
                    f"Consider `manage.py forge propose {skill_name}`."
                ),
                details={
                    "skill": skill_name,
                    "metric": metric,
                    "sessions_scored": skill_status.get("sessions_scored"),
                    "thresholds_crossed": skill_status.get("thresholds_crossed") or [],
                },
            )
        )
    return results


def _check_forge_unscored(home: Path) -> list[CheckResult]:
    unscored_count = _forge_unscored_transcript_count(home)
    if unscored_count < SKILL_FORGE_UNSCORED_THRESHOLD:
        return []
    return [
        CheckResult(
            status="warn",
            code=SKILL_FORGE_UNSCORED,
            message=(
                f"Found {unscored_count} unscored sessions. "
                "Run `score-session.sh --source both --since week`."
            ),
            details={
                "unscored_sessions": unscored_count,
                "threshold": SKILL_FORGE_UNSCORED_THRESHOLD,
            },
        )
    ]


def validate_forge_health(
    model: dict[str, Any],
    *,
    home: Path | str | None = None,
    scoring_script: Path | str | None = None,
) -> list[CheckResult]:
    """Surface passive skill-forge state in doctor without mutating operator files."""
    from .forge import default_scoring_script, forge_status

    home_dir = Path(home).expanduser() if home is not None else Path.home()
    score_path = Path(scoring_script).expanduser() if scoring_script is not None else default_scoring_script()
    root_dir = _forge_root_dir(model)
    try:
        status_payload = forge_status(home=home_dir, root_dir=root_dir, scoring_script=score_path)
    except Exception as exc:
        return [
            CheckResult(
                status="warn",
                code="skill-forge-health",
                message="forge health could not be inspected",
                details={"error": str(exc)},
            )
        ]

    results: list[CheckResult] = []
    results.extend(_check_forge_hook(home_dir, score_path))
    results.extend(_check_forge_stale(status_payload))
    results.extend(_check_forge_pending(root_dir))
    results.extend(_check_forge_unscored(home_dir))
    return results


def _check_top_level_duplicates(model: dict[str, Any]) -> tuple[list[str], set[str]]:
    issues: list[str] = []
    client_dup_ids = find_duplicates(model.get("clients") or [], "id")
    if client_dup_ids:
        issues.append(f"clients contain duplicate ids: {', '.join(client_dup_ids)}")
    for client in model.get("clients") or []:
        if not client.get("id"):
            issues.append("every client entry must have an id")
    declared_client_ids = {
        str(client.get("id", "")).strip()
        for client in model.get("clients") or []
        if str(client.get("id", "")).strip()
    }
    default_client = str((model.get("selection") or {}).get("default_client") or "").strip()
    if default_client and default_client not in declared_client_ids:
        issues.append(f"selection.default_client references unknown client {default_client!r}")

    for section in (
        "repos", "artifacts", "env_files", "skills", "tasks",
        "services", "logs", "checks", "ingress_routes",
    ):
        duplicates = find_duplicates(model.get(section) or [], "id")
        if duplicates:
            issues.append(f"{section} contain duplicate ids: {', '.join(duplicates)}")

    for section in ("repos", "logs", "artifacts", "env_files"):
        duplicates = find_duplicates(model[section], "path")
        if duplicates:
            issues.append(f"{section} contain duplicate paths: {', '.join(duplicates)}")
    return issues, declared_client_ids


def _check_repo_entries(model: dict[str, Any], declared_client_ids: set[str]) -> list[str]:
    issues: list[str] = []
    for repo in model["repos"]:
        if not repo.get("id"):
            issues.append("every repo entry must have an id")
        if not repo.get("path"):
            issues.append(f"repo {repo.get('id', '(missing id)')} is missing path")
        if repo.get("client") and repo["client"] not in declared_client_ids:
            issues.append(f"repo {repo.get('id')} references unknown client {repo['client']!r}")

        source = repo.get("source") or {}
        source_kind = source.get("kind", "manual")
        if source_kind not in VALID_REPO_SOURCE_KINDS:
            issues.append(f"repo {repo.get('id')} has unsupported source.kind {source_kind!r}")

        sync = repo.get("sync") or {}
        sync_mode = sync.get("mode") or (
            "ensure-directory" if source_kind == "directory" else "external"
        )
        if sync_mode not in VALID_SYNC_MODES:
            issues.append(f"repo {repo.get('id')} has unsupported sync.mode {sync_mode!r}")
        if source_kind == "git" and not source.get("url"):
            issues.append(f"repo {repo.get('id')} is git-backed but missing source.url")
        issues.extend(_check_repo_project_contract(repo))
    return issues


def _check_repo_project_contract(repo: dict[str, Any]) -> list[str]:
    project_kind = str(repo.get("project_kind") or "").strip()
    lanes = repo.get("command_lanes") or {}
    issues: list[str] = []

    if not project_kind and lanes:
        issues.append(f"repo {repo.get('id')} declares command_lanes without project_kind")
        return issues
    if not project_kind:
        return issues
    if project_kind not in VALID_PROJECT_KINDS:
        issues.append(f"repo {repo.get('id')} has unsupported project_kind {project_kind!r}")
        return issues
    if not isinstance(lanes, dict):
        return [f"repo {repo.get('id')} command_lanes must be a mapping"]

    if project_kind == PROJECT_KIND_IOS:
        lane_ids = {str(lane_id).strip() for lane_id in lanes if str(lane_id).strip()}
        missing = [lane_id for lane_id in IOS_COMMAND_LANES if lane_id not in lane_ids]
        if missing:
            issues.append(
                f"repo {repo.get('id')} project_kind ios is missing command_lanes: "
                + ", ".join(missing)
            )
        unsupported = sorted(lane_id for lane_id in lane_ids if lane_id not in IOS_COMMAND_LANES)
        if unsupported:
            issues.append(
                f"repo {repo.get('id')} project_kind ios has unsupported command_lanes: "
                + ", ".join(unsupported)
            )

    for lane_id, lane in lanes.items():
        if not isinstance(lane, dict):
            issues.append(f"repo {repo.get('id')} command_lanes.{lane_id} must be a mapping")
            continue
        if not _command_lane_has_action(lane):
            issues.append(f"repo {repo.get('id')} command_lanes.{lane_id} is missing command")
    return issues


def _command_lane_has_action(lane: dict[str, Any]) -> bool:
    if str(lane.get("command") or "").strip():
        return True
    status = str(lane.get("status") or "").strip()
    if status not in {"manual", "deferred", "unsupported"}:
        return False
    return bool(str(lane.get("reason") or lane.get("notes") or "").strip())


def _check_artifact_identity(
    artifact: dict[str, Any], declared_client_ids: set[str],
) -> list[str]:
    issues: list[str] = []
    if not artifact.get("id"):
        issues.append("every artifact entry must have an id")
    if not artifact.get("path"):
        issues.append(f"artifact {artifact.get('id', '(missing id)')} is missing path")
    if artifact.get("client") and artifact["client"] not in declared_client_ids:
        issues.append(f"artifact {artifact.get('id')} references unknown client {artifact['client']!r}")
    return issues


def _check_artifact_source(artifact: dict[str, Any]) -> tuple[list[str], str]:
    source = artifact.get("source") or {}
    source_kind = source.get("kind", "manual")
    issues: list[str] = []
    if source_kind not in VALID_ARTIFACT_SOURCE_KINDS:
        issues.append(f"artifact {artifact.get('id')} has unsupported source.kind {source_kind!r}")
    if source_kind == "url" and str(source.get("url") or "").strip():
        try:
            validate_url_download_source(source, artifact_id=str(artifact.get("id", "(missing id)")))
        except RuntimeError as exc:
            issues.append(str(exc))
    return issues, source_kind


def _default_artifact_sync_mode(source_kind: str) -> str:
    if source_kind == "url":
        return "download-if-missing"
    if source_kind == "file":
        return "copy-if-missing"
    return "manual"


def _check_artifact_sync(artifact: dict[str, Any], source_kind: str) -> list[str]:
    sync = artifact.get("sync") or {}
    sync_mode = sync.get("mode") or _default_artifact_sync_mode(source_kind)
    if sync_mode in VALID_ARTIFACT_SYNC_MODES:
        return []
    return [f"artifact {artifact.get('id')} has unsupported sync.mode {sync_mode!r}"]


def _check_artifact_entries(model: dict[str, Any], declared_client_ids: set[str]) -> list[str]:
    issues: list[str] = []
    for artifact in model["artifacts"]:
        source_issues, source_kind = _check_artifact_source(artifact)
        issues.extend(_check_artifact_identity(artifact, declared_client_ids))
        issues.extend(source_issues)
        issues.extend(_check_artifact_sync(artifact, source_kind))
    return issues


def _check_env_file_entries(
    model: dict[str, Any], declared_client_ids: set[str], repo_ids: set[Any],
) -> list[str]:
    issues: list[str] = []
    for env_file in model["env_files"]:
        if not env_file.get("id"):
            issues.append("every env_files entry must have an id")
        if not env_file.get("path"):
            issues.append(f"env file {env_file.get('id', '(missing id)')} is missing path")
        if env_file.get("client") and env_file["client"] not in declared_client_ids:
            issues.append(f"env file {env_file.get('id')} references unknown client {env_file['client']!r}")
        if env_file.get("repo") and env_file["repo"] not in repo_ids:
            issues.append(f"env file {env_file.get('id')} references unknown repo {env_file['repo']!r}")

        source = env_file.get("source") or {}
        source_kind = source.get("kind", "manual")
        if source_kind not in VALID_ENV_FILE_SOURCE_KINDS:
            issues.append(f"env file {env_file.get('id')} has unsupported source.kind {source_kind!r}")

        sync = env_file.get("sync") or {}
        sync_mode = sync.get("mode") or ("write" if source_kind == "file" else "manual")
        if sync_mode not in VALID_ENV_FILE_SYNC_MODES:
            issues.append(f"env file {env_file.get('id')} has unsupported sync.mode {sync_mode!r}")
        if source_kind == "file" and not source.get("path"):
            issues.append(f"env file {env_file.get('id')} is file-backed but missing source.path")
    return issues


def _check_skill_identity(
    skillset: dict[str, Any], declared_client_ids: set[str],
) -> list[str]:
    issues: list[str] = []
    if not skillset.get("id"):
        issues.append("every skills entry must have an id")
    if skillset.get("client") and skillset["client"] not in declared_client_ids:
        issues.append(f"skill set {skillset.get('id')} references unknown client {skillset['client']!r}")
    return issues


def _required_skill_fields(skillset: dict[str, Any]) -> tuple[str, ...]:
    kind = skillset.get("kind", "packaged-skill-set")
    if kind == "skill-repo-set":
        return "skill_repos_config", "lock_path"
    return "bundle_dir", "manifest", "sources_config", "lock_path"


def _check_skill_required_fields(skillset: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in _required_skill_fields(skillset):
        if not skillset.get(field):
            issues.append(f"skill set {skillset.get('id', '(missing id)')} is missing {field}")
    return issues


def _check_skill_sync(skillset: dict[str, Any]) -> list[str]:
    sync = skillset.get("sync") or {}
    sync_mode = sync.get("mode") or "unpack-bundles"
    if sync_mode in VALID_SKILL_SYNC_MODES:
        return []
    return [f"skill set {skillset.get('id')} has unsupported sync.mode {sync_mode!r}"]


def _check_skill_install_targets(skillset: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    targets = skillset.get("install_targets") or []
    if not targets:
        return [f"skill set {skillset.get('id')} must declare at least one install target"]

    target_dup_ids = find_duplicates(targets, "id")
    if target_dup_ids:
        issues.append(f"skill set {skillset.get('id')} contains duplicate target ids: {', '.join(target_dup_ids)}")

    for target in targets:
        if not target.get("id"):
            issues.append(f"skill set {skillset.get('id')} contains a target without an id")
        if not target.get("path"):
            issues.append(f"skill set {skillset.get('id')} target {target.get('id', '(missing id)')} is missing path")
    return issues


def _check_skill_entries(model: dict[str, Any], declared_client_ids: set[str]) -> list[str]:
    issues: list[str] = []
    for skillset in model["skills"]:
        issues.extend(_check_skill_identity(skillset, declared_client_ids))
        issues.extend(_check_skill_required_fields(skillset))
        issues.extend(_check_skill_sync(skillset))
        issues.extend(_check_skill_install_targets(skillset))
    return issues


def _check_dependency_list(
    raw_dependencies: Any,
    *,
    owner_kind: str,
    owner_id: str,
    self_id: str,
    valid_ids: set[str],
    accept_extra: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Validate a depends_on list. Returns (issues, deduped dependency ids)."""
    issues: list[str] = []
    dependency_ids: list[str] = []
    if raw_dependencies and not isinstance(raw_dependencies, list):
        issues.append(f"{owner_kind} {owner_id} has non-list depends_on")
        return issues, dependency_ids
    seen: set[str] = set()
    accept_extra = accept_extra or set()
    for raw_dependency in raw_dependencies or []:
        dependency_id = str(raw_dependency).strip()
        if not dependency_id:
            issues.append(f"{owner_kind} {owner_id} contains an empty depends_on entry")
            continue
        if dependency_id in seen:
            issues.append(f"{owner_kind} {owner_id} contains duplicate depends_on entry {dependency_id!r}")
            continue
        if dependency_id == self_id:
            issues.append(f"{owner_kind} {owner_id} cannot depend on itself")
            continue
        if dependency_id not in valid_ids and dependency_id not in accept_extra:
            issues.append(f"{owner_kind} {owner_id} references unknown dependency {dependency_id!r}")
            continue
        dependency_ids.append(dependency_id)
        seen.add(dependency_id)
    return issues, dependency_ids


def _check_task_success(task: dict[str, Any], bridge_ids: set[str]) -> list[str]:
    success = task.get("success") or {}
    success_type = success.get("type")
    issues: list[str] = []
    if not success_type:
        issues.append(f"task {task.get('id', '(missing id)')} is missing success.type")
    elif success_type not in VALID_TASK_SUCCESS_TYPES:
        issues.append(f"task {task.get('id')} has unsupported success.type {success_type!r}")
    issues.extend(_check_path_exists_success(task, success, success_type))
    issues.extend(_check_all_outputs_success(task, success, success_type, bridge_ids))
    issues.extend(_check_port_listening_success(task, success, success_type))
    return issues


def _check_path_exists_success(
    task: dict[str, Any], success: dict[str, Any], success_type: str | None,
) -> list[str]:
    if success_type == "path_exists" and not success.get("path"):
        return [f"task {task.get('id')} path_exists success is missing path"]
    return []


def _check_all_outputs_success(
    task: dict[str, Any],
    success: dict[str, Any],
    success_type: str | None,
    bridge_ids: set[str],
) -> list[str]:
    if success_type != "all_outputs_exist":
        return []
    target = str(success.get("target") or "").strip()
    if not target:
        return [f"task {task.get('id')} all_outputs_exist success is missing target"]
    if target not in bridge_ids:
        return [
            f"task {task.get('id')} all_outputs_exist success references unknown bridge {target!r}"
        ]
    return []


def _check_port_listening_success(
    task: dict[str, Any], success: dict[str, Any], success_type: str | None,
) -> list[str]:
    if success_type == "port_listening" and not _is_int_port(success.get("port")):
        return [f"task {task.get('id')} port_listening success is missing integer port"]
    return []


def _check_task_entries(
    model: dict[str, Any],
    declared_client_ids: set[str],
    repo_ids: set[Any],
    log_ids: set[Any],
    task_ids: set[str],
    bridge_ids: set[str],
) -> tuple[list[str], dict[str, list[str]]]:
    issues: list[str] = []
    task_dependency_map: dict[str, list[str]] = {}
    for task in model["tasks"]:
        task_id = str(task.get("id", "")).strip()
        if not task.get("id"):
            issues.append("every task entry must have an id")
        if task.get("client") and task["client"] not in declared_client_ids:
            issues.append(f"task {task.get('id')} references unknown client {task['client']!r}")
        if task.get("repo") and task["repo"] not in repo_ids:
            issues.append(f"task {task.get('id')} references unknown repo {task['repo']!r}")
        if task.get("log") and task["log"] not in log_ids:
            issues.append(f"task {task.get('id')} references unknown log {task['log']!r}")
        if not str(task.get("command") or "").strip():
            issues.append(f"task {task.get('id', '(missing id)')} is missing command")

        for field_name in ("inputs", "outputs"):
            raw_value = task.get(field_name) or []
            if not isinstance(raw_value, list):
                issues.append(f"task {task.get('id')} has non-list {field_name}")

        dep_issues, dependency_ids = _check_dependency_list(
            task.get("depends_on") or [],
            owner_kind="task",
            owner_id=str(task.get("id")),
            self_id=task_id,
            valid_ids=task_ids,
        )
        issues.extend(dep_issues)
        if task_id:
            task_dependency_map[task_id] = dependency_ids

        issues.extend(_check_task_success(task, bridge_ids))
    return issues, task_dependency_map


def _check_service_healthcheck(service: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    healthcheck = service.get("healthcheck") or {}
    healthcheck_type = healthcheck.get("type")
    if not healthcheck_type:
        return issues
    if healthcheck_type not in VALID_HEALTHCHECK_TYPES:
        issues.append(
            f"service {service.get('id')} has unsupported healthcheck.type {healthcheck_type!r}"
        )
    if healthcheck_type == "http" and not healthcheck.get("url"):
        issues.append(f"service {service.get('id')} http healthcheck is missing url")
    if healthcheck_type == "path_exists" and not healthcheck.get("path"):
        issues.append(f"service {service.get('id')} path_exists healthcheck is missing path")
    if healthcheck_type == "process_running" and not healthcheck.get("pattern"):
        issues.append(f"service {service.get('id')} process_running healthcheck is missing pattern")
    if healthcheck_type == "port" and not _is_int_port(healthcheck.get("port")):
        issues.append(f"service {service.get('id')} port healthcheck is missing integer port")
    return issues


def _check_service_bootstrap_tasks(service: dict[str, Any], task_ids: set[str]) -> list[str]:
    issues: list[str] = []
    raw_bootstrap_tasks = service.get("bootstrap_tasks") or []
    if raw_bootstrap_tasks and not isinstance(raw_bootstrap_tasks, list):
        issues.append(f"service {service.get('id')} has non-list bootstrap_tasks")
        return issues
    seen: set[str] = set()
    for raw_task in raw_bootstrap_tasks:
        task_id = str(raw_task).strip()
        if not task_id:
            issues.append(f"service {service.get('id')} contains an empty bootstrap_tasks entry")
            continue
        if task_id in seen:
            issues.append(f"service {service.get('id')} contains duplicate bootstrap_tasks entry {task_id!r}")
            continue
        if task_id not in task_ids:
            issues.append(f"service {service.get('id')} references unknown bootstrap task {task_id!r}")
            continue
        seen.add(task_id)
    return issues


def _check_service_entries(
    model: dict[str, Any],
    declared_client_ids: set[str],
    repo_ids: set[Any],
    artifact_ids: set[Any],
    log_ids: set[Any],
    task_ids: set[str],
) -> tuple[list[str], dict[str, list[str]], dict[str, dict[str, Any]], set[str]]:
    issues: list[str] = []
    service_ids = {
        str(service.get("id", "")).strip()
        for service in model["services"]
        if str(service.get("id", "")).strip()
    }
    services_by_id = {
        str(service.get("id", "")).strip(): service
        for service in model["services"]
        if str(service.get("id", "")).strip()
    }
    service_dependency_map: dict[str, list[str]] = {}

    for service in model["services"]:
        service_id = str(service.get("id", "")).strip()
        if not service.get("id"):
            issues.append("every service entry must have an id")
        if service.get("client") and service["client"] not in declared_client_ids:
            issues.append(f"service {service.get('id')} references unknown client {service['client']!r}")
        if service.get("repo") and service["repo"] not in repo_ids:
            issues.append(f"service {service.get('id')} references unknown repo {service['repo']!r}")
        if service.get("artifact") and service["artifact"] not in artifact_ids:
            issues.append(f"service {service.get('id')} references unknown artifact {service['artifact']!r}")
        if service.get("log") and service["log"] not in log_ids:
            issues.append(f"service {service.get('id')} references unknown log {service['log']!r}")
        if not str(service.get("command") or "").strip():
            issues.append(f"service {service.get('id', '(missing id)')} is missing command")

        dep_issues, dependency_ids = _check_dependency_list(
            service.get("depends_on") or [],
            owner_kind="service",
            owner_id=str(service.get("id")),
            self_id=service_id,
            valid_ids=service_ids,
            accept_extra=set(artifact_ids),
        )
        issues.extend(dep_issues)
        if service_id:
            service_dependency_map[service_id] = dependency_ids

        issues.extend(_check_service_bootstrap_tasks(service, task_ids))
        issues.extend(_check_service_healthcheck(service))

    return issues, service_dependency_map, services_by_id, service_ids


def _detect_dependency_cycles(
    dependency_map: dict[str, list[str]], owner_kind: str,
) -> list[str]:
    issues: list[str] = []
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            cycle_start = visiting.index(node_id)
            cycle = visiting[cycle_start:] + [node_id]
            issues.append(f"{owner_kind} dependency cycle detected: " + " -> ".join(cycle))
            return
        visiting.append(node_id)
        for dep_id in dependency_map.get(node_id, []):
            visit(dep_id)
        visiting.pop()
        visited.add(node_id)

    for node_id in sorted(dependency_map):
        visit(node_id)
    return issues


def _check_ingress_route_entries(
    model: dict[str, Any],
    declared_client_ids: set[str],
    service_ids: set[str],
    services_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    issues: list[str] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for route in model.get("ingress_routes") or []:
        issues.extend(_check_ingress_route_identity(route, declared_client_ids))
        issues.extend(_check_ingress_route_service(route, service_ids, services_by_id))
        listener = _ingress_route_listener(route, issues)
        path = _ingress_route_path(route, issues)
        match = _ingress_route_match(route, issues)
        issues.extend(_check_ingress_route_strip_prefix(route))
        issues.extend(_check_ingress_route_duplicate(seen_keys, listener, path, match))
    return issues


def _check_ingress_route_identity(route: dict[str, Any], declared_client_ids: set[str]) -> list[str]:
    issues: list[str] = []
    if not str(route.get("id", "")).strip():
        issues.append("every ingress_routes entry must have an id")
    if route.get("client") and route["client"] not in declared_client_ids:
        issues.append(f"ingress route {route.get('id')} references unknown client {route['client']!r}")
    return issues


def _check_ingress_route_service(
    route: dict[str, Any],
    service_ids: set[str],
    services_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    service_id = str(route.get("service_id") or "").strip()
    if not service_id:
        return [f"ingress route {route.get('id', '(missing id)')} is missing service_id"]
    if service_id not in service_ids:
        return [f"ingress route {route.get('id')} references unknown service {service_id!r}"]
    if not _looks_like_ingress_origin(services_by_id[service_id].get("origin_url")):
        return [
            f"ingress route {route.get('id')} references service {service_id!r} without a valid origin_url"
        ]
    return []


def _ingress_route_listener(route: dict[str, Any], issues: list[str]) -> str:
    listener = str(route.get("listener") or "").strip().lower()
    if listener not in VALID_INGRESS_ROUTE_LISTENERS:
        issues.append(f"ingress route {route.get('id')} has unsupported listener {route.get('listener')!r}")
    return listener


def _ingress_route_path(route: dict[str, Any], issues: list[str]) -> str:
    path = str(route.get("path") or "").strip()
    path_prefix = str(route.get("path_prefix") or "").strip()
    if path_prefix and not path_prefix.startswith("/"):
        issues.append(f"ingress route {route.get('id')} path_prefix must start with '/'")
    if path and path_prefix and path != path_prefix:
        issues.append(
            f"ingress route {route.get('id')} path and path_prefix must match when both are set"
        )
    effective_path = path or path_prefix
    if not effective_path:
        issues.append(f"ingress route {route.get('id', '(missing id)')} is missing path")
    elif not effective_path.startswith("/"):
        issues.append(f"ingress route {route.get('id')} path must start with '/'")
    return effective_path


def _ingress_route_match(route: dict[str, Any], issues: list[str]) -> str:
    match = str(route.get("match") or "exact").strip().lower()
    if match not in VALID_INGRESS_ROUTE_MATCHES:
        issues.append(f"ingress route {route.get('id')} has unsupported match {route.get('match')!r}")
    return match


def _check_ingress_route_strip_prefix(route: dict[str, Any]) -> list[str]:
    if "strip_prefix" in route and not isinstance(route.get("strip_prefix"), bool):
        return [f"ingress route {route.get('id')} strip_prefix must be a boolean"]
    return []


def _check_ingress_route_duplicate(
    seen_keys: set[tuple[str, str, str]],
    listener: str,
    path: str,
    match: str,
) -> list[str]:
    if not path:
        return []
    conflict_key = (listener or "public", path, match or "exact")
    if conflict_key in seen_keys:
        seen_keys.add(conflict_key)
        return [
            "ingress routes contain duplicate listener/path/match: "
            f"{listener or 'public'} {path} ({match or 'exact'})"
        ]
    seen_keys.add(conflict_key)
    return []


def _check_log_entries(model: dict[str, Any], declared_client_ids: set[str]) -> list[str]:
    issues: list[str] = []
    for log_item in model["logs"]:
        if not log_item.get("id"):
            issues.append("every log entry must have an id")
        if not log_item.get("path"):
            issues.append(f"log {log_item.get('id', '(missing id)')} is missing path")
        if log_item.get("client") and log_item["client"] not in declared_client_ids:
            issues.append(f"log {log_item.get('id')} references unknown client {log_item['client']!r}")
    return issues


def _check_check_entries(model: dict[str, Any], declared_client_ids: set[str]) -> list[str]:
    issues: list[str] = []
    for check in model["checks"]:
        check_type = check.get("type")
        if check_type not in VALID_CHECK_TYPES:
            issues.append(f"check {check.get('id')} has unsupported type {check_type!r}")
        if check_type == "path_exists" and not check.get("path"):
            issues.append(f"check {check.get('id')} is missing path")
        if check.get("client") and check["client"] not in declared_client_ids:
            issues.append(f"check {check.get('id')} references unknown client {check['client']!r}")
    return issues


def _build_manifest_check_result(issues: list[str], model: dict[str, Any]) -> list[CheckResult]:
    if issues:
        return [CheckResult(
            status="fail",
            code="runtime-manifest",
            message="runtime manifest contains invalid definitions",
            details={"issues": issues},
        )]
    return [CheckResult(
        status="pass",
        code="runtime-manifest",
        message="runtime manifest definitions are internally consistent",
        details={
            "repos": len(model["repos"]),
            "artifacts": len(model["artifacts"]),
            "env_files": len(model["env_files"]),
            "skills": len(model["skills"]),
            "tasks": len(model["tasks"]),
            "services": len(model["services"]),
            "logs": len(model["logs"]),
            "checks": len(model["checks"]),
        },
    )]


def check_manifest(model: dict[str, Any]) -> list[CheckResult]:
    issues, declared_client_ids = _check_top_level_duplicates(model)

    repo_ids = {repo.get("id") for repo in model["repos"]}
    artifact_ids = {artifact.get("id") for artifact in model["artifacts"]}
    task_ids = {
        str(task.get("id", "")).strip()
        for task in model["tasks"]
        if str(task.get("id", "")).strip()
    }
    bridge_ids = {
        str(bridge.get("id", "")).strip()
        for bridge in model.get("bridges") or []
        if str(bridge.get("id", "")).strip()
    }
    log_ids = {log_item.get("id") for log_item in model["logs"]}

    issues.extend(_check_repo_entries(model, declared_client_ids))
    issues.extend(_check_artifact_entries(model, declared_client_ids))
    issues.extend(_check_env_file_entries(model, declared_client_ids, repo_ids))
    issues.extend(_check_skill_entries(model, declared_client_ids))
    task_issues, task_dependency_map = _check_task_entries(
        model, declared_client_ids, repo_ids, log_ids, task_ids, bridge_ids,
    )
    issues.extend(task_issues)
    service_issues, service_dependency_map, services_by_id, service_ids = _check_service_entries(
        model, declared_client_ids, repo_ids, artifact_ids, log_ids, task_ids,
    )
    issues.extend(service_issues)
    issues.extend(_detect_dependency_cycles(service_dependency_map, "service"))
    issues.extend(_check_ingress_route_entries(
        model, declared_client_ids, service_ids, services_by_id,
    ))
    issues.extend(_detect_dependency_cycles(task_dependency_map, "task"))
    issues.extend(_check_log_entries(model, declared_client_ids))
    issues.extend(_check_check_entries(model, declared_client_ids))

    return _build_manifest_check_result(issues, model)


def validate_connector_contract(model: dict[str, Any]) -> list[CheckResult]:
    superset = split_csv_values((model.get("env") or {}).get("SKILLBOX_FWC_CONNECTORS", ""))
    superset_set = set(superset)
    issues: list[str] = []
    client_contracts: list[dict[str, Any]] = []

    for client in model.get("clients") or []:
        client_id = str(client.get("id", "")).strip()
        source = str(client.get("_overlay_path") or "").strip()
        entries, entry_issues = normalize_client_connector_entries(client.get("connectors"), client_id=client_id)
        issues.extend(entry_issues)
        if not entries:
            continue

        connector_ids = [str(entry.get("id", "")).strip() for entry in entries if str(entry.get("id", "")).strip()]
        duplicate_ids = find_duplicates(entries, "id")
        if duplicate_ids:
            issues.append(f"client {client_id} declares duplicate connectors: {', '.join(duplicate_ids)}")

        widened = sorted(set(connector_ids) - superset_set)
        if widened:
            location = f" in {source}" if source else ""
            issues.append(
                f"client {client_id}{location} declares connectors outside SKILLBOX_FWC_CONNECTORS: {', '.join(widened)}"
            )

        client_contracts.append(
            {
                "client_id": client_id,
                "connectors": connector_ids,
                "overlay_path": source,
            }
        )

    if issues:
        return [
            CheckResult(
                status="fail",
                code="connector-contract",
                message="client connector declarations violate the box-level connector contract",
                details={
                    "issues": issues,
                    "box_superset": superset,
                },
            )
        ]

    return [
        CheckResult(
            status="pass",
            code="connector-contract",
            message="client connector declarations stay within the box-level connector superset",
            details={
                "box_superset": superset,
                "clients": client_contracts,
            },
        )
    ]


def _declared_model_ids(model: dict[str, Any], key: str) -> set[str]:
    return {
        str(item.get("id", "")).strip()
        for item in model.get(key) or []
        if str(item.get("id", "")).strip()
    }


def _parity_ledger_graph_ids(model: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    return (
        _declared_model_ids(model, "bridges"),
        _declared_model_ids(model, "tasks"),
        _declared_model_ids(model, "services"),
    )


def _parity_ledger_item_values(item: dict[str, Any]) -> tuple[str, str, str, Any, str, str, bool]:
    item_id = str(item.get("id", "")).strip() or "(missing id)"
    action = str(item.get("action", "")).strip()
    ownership_state = str(item.get("ownership_state", "")).strip()
    bridge_dependency = item.get("bridge_dependency")
    request_error_raw = item.get("request_error")
    request_error = str(request_error_raw).strip() if request_error_raw is not None else ""
    surface = str(item.get("legacy_surface", "")).strip() or item_id
    surface_type = str(item.get("surface_type", "service")).strip() or "service"
    return item_id, action, ownership_state, bridge_dependency, request_error, surface, surface_type == "service"


def _parity_enum_issues(
    item_id: str,
    *,
    action: str,
    ownership_state: str,
    request_error: str,
) -> list[str]:
    issues: list[str] = []
    if action and action not in PARITY_LEDGER_ACTIONS:
        issues.append(f"parity_ledger {item_id}: unsupported action {action!r}")
    if ownership_state and ownership_state not in PARITY_OWNERSHIP_STATES:
        issues.append(f"parity_ledger {item_id}: unsupported ownership_state {ownership_state!r}")
    if request_error and request_error not in LOCAL_RUNTIME_ERROR_CODES:
        issues.append(
            f"parity_ledger {item_id}: request_error {request_error!r} is not one of "
            f"the stable error codes"
        )
    return issues


def _parity_bridge_dependency_issues(
    item_id: str,
    bridge_dependency: Any,
    *,
    bridge_ids: set[str],
    task_ids: set[str],
) -> list[str]:
    if bridge_dependency is None or not str(bridge_dependency).strip():
        return []

    dep_id = str(bridge_dependency).strip()
    if dep_id in task_ids and dep_id not in bridge_ids:
        return [
            f"parity_ledger {item_id}: bridge_dependency {dep_id!r} refers to a "
            f"bootstrap_task id; only legacy_env_bridge ids are allowed"
        ]
    if dep_id not in bridge_ids:
        return [
            f"parity_ledger {item_id}: bridge_dependency {dep_id!r} is not a "
            f"declared legacy_env_bridge"
        ]
    return []


def _covered_parity_service_issues(
    item_id: str,
    surface: str,
    *,
    service_ids: set[str],
) -> list[str]:
    if not item_id or not service_ids or item_id in service_ids or surface in service_ids:
        return []
    return [
        f"parity_ledger {item_id}: ownership_state is 'covered' but no "
        f"managed_service with that id is declared"
    ]


def _deferred_parity_service_issues(
    item_id: str,
    ownership_state: str,
    *,
    service_ids: set[str],
) -> list[str]:
    if not item_id or item_id not in service_ids:
        return []
    return [
        f"parity_ledger {item_id}: ownership_state is {ownership_state!r} "
        f"but a managed_service with that id is declared"
    ]


def _parity_service_cross_reference(
    *,
    item_id: str,
    ownership_state: str,
    surface: str,
    is_service_row: bool,
    service_ids: set[str],
) -> tuple[list[str], str | None, str | None]:
    if not is_service_row:
        return [], None, None
    if ownership_state == "covered":
        return _covered_parity_service_issues(item_id, surface, service_ids=service_ids), surface, None
    if ownership_state in ("deferred", "bridge-only"):
        return _deferred_parity_service_issues(
            item_id,
            ownership_state,
            service_ids=service_ids,
        ), None, surface
    return [], None, None


def _parity_ledger_item_result(
    item: dict[str, Any],
    *,
    bridge_ids: set[str],
    task_ids: set[str],
    service_ids: set[str],
) -> tuple[list[str], str | None, str | None]:
    (
        item_id,
        action,
        ownership_state,
        bridge_dependency,
        request_error,
        surface,
        is_service_row,
    ) = _parity_ledger_item_values(item)
    issues = _parity_enum_issues(
        item_id,
        action=action,
        ownership_state=ownership_state,
        request_error=request_error,
    )
    issues.extend(
        _parity_bridge_dependency_issues(
            item_id,
            bridge_dependency,
            bridge_ids=bridge_ids,
            task_ids=task_ids,
        )
    )
    service_issues, covered_service, deferred_surface = _parity_service_cross_reference(
        item_id=item_id,
        ownership_state=ownership_state,
        surface=surface,
        is_service_row=is_service_row,
        service_ids=service_ids,
    )
    issues.extend(service_issues)
    return issues, covered_service, deferred_surface


def validate_parity_ledger(model: dict[str, Any]) -> list[CheckResult]:
    """Detect drift between the runtime graph and the declared parity ledger.

    Implements the `doctor` drift surface from shared.md US-4 and backend.md
    Business Rule 6. Issues are reported with the stable
    ``LOCAL_RUNTIME_COVERAGE_GAP`` code so downstream CLI formatters can map
    them back to the shared contract.

    Drift conditions flagged here:

    * ``bridge_dependency`` references an id that is not a declared bridge
      (shared.md:279-281 restricts this field to ``legacy_env_bridge.id``
      and explicitly forbids ``bootstrap_task.id``).
    * ``action`` or ``ownership_state`` uses an unknown value.
    * A parity-ledger item is marked ``ownership_state == "covered"`` but no
      managed_service with the same id exists in the runtime graph.
    * A parity-ledger item is marked ``ownership_state == "deferred"`` or
      ``"bridge-only"`` but a managed_service with the same id is declared
      (runtime claims coverage the ledger denies).
    * ``request_error`` is set to a value outside the canonical seven.
    """
    ledger = model.get("parity_ledger") or []
    if not ledger:
        return [
            CheckResult(
                status="pass",
                code="parity-ledger",
                message="no parity-ledger items declared",
                details={"deferred_surfaces": [], "covered_services": []},
            )
        ]

    bridge_ids, task_ids, service_ids = _parity_ledger_graph_ids(model)
    issues: list[str] = []
    covered_services: list[str] = []
    deferred_surfaces: list[str] = []

    for item in ledger:
        item_issues, covered_service, deferred_surface = _parity_ledger_item_result(
            item,
            bridge_ids=bridge_ids,
            task_ids=task_ids,
            service_ids=service_ids,
        )
        issues.extend(item_issues)
        if covered_service is not None:
            covered_services.append(covered_service)
        if deferred_surface is not None:
            deferred_surfaces.append(deferred_surface)

    if issues:
        return [
            CheckResult(
                status="fail",
                code=LOCAL_RUNTIME_COVERAGE_GAP,
                message="parity ledger drifts from the declared runtime graph",
                details={
                    "error_code": LOCAL_RUNTIME_COVERAGE_GAP,
                    "issues": issues,
                    "covered_services": sorted(set(covered_services)),
                    "deferred_surfaces": sorted(set(deferred_surfaces)),
                },
            )
        ]

    return [
        CheckResult(
            status="pass",
            code="parity-ledger",
            message="parity ledger matches the declared runtime graph",
            details={
                "covered_services": sorted(set(covered_services)),
                "deferred_surfaces": sorted(set(deferred_surfaces)),
            },
        )
    ]
