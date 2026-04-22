from __future__ import annotations

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
)

VALID_INGRESS_ROUTE_LISTENERS = {"public", "private"}
VALID_INGRESS_ROUTE_MATCHES = {"exact", "prefix"}
CLIENT_SHARED_SKILLS_REL = "../_shared/skills"
VENDORED_SHARED_SKILLS_ESCAPE_HATCH = "allow_vendored_shared_skills"


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
        raise RuntimeError(
            "Unknown runtime client(s): "
            + ", ".join(unknown_clients)
            + ". Available clients: "
            + (", ".join(sorted(available_clients)) or "(none)")
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


def filter_model(model: dict[str, Any], active_profiles: set[str], active_clients: set[str]) -> dict[str, Any]:
    if not active_profiles and not active_clients:
        return model

    filtered_model = dict(model)
    filtered_model["active_profiles"] = sorted(active_profiles)
    filtered_model["active_clients"] = sorted(active_clients)
    filtered_model["clients"] = [
        copy.deepcopy(client)
        for client in model["clients"]
        if not active_clients or str(client.get("id", "")).strip() in active_clients
    ]
    filtered_model["repos"] = [
        copy.deepcopy(repo)
        for repo in model["repos"]
        if item_matches_profiles(repo, active_profiles) and item_matches_clients(repo, active_clients)
    ]
    filtered_model["artifacts"] = [
        copy.deepcopy(artifact)
        for artifact in model["artifacts"]
        if item_matches_profiles(artifact, active_profiles) and item_matches_clients(artifact, active_clients)
    ]
    filtered_model["env_files"] = [
        copy.deepcopy(env_file)
        for env_file in model["env_files"]
        if item_matches_profiles(env_file, active_profiles) and item_matches_clients(env_file, active_clients)
    ]
    filtered_model["skills"] = [
        copy.deepcopy(skillset)
        for skillset in model["skills"]
        if item_matches_profiles(skillset, active_profiles) and item_matches_clients(skillset, active_clients)
    ]
    filtered_model["tasks"] = [
        copy.deepcopy(task)
        for task in model["tasks"]
        if item_matches_profiles(task, active_profiles) and item_matches_clients(task, active_clients)
    ]
    filtered_model["services"] = [
        copy.deepcopy(service)
        for service in model["services"]
        if item_matches_profiles(service, active_profiles) and item_matches_clients(service, active_clients)
    ]
    filtered_model["logs"] = [
        copy.deepcopy(log_item)
        for log_item in model["logs"]
        if item_matches_profiles(log_item, active_profiles) and item_matches_clients(log_item, active_clients)
    ]
    filtered_model["checks"] = [
        copy.deepcopy(check)
        for check in model["checks"]
        if item_matches_profiles(check, active_profiles) and item_matches_clients(check, active_clients)
    ]
    filtered_model["bridges"] = [
        copy.deepcopy(bridge)
        for bridge in model.get("bridges") or []
        if item_matches_profiles(bridge, active_profiles) and item_matches_clients(bridge, active_clients)
    ]
    filtered_model["ingress_routes"] = [
        copy.deepcopy(route)
        for route in model.get("ingress_routes") or []
        if item_matches_profiles(route, active_profiles) and item_matches_clients(route, active_clients)
    ]
    filtered_model["parity_ledger"] = [
        copy.deepcopy(item)
        for item in model.get("parity_ledger") or []
        if parity_ledger_item_matches_profiles(item, active_profiles)
        and item_matches_clients(item, active_clients)
    ]

    included_repo_ids = {repo["id"] for repo in filtered_model["repos"]}
    included_artifact_ids = {artifact["id"] for artifact in filtered_model["artifacts"]}
    included_task_ids = {task["id"] for task in filtered_model["tasks"]}
    included_log_ids = {log_item["id"] for log_item in filtered_model["logs"]}

    tasks_by_id = {
        str(task["id"]): task
        for task in model["tasks"]
        if str(task.get("id", "")).strip()
    }

    def raw_task_dependency_ids(task: dict[str, Any]) -> list[str]:
        raw_dependencies = task.get("depends_on") or []
        if not isinstance(raw_dependencies, list):
            return []

        dependency_ids: list[str] = []
        seen_dependency_ids: set[str] = set()
        for raw_dependency in raw_dependencies:
            dependency_id = str(raw_dependency).strip()
            if not dependency_id or dependency_id in seen_dependency_ids:
                continue
            dependency_ids.append(dependency_id)
            seen_dependency_ids.add(dependency_id)
        return dependency_ids

    def raw_service_bootstrap_task_ids(service: dict[str, Any]) -> list[str]:
        raw_tasks = service.get("bootstrap_tasks") or []
        if not isinstance(raw_tasks, list):
            return []

        task_ids: list[str] = []
        seen_task_ids: set[str] = set()
        for raw_task in raw_tasks:
            task_id = str(raw_task).strip()
            if not task_id or task_id in seen_task_ids:
                continue
            task_ids.append(task_id)
            seen_task_ids.add(task_id)
        return task_ids

    def include_task(task_id: str) -> None:
        task = tasks_by_id.get(task_id)
        if task is None:
            return
        if not item_matches_profiles(task, active_profiles) or not item_matches_clients(task, active_clients):
            return
        for dependency_id in raw_task_dependency_ids(task):
            include_task(dependency_id)
        if task_id in included_task_ids:
            return
        filtered_model["tasks"].append(copy.deepcopy(task))
        included_task_ids.add(task_id)

    for service in filtered_model["services"]:
        for task_id in raw_service_bootstrap_task_ids(service):
            include_task(task_id)

    for task in list(filtered_model["tasks"]):
        for dependency_id in raw_task_dependency_ids(task):
            include_task(dependency_id)

    for service in filtered_model["services"]:
        service["bootstrap_tasks"] = [
            task_id
            for task_id in raw_service_bootstrap_task_ids(service)
            if task_id in included_task_ids
        ]

    for task in filtered_model["tasks"]:
        task["depends_on"] = [
            dependency_id
            for dependency_id in raw_task_dependency_ids(task)
            if dependency_id in included_task_ids
        ]

    required_repo_ids = {
        str(service["repo"])
        for service in filtered_model["services"]
        if service.get("repo")
    } | {
        str(task["repo"])
        for task in filtered_model["tasks"]
        if task.get("repo")
    }
    required_artifact_ids = {
        str(service["artifact"])
        for service in filtered_model["services"]
        if service.get("artifact")
    }
    required_log_ids = {
        str(service["log"])
        for service in filtered_model["services"]
        if service.get("log")
    } | {
        str(task["log"])
        for task in filtered_model["tasks"]
        if task.get("log")
    }

    for repo in model["repos"]:
        repo_id = str(repo.get("id", "")).strip()
        if repo_id and repo_id in required_repo_ids and repo_id not in included_repo_ids:
            filtered_model["repos"].append(copy.deepcopy(repo))
            included_repo_ids.add(repo_id)

    for artifact in model["artifacts"]:
        artifact_id = str(artifact.get("id", "")).strip()
        if artifact_id and artifact_id in required_artifact_ids and artifact_id not in included_artifact_ids:
            filtered_model["artifacts"].append(copy.deepcopy(artifact))
            included_artifact_ids.add(artifact_id)

    for log_item in model["logs"]:
        log_id = str(log_item.get("id", "")).strip()
        if log_id and log_id in required_log_ids and log_id not in included_log_ids:
            filtered_model["logs"].append(copy.deepcopy(log_item))
            included_log_ids.add(log_id)

    return filtered_model

def lock_skill_map(lock_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    skills = lock_payload.get("skills") or []
    if not isinstance(skills, list):
        raise RuntimeError("Lockfile field 'skills' must be a list")

    mapping: dict[str, dict[str, Any]] = {}
    for item in skills:
        if not isinstance(item, dict):
            raise RuntimeError("Lockfile skill entries must be objects")
        name = str(item.get("name", "")).strip()
        if not name:
            raise RuntimeError("Lockfile skill entries must include a non-empty name")
        if name in mapping:
            raise RuntimeError(f"Lockfile contains duplicate skill entry {name!r}")

        targets = item.get("targets") or []
        if not isinstance(targets, list):
            raise RuntimeError(f"Lockfile skill {name!r} has a non-list targets field")

        targets_by_id: dict[str, dict[str, Any]] = {}
        for target in targets:
            if not isinstance(target, dict):
                raise RuntimeError(f"Lockfile skill {name!r} contains a non-object target entry")
            target_id = str(target.get("id", "")).strip()
            if not target_id:
                raise RuntimeError(f"Lockfile skill {name!r} contains a target without an id")
            if target_id in targets_by_id:
                raise RuntimeError(f"Lockfile skill {name!r} contains duplicate target {target_id!r}")
            targets_by_id[target_id] = target

        mapping[name] = item | {"targets_by_id": targets_by_id}

    return mapping


def collect_skill_inventory(skillset: dict[str, Any]) -> dict[str, Any]:
    bundle_dir = Path(str(skillset["bundle_dir_host_path"]))
    manifest_path = Path(str(skillset["manifest_host_path"]))
    sources_config_path = Path(str(skillset["sources_config_host_path"]))
    lock_path = Path(str(skillset["lock_path_host_path"]))

    manifest_exists = manifest_path.is_file()
    sources_exists = sources_config_path.is_file()
    bundle_dir_exists = bundle_dir.is_dir()

    expected_skills = read_manifest_skills(manifest_path) if manifest_exists else []
    bundles: dict[str, dict[str, Any]] = {}
    if bundle_dir_exists:
        for bundle_path in sorted(bundle_dir.glob("*.skill")):
            bundles[bundle_path.stem] = bundle_metadata(bundle_path, expected_skill_name=bundle_path.stem)

    missing_bundles = sorted(name for name in expected_skills if name not in bundles)
    extra_bundles = sorted(name for name in bundles if name not in expected_skills)

    lock_payload: dict[str, Any] | None = None
    lock_error: str | None = None
    if lock_path.exists():
        try:
            lock_payload = load_json_file(lock_path)
            lock_skill_map(lock_payload)
        except RuntimeError as exc:
            lock_error = str(exc)

    lock_skills: dict[str, dict[str, Any]] = {}
    if lock_payload and not lock_error:
        lock_skills = lock_skill_map(lock_payload)

    skill_names = list(expected_skills)
    for extra_name in sorted(set(bundles) - set(skill_names)):
        skill_names.append(extra_name)
    for lock_name in sorted(set(lock_skills) - set(skill_names)):
        skill_names.append(lock_name)

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

    skills: list[dict[str, Any]] = []
    for skill_name in skill_names:
        bundle_record = bundles.get(skill_name)
        lock_record = lock_skills.get(skill_name)
        skill_entry = {
            "name": skill_name,
            "bundle_present": bundle_record is not None,
            "bundle_state": "missing" if bundle_record is None else "present",
            "bundle_sha256": bundle_record.get("bundle_sha256") if bundle_record else None,
            "bundle_tree_sha256": bundle_record.get("bundle_tree_sha256") if bundle_record else None,
            "targets": [],
        }

        if bundle_record and lock_record:
            if (
                lock_record.get("bundle_sha256") == bundle_record["bundle_sha256"]
                and lock_record.get("bundle_tree_sha256") == bundle_record["bundle_tree_sha256"]
            ):
                skill_entry["bundle_state"] = "ok"
            else:
                skill_entry["bundle_state"] = "drift"
        elif bundle_record and lock_payload:
            skill_entry["bundle_state"] = "untracked"

        for target in target_states:
            install_dir = Path(target["host_path"]) / skill_name
            install_tree_sha = directory_tree_sha256(install_dir)
            target_lock = lock_record.get("targets_by_id", {}).get(target["id"]) if lock_record else None

            target_state = "missing"
            if install_dir.exists():
                target_state = "present"
            if target_lock:
                if install_tree_sha is None:
                    target_state = "missing"
                elif target_lock.get("tree_sha256") == install_tree_sha:
                    target_state = "ok"
                else:
                    target_state = "drift"
            elif install_tree_sha is not None and lock_payload:
                target_state = "untracked"

            skill_entry["targets"].append(
                {
                    "id": target["id"],
                    "path": str(target["path"]),
                    "host_path": str(install_dir),
                    "present": install_dir.exists(),
                    "tree_sha256": install_tree_sha,
                    "state": target_state,
                }
            )

        skills.append(skill_entry)

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
    from .publish import extract_bundle_to_target

    actions: list[str] = []

    for skillset in model["skills"]:
        if skillset.get("kind") == "skill-repo-set":
            continue
        inventory = collect_skill_inventory(skillset)
        missing_inputs: list[str] = []
        for field, present in (
            ("bundle_dir", inventory["bundle_dir_exists"]),
            ("manifest", inventory["manifest_exists"]),
            ("sources_config", inventory["sources_config_exists"]),
        ):
            if not present:
                missing_inputs.append(field)
        if missing_inputs:
            raise RuntimeError(
                f"Skill set {skillset['id']} is missing required files: {', '.join(missing_inputs)}"
            )
        if inventory["missing_bundles"]:
            raise RuntimeError(
                f"Skill set {skillset['id']} is missing bundles for: {', '.join(inventory['missing_bundles'])}"
            )

        if inventory["extra_bundles"]:
            actions.append(
                f"ignore-extra-bundles: {skillset['id']} -> {', '.join(inventory['extra_bundles'])}"
            )

        for target in skillset.get("install_targets") or []:
            target_root = Path(str(target["host_path"]))
            ensure_directory(target_root, dry_run)
            actions.append(f"ensure-directory: {target_root}")

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

        lock_path = Path(str(skillset["lock_path_host_path"]))
        if dry_run:
            actions.append(f"write-lockfile: {lock_path}")
            continue

        lock_payload = build_skill_lock(skillset, inventory, install_hashes)
        changed = write_json_file(lock_path, lock_payload)
        actions.append(f"{'write-lockfile' if changed else 'lockfile-unchanged'}: {lock_path}")

    return actions


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

        required_missing: list[str] = []
        for label, present, display_path in (
            ("bundle_dir", inventory["bundle_dir_exists"], inventory["bundle_dir_host_path"]),
            ("manifest", inventory["manifest_exists"], inventory["manifest_host_path"]),
            ("sources_config", inventory["sources_config_exists"], inventory["sources_config_host_path"]),
        ):
            if not present:
                required_missing.append(f"{skillset['id']}: missing {label} at {display_path}")

        if required_missing:
            bundle_failures.extend(required_missing)
            continue

        if inventory["missing_bundles"]:
            bundle_failures.append(
                f"{skillset['id']}: missing bundles for {', '.join(inventory['missing_bundles'])}"
            )
        if inventory["extra_bundles"]:
            bundle_warnings.append(
                f"{skillset['id']}: extra bundles present for {', '.join(inventory['extra_bundles'])}"
            )

        if inventory["lock_error"]:
            lock_failures.append(f"{skillset['id']}: {inventory['lock_error']}")
        elif not inventory["lock_present"]:
            lock_warnings.append(
                f"{skillset['id']}: lockfile missing at {inventory['lock_path_host_path']}"
            )
        else:
            lock_payload = inventory["lock_payload"] or {}
            if lock_payload.get("version") != LOCKFILE_VERSION:
                lock_failures.append(
                    f"{skillset['id']}: lockfile version {lock_payload.get('version')!r} does not match {LOCKFILE_VERSION}"
                )
            if lock_payload.get("id") != skillset["id"]:
                lock_failures.append(f"{skillset['id']}: lockfile id does not match the skill set id")
            if lock_payload.get("manifest_sha256") != inventory["manifest_sha256"]:
                lock_failures.append(f"{skillset['id']}: lockfile manifest digest is stale")
            if lock_payload.get("sources_config_sha256") != inventory["sources_config_sha256"]:
                lock_failures.append(f"{skillset['id']}: lockfile sources config digest is stale")

            indexed_lock = lock_skill_map(lock_payload)
            expected_skill_names = set(inventory["expected_skills"])
            if set(indexed_lock) - expected_skill_names:
                extras = ", ".join(sorted(set(indexed_lock) - expected_skill_names))
                lock_failures.append(f"{skillset['id']}: lockfile contains extra skills: {extras}")

            for skill_name in inventory["expected_skills"]:
                lock_record = indexed_lock.get(skill_name)
                if lock_record is None:
                    lock_failures.append(f"{skillset['id']}: lockfile is missing skill {skill_name}")
                    continue

                bundle_record = inventory["bundles"].get(skill_name)
                if bundle_record is None:
                    continue

                if lock_record.get("bundle_sha256") != bundle_record["bundle_sha256"]:
                    lock_failures.append(
                        f"{skillset['id']}: lockfile bundle digest is stale for {skill_name}"
                    )
                if lock_record.get("bundle_tree_sha256") != bundle_record["bundle_tree_sha256"]:
                    lock_failures.append(
                        f"{skillset['id']}: lockfile bundle tree digest is stale for {skill_name}"
                    )

                lock_targets = lock_record.get("targets_by_id", {})
                configured_targets = {target["id"] for target in skillset.get("install_targets") or []}
                if set(lock_targets) - configured_targets:
                    extras = ", ".join(sorted(set(lock_targets) - configured_targets))
                    lock_failures.append(
                        f"{skillset['id']}: lockfile contains unexpected targets for {skill_name}: {extras}"
                    )

                missing_targets = sorted(configured_targets - set(lock_targets))
                if missing_targets:
                    lock_failures.append(
                        f"{skillset['id']}: lockfile is missing targets for {skill_name}: {', '.join(missing_targets)}"
                    )

        for skill_entry in inventory["skills"]:
            bundle_state = skill_entry["bundle_state"]
            if bundle_state == "drift":
                install_failures.append(
                    f"{skillset['id']}: bundle digest drift detected for {skill_entry['name']}"
                )
            elif bundle_state == "untracked" and inventory["lock_present"]:
                install_failures.append(
                    f"{skillset['id']}: bundle {skill_entry['name']} is not represented in the lockfile"
                )

            for target in skill_entry["targets"]:
                if target["state"] == "drift":
                    install_failures.append(
                        f"{skillset['id']}: installed drift for {skill_entry['name']} in {target['id']}"
                    )
                elif target["state"] == "untracked":
                    install_failures.append(
                        f"{skillset['id']}: unmanaged install for {skill_entry['name']} in {target['id']}"
                    )
                elif target["state"] == "missing":
                    install_warnings.append(
                        f"{skillset['id']}: missing install for {skill_entry['name']} in {target['id']}"
                    )

    results: list[CheckResult] = []
    if bundle_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-bundle-state",
                message="managed skill bundles do not satisfy the declared manifest",
                details={"issues": bundle_failures},
            )
        )
    elif bundle_warnings:
        results.append(
            CheckResult(
                status="warn",
                code="skill-bundle-state",
                message="managed skill bundle directory contains undeclared bundles",
                details={"issues": bundle_warnings},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-bundle-state",
                message="managed skill bundle directories satisfy the declared manifests",
            )
        )

    if lock_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-lock-state",
                message="managed skill lockfiles are invalid or stale",
                details={"issues": lock_failures},
            )
        )
    elif lock_warnings:
        results.append(
            CheckResult(
                status="warn",
                code="skill-lock-state",
                message="managed skill lockfiles have not been generated yet",
                details={"issues": lock_warnings},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-lock-state",
                message="managed skill lockfiles match the current bundle and source manifests",
            )
        )

    if install_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-install-state",
                message="installed skill directories drifted from the managed bundles",
                details={"issues": install_failures},
            )
        )
    elif install_warnings:
        results.append(
            CheckResult(
                status="warn",
                code="skill-install-state",
                message="managed skill installs are missing and can be created by sync",
                details={"issues": install_warnings},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-install-state",
                message="managed skill installs match the lockfile and bundle contents",
            )
        )

    return results


def validate_skill_repo_sets(model: dict[str, Any]) -> list[CheckResult]:
    """Validate skill-repo-set skillsets using 2-layer drift detection."""
    from .distribution.doctor import validate_distribution_doctor_checks

    distribution_results = validate_distribution_doctor_checks(model)
    has_repo_sets = any(s.get("kind") == "skill-repo-set" for s in model["skills"])
    if not has_repo_sets:
        return distribution_results

    config_failures: list[str] = []
    lock_failures: list[str] = []
    lock_warnings: list[str] = []
    install_failures: list[str] = []
    install_warnings: list[str] = []
    shared_source_failures: list[str] = []
    declared_skills_by_skillset: dict[str, set[str]] = {}
    effective_skill_owner: dict[str, str] = {}

    def validate_shared_client_planning_sources(
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

            picked_names = {
                str(item).strip()
                for item in entry.get("pick") or []
                if str(item).strip()
            }
            protected_picks = sorted(picked_names & protected_names)
            if not protected_picks:
                continue

            if bool(entry.get(VENDORED_SHARED_SKILLS_ESCAPE_HATCH)):
                continue

            source_path = Path(local_path).expanduser()
            if not source_path.is_absolute():
                source_path = overlay_dir / source_path

            if not source_path.exists() and not source_path.is_symlink():
                failures.append(
                    f"{client_id}: skill_repos[{index}] references missing local path "
                    f"{repo_rel(DEFAULT_ROOT_DIR, source_path)} for protected planning skills "
                    f"{', '.join(protected_picks)}; point it at {CLIENT_SHARED_SKILLS_REL} "
                    f"or set {VENDORED_SHARED_SKILLS_ESCAPE_HATCH}: true when divergence is intentional"
                )
                continue

            if source_path.resolve() != expected_shared_source:
                failures.append(
                    f"{client_id}: skill_repos[{index}] sources protected planning skills "
                    f"{', '.join(protected_picks)} from {repo_rel(DEFAULT_ROOT_DIR, source_path)}; "
                    f"point it at {CLIENT_SHARED_SKILLS_REL} or set "
                    f"{VENDORED_SHARED_SKILLS_ESCAPE_HATCH}: true when divergence is intentional"
                )

        return failures

    def declared_skill_names(config: dict[str, Any]) -> set[str]:
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

        declared = declared_skill_names(config)
        declared_skills_by_skillset[sid] = declared
        for skill_name in declared:
            effective_skill_owner[skill_name] = sid

    for skillset in model["skills"]:
        if skillset.get("kind") != "skill-repo-set":
            continue

        sid = skillset["id"]
        config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
        lock_path = Path(str(skillset.get("lock_path_host_path", "")))

        if not config_path.is_file():
            config_failures.append(f"{sid}: skill_repos config missing at {config_path}")
            continue

        try:
            config = load_skill_repos_config(config_path)
        except RuntimeError as exc:
            config_failures.append(f"{sid}: {exc}")
            continue

        shared_source_failures.extend(
            validate_shared_client_planning_sources(skillset, config_path, config)
        )

        config_sha = file_sha256(config_path)
        declared_skill_names_for_set = declared_skills_by_skillset.get(sid, set())

        if not lock_path.is_file():
            lock_warnings.append(f"{sid}: lockfile missing at {lock_path} — run sync")
            continue

        try:
            lock_payload = load_json_file(lock_path)
        except RuntimeError as exc:
            lock_failures.append(f"{sid}: {exc}")
            continue

        if lock_payload.get("version") != SKILL_REPOS_LOCKFILE_VERSION:
            lock_failures.append(
                f"{sid}: lockfile version {lock_payload.get('version')!r} "
                f"does not match {SKILL_REPOS_LOCKFILE_VERSION}"
            )
            continue

        if lock_payload.get("config_sha") != config_sha:
            lock_failures.append(f"{sid}: config changed since last sync (config_sha mismatch)")

        lock_skills_by_name: dict[str, dict[str, Any]] = {}
        for skill_entry in lock_payload.get("skills") or []:
            name = str(skill_entry.get("name", ""))
            if name:
                lock_skills_by_name[name] = skill_entry

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

        installed_skill_names: set[str] = set()
        for target in skillset.get("install_targets") or []:
            target_root = Path(str(target["host_path"]))
            if target_root.is_dir():
                for child in target_root.iterdir():
                    if child.is_dir() and (child / "SKILL.md").is_file():
                        installed_skill_names.add(child.name)
            break

        all_declared: set[str] = set()
        for s in model["skills"]:
            if s.get("kind") != "skill-repo-set":
                continue
            sp = Path(str(s.get("skill_repos_config_host_path", "")))
            if sp.is_file():
                try:
                    sc = load_skill_repos_config(sp)
                    for e in sc.get("skill_repos") or []:
                        p = e.get("pick")
                        if p:
                            all_declared.update(p)
                        elif e.get("repo"):
                            r = e["repo"]
                            all_declared.add(r.split("/")[-1] if "/" in r else r)
                except RuntimeError:
                    pass

        extras = installed_skill_names - all_declared
        for extra_name in sorted(extras):
            install_warnings.append(
                f"{sid}: SKILL_EXTRA_INSTALLED: {extra_name} is installed but not declared"
            )

    results: list[CheckResult] = []

    if config_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-repo-config",
                message="skill repo config is invalid or missing",
                details={"issues": config_failures},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-repo-config",
                message="skill repo configs are valid",
            )
        )

    if shared_source_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-repo-shared-source",
                message="client planning skill bundles must use the shared source contract",
                details={"issues": shared_source_failures},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-repo-shared-source",
                message="client planning skill bundles honor the shared source contract",
            )
        )

    if lock_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-repo-lock",
                message="skill repo lockfiles are invalid or stale",
                details={"issues": lock_failures},
            )
        )
    elif lock_warnings:
        results.append(
            CheckResult(
                status="warn",
                code="skill-repo-lock",
                message="skill repo lockfiles need generation",
                details={"issues": lock_warnings},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-repo-lock",
                message="skill repo lockfiles match the current config",
            )
        )

    if install_failures:
        results.append(
            CheckResult(
                status="fail",
                code="skill-repo-install",
                message="installed skills drifted from declared repos",
                details={"issues": install_failures},
            )
        )
    elif install_warnings:
        results.append(
            CheckResult(
                status="warn",
                code="skill-repo-install",
                message="extra skills installed that are not declared",
                details={"issues": install_warnings},
            )
        )
    else:
        results.append(
            CheckResult(
                status="pass",
                code="skill-repo-install",
                message="installed skills match declared repo state",
            )
        )

    return results + distribution_results


def check_manifest(model: dict[str, Any]) -> list[CheckResult]:
    issues: list[str] = []

    client_ids = find_duplicates(model.get("clients") or [], "id")
    if client_ids:
        issues.append(f"clients contain duplicate ids: {', '.join(client_ids)}")
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

    for section in ("repos", "artifacts", "env_files", "skills", "tasks", "services", "logs", "checks", "ingress_routes"):
        duplicates = find_duplicates(model.get(section) or [], "id")
        if duplicates:
            issues.append(f"{section} contain duplicate ids: {', '.join(duplicates)}")

    duplicate_repo_paths = find_duplicates(model["repos"], "path")
    if duplicate_repo_paths:
        issues.append(f"repos contain duplicate paths: {', '.join(duplicate_repo_paths)}")

    duplicate_log_paths = find_duplicates(model["logs"], "path")
    if duplicate_log_paths:
        issues.append(f"logs contain duplicate paths: {', '.join(duplicate_log_paths)}")

    duplicate_artifact_paths = find_duplicates(model["artifacts"], "path")
    if duplicate_artifact_paths:
        issues.append(f"artifacts contain duplicate paths: {', '.join(duplicate_artifact_paths)}")

    duplicate_env_file_paths = find_duplicates(model["env_files"], "path")
    if duplicate_env_file_paths:
        issues.append(f"env_files contain duplicate paths: {', '.join(duplicate_env_file_paths)}")

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

    for artifact in model["artifacts"]:
        if not artifact.get("id"):
            issues.append("every artifact entry must have an id")
        if not artifact.get("path"):
            issues.append(f"artifact {artifact.get('id', '(missing id)')} is missing path")
        if artifact.get("client") and artifact["client"] not in declared_client_ids:
            issues.append(f"artifact {artifact.get('id')} references unknown client {artifact['client']!r}")

        source = artifact.get("source") or {}
        source_kind = source.get("kind", "manual")
        if source_kind not in VALID_ARTIFACT_SOURCE_KINDS:
            issues.append(f"artifact {artifact.get('id')} has unsupported source.kind {source_kind!r}")

        sync = artifact.get("sync") or {}
        sync_mode = sync.get("mode") or (
            "download-if-missing" if source_kind == "url" else "copy-if-missing" if source_kind == "file" else "manual"
        )
        if sync_mode not in VALID_ARTIFACT_SYNC_MODES:
            issues.append(f"artifact {artifact.get('id')} has unsupported sync.mode {sync_mode!r}")
        if source_kind == "url" and str(source.get("url") or "").strip():
            try:
                validate_url_download_source(source, artifact_id=str(artifact.get("id", "(missing id)")))
            except RuntimeError as exc:
                issues.append(str(exc))

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

    for skillset in model["skills"]:
        if not skillset.get("id"):
            issues.append("every skills entry must have an id")
        if skillset.get("client") and skillset["client"] not in declared_client_ids:
            issues.append(f"skill set {skillset.get('id')} references unknown client {skillset['client']!r}")

        kind = skillset.get("kind", "packaged-skill-set")
        if kind == "skill-repo-set":
            for field in ("skill_repos_config", "lock_path"):
                if not skillset.get(field):
                    issues.append(f"skill set {skillset.get('id', '(missing id)')} is missing {field}")
        else:
            for field in ("bundle_dir", "manifest", "sources_config", "lock_path"):
                if not skillset.get(field):
                    issues.append(f"skill set {skillset.get('id', '(missing id)')} is missing {field}")

        sync = skillset.get("sync") or {}
        sync_mode = sync.get("mode") or "unpack-bundles"
        if sync_mode not in VALID_SKILL_SYNC_MODES:
            issues.append(f"skill set {skillset.get('id')} has unsupported sync.mode {sync_mode!r}")

        targets = skillset.get("install_targets") or []
        if not targets:
            issues.append(f"skill set {skillset.get('id')} must declare at least one install target")
            continue

        target_ids = find_duplicates(targets, "id")
        if target_ids:
            issues.append(f"skill set {skillset.get('id')} contains duplicate target ids: {', '.join(target_ids)}")

        for target in targets:
            if not target.get("id"):
                issues.append(f"skill set {skillset.get('id')} contains a target without an id")
            if not target.get("path"):
                issues.append(f"skill set {skillset.get('id')} target {target.get('id', '(missing id)')} is missing path")

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

        raw_dependencies = task.get("depends_on") or []
        if raw_dependencies and not isinstance(raw_dependencies, list):
            issues.append(f"task {task.get('id')} has non-list depends_on")
            raw_dependencies = []

        dependency_ids: list[str] = []
        seen_dependency_ids: set[str] = set()
        for raw_dependency in raw_dependencies:
            dependency_id = str(raw_dependency).strip()
            if not dependency_id:
                issues.append(f"task {task.get('id')} contains an empty depends_on entry")
                continue
            if dependency_id in seen_dependency_ids:
                issues.append(f"task {task.get('id')} contains duplicate depends_on entry {dependency_id!r}")
                continue
            if dependency_id == task_id:
                issues.append(f"task {task.get('id')} cannot depend on itself")
                continue
            if dependency_id not in task_ids:
                issues.append(f"task {task.get('id')} references unknown dependency {dependency_id!r}")
                continue
            dependency_ids.append(dependency_id)
            seen_dependency_ids.add(dependency_id)
        if task_id:
            task_dependency_map[task_id] = dependency_ids

        success = task.get("success") or {}
        success_type = success.get("type")
        if not success_type:
            issues.append(f"task {task.get('id', '(missing id)')} is missing success.type")
        elif success_type not in VALID_TASK_SUCCESS_TYPES:
            issues.append(f"task {task.get('id')} has unsupported success.type {success_type!r}")
        if success_type == "path_exists" and not success.get("path"):
            issues.append(f"task {task.get('id')} path_exists success is missing path")
        if success_type == "all_outputs_exist":
            target = str(success.get("target") or "").strip()
            if not target:
                issues.append(f"task {task.get('id')} all_outputs_exist success is missing target")
            elif target not in bridge_ids:
                issues.append(
                    f"task {task.get('id')} all_outputs_exist success references unknown bridge {target!r}"
                )
        if success_type == "port_listening" and not _is_int_port(success.get("port")):
            issues.append(f"task {task.get('id')} port_listening success is missing integer port")

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

        raw_dependencies = service.get("depends_on") or []
        if raw_dependencies and not isinstance(raw_dependencies, list):
            issues.append(f"service {service.get('id')} has non-list depends_on")
            raw_dependencies = []

        dependency_ids: list[str] = []
        seen_dependency_ids: set[str] = set()
        for raw_dependency in raw_dependencies:
            dependency_id = str(raw_dependency).strip()
            if not dependency_id:
                issues.append(f"service {service.get('id')} contains an empty depends_on entry")
                continue
            if dependency_id in seen_dependency_ids:
                issues.append(f"service {service.get('id')} contains duplicate depends_on entry {dependency_id!r}")
                continue
            if dependency_id == service_id:
                issues.append(f"service {service.get('id')} cannot depend on itself")
                continue
            if dependency_id not in service_ids and dependency_id not in artifact_ids:
                issues.append(f"service {service.get('id')} references unknown dependency {dependency_id!r}")
                continue
            dependency_ids.append(dependency_id)
            seen_dependency_ids.add(dependency_id)
        if service_id:
            service_dependency_map[service_id] = dependency_ids

        raw_bootstrap_tasks = service.get("bootstrap_tasks") or []
        if raw_bootstrap_tasks and not isinstance(raw_bootstrap_tasks, list):
            issues.append(f"service {service.get('id')} has non-list bootstrap_tasks")
            raw_bootstrap_tasks = []

        seen_bootstrap_tasks: set[str] = set()
        for raw_task in raw_bootstrap_tasks:
            task_id = str(raw_task).strip()
            if not task_id:
                issues.append(f"service {service.get('id')} contains an empty bootstrap_tasks entry")
                continue
            if task_id in seen_bootstrap_tasks:
                issues.append(f"service {service.get('id')} contains duplicate bootstrap_tasks entry {task_id!r}")
                continue
            if task_id not in task_ids:
                issues.append(f"service {service.get('id')} references unknown bootstrap task {task_id!r}")
                continue
            seen_bootstrap_tasks.add(task_id)

        healthcheck = service.get("healthcheck") or {}
        healthcheck_type = healthcheck.get("type")
        if healthcheck_type:
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

    visiting: list[str] = []
    visited: set[str] = set()

    def visit_service_dependency(service_id: str) -> None:
        if service_id in visited:
            return
        if service_id in visiting:
            cycle_start = visiting.index(service_id)
            cycle = visiting[cycle_start:] + [service_id]
            issues.append("service dependency cycle detected: " + " -> ".join(cycle))
            return

        visiting.append(service_id)
        for dependency_id in service_dependency_map.get(service_id, []):
            visit_service_dependency(dependency_id)
        visiting.pop()
        visited.add(service_id)

    for service_id in sorted(service_dependency_map):
        visit_service_dependency(service_id)

    ingress_route_conflicts: set[tuple[str, str, str]] = set()
    for route in model.get("ingress_routes") or []:
        route_id = str(route.get("id", "")).strip()
        if not route_id:
            issues.append("every ingress_routes entry must have an id")
        if route.get("client") and route["client"] not in declared_client_ids:
            issues.append(f"ingress route {route.get('id')} references unknown client {route['client']!r}")

        service_id = str(route.get("service_id") or "").strip()
        if not service_id:
            issues.append(f"ingress route {route.get('id', '(missing id)')} is missing service_id")
        elif service_id not in service_ids:
            issues.append(f"ingress route {route.get('id')} references unknown service {service_id!r}")
        elif not _looks_like_ingress_origin(services_by_id[service_id].get("origin_url")):
            issues.append(
                f"ingress route {route.get('id')} references service {service_id!r} without a valid origin_url"
            )

        listener = str(route.get("listener") or "").strip().lower()
        if listener not in VALID_INGRESS_ROUTE_LISTENERS:
            issues.append(
                f"ingress route {route.get('id')} has unsupported listener {route.get('listener')!r}"
            )

        path = str(route.get("path") or "").strip()
        if not path:
            issues.append(f"ingress route {route.get('id', '(missing id)')} is missing path")
        elif not path.startswith("/"):
            issues.append(f"ingress route {route.get('id')} path must start with '/'")

        match = str(route.get("match") or "exact").strip().lower()
        if match not in VALID_INGRESS_ROUTE_MATCHES:
            issues.append(
                f"ingress route {route.get('id')} has unsupported match {route.get('match')!r}"
            )

        conflict_key = (listener or "public", path, match or "exact")
        if path:
            if conflict_key in ingress_route_conflicts:
                issues.append(
                    "ingress routes contain duplicate listener/path/match: "
                    f"{listener or 'public'} {path} ({match or 'exact'})"
                )
            ingress_route_conflicts.add(conflict_key)

    task_visiting: list[str] = []
    task_visited: set[str] = set()

    def visit_task_dependency(task_id: str) -> None:
        if task_id in task_visited:
            return
        if task_id in task_visiting:
            cycle_start = task_visiting.index(task_id)
            cycle = task_visiting[cycle_start:] + [task_id]
            issues.append("task dependency cycle detected: " + " -> ".join(cycle))
            return

        task_visiting.append(task_id)
        for dependency_id in task_dependency_map.get(task_id, []):
            visit_task_dependency(dependency_id)
        task_visiting.pop()
        task_visited.add(task_id)

    for task_id in sorted(task_dependency_map):
        visit_task_dependency(task_id)

    for log_item in model["logs"]:
        if not log_item.get("id"):
            issues.append("every log entry must have an id")
        if not log_item.get("path"):
            issues.append(f"log {log_item.get('id', '(missing id)')} is missing path")
        if log_item.get("client") and log_item["client"] not in declared_client_ids:
            issues.append(f"log {log_item.get('id')} references unknown client {log_item['client']!r}")

    for check in model["checks"]:
        check_type = check.get("type")
        if check_type not in VALID_CHECK_TYPES:
            issues.append(f"check {check.get('id')} has unsupported type {check_type!r}")
        if check_type == "path_exists" and not check.get("path"):
            issues.append(f"check {check.get('id')} is missing path")
        if check.get("client") and check["client"] not in declared_client_ids:
            issues.append(f"check {check.get('id')} references unknown client {check['client']!r}")

    if issues:
        return [
            CheckResult(
                status="fail",
                code="runtime-manifest",
                message="runtime manifest contains invalid definitions",
                details={"issues": issues},
            )
        ]

    return [
        CheckResult(
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
        )
    ]


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

    bridge_ids = {
        str(bridge.get("id", "")).strip()
        for bridge in model.get("bridges") or []
        if str(bridge.get("id", "")).strip()
    }
    task_ids = {
        str(task.get("id", "")).strip()
        for task in model.get("tasks") or []
        if str(task.get("id", "")).strip()
    }
    service_ids = {
        str(service.get("id", "")).strip()
        for service in model.get("services") or []
        if str(service.get("id", "")).strip()
    }

    issues: list[str] = []
    covered_services: list[str] = []
    deferred_surfaces: list[str] = []

    for item in ledger:
        item_id = str(item.get("id", "")).strip() or "(missing id)"
        action = str(item.get("action", "")).strip()
        ownership_state = str(item.get("ownership_state", "")).strip()
        bridge_dependency = item.get("bridge_dependency")
        request_error_raw = item.get("request_error")
        request_error = str(request_error_raw).strip() if request_error_raw is not None else ""
        surface = str(item.get("legacy_surface", "")).strip() or item_id
        # Only rows that represent an actual managed_service participate in the
        # cross-reference checks against the service graph. Non-service rows
        # (flag, helper, env_target, bridge, ...) describe legacy surfaces that
        # are intentionally not modelled as services — e.g. the
        # ``legacy-mode-selector`` flag row records that the runtime ``--mode``
        # argument has replaced the legacy ``db=`` selector. Missing
        # ``surface_type`` defaults to ``"service"`` so pre-existing overlays
        # that never declared the field keep their stricter check.
        surface_type = str(item.get("surface_type", "service")).strip() or "service"
        is_service_row = surface_type == "service"

        if action and action not in PARITY_LEDGER_ACTIONS:
            issues.append(
                f"parity_ledger {item_id}: unsupported action {action!r}"
            )
        if ownership_state and ownership_state not in PARITY_OWNERSHIP_STATES:
            issues.append(
                f"parity_ledger {item_id}: unsupported ownership_state {ownership_state!r}"
            )
        if request_error and request_error not in LOCAL_RUNTIME_ERROR_CODES:
            issues.append(
                f"parity_ledger {item_id}: request_error {request_error!r} is not one of "
                f"the stable error codes"
            )

        if bridge_dependency is not None and str(bridge_dependency).strip():
            dep_id = str(bridge_dependency).strip()
            if dep_id in task_ids and dep_id not in bridge_ids:
                issues.append(
                    f"parity_ledger {item_id}: bridge_dependency {dep_id!r} refers to a "
                    f"bootstrap_task id; only legacy_env_bridge ids are allowed"
                )
            elif dep_id not in bridge_ids:
                issues.append(
                    f"parity_ledger {item_id}: bridge_dependency {dep_id!r} is not a "
                    f"declared legacy_env_bridge"
                )

        if ownership_state == "covered":
            if is_service_row:
                covered_services.append(surface)
                if item_id and service_ids and item_id not in service_ids and surface not in service_ids:
                    issues.append(
                        f"parity_ledger {item_id}: ownership_state is 'covered' but no "
                        f"managed_service with that id is declared"
                    )
        elif ownership_state in ("deferred", "bridge-only"):
            if is_service_row:
                deferred_surfaces.append(surface)
                if item_id and item_id in service_ids:
                    issues.append(
                        f"parity_ledger {item_id}: ownership_state is {ownership_state!r} "
                        f"but a managed_service with that id is declared"
                    )

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
