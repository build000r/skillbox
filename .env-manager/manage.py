#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT_DIR = SCRIPT_DIR.parent.resolve()
SCRIPTS_DIR = DEFAULT_ROOT_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.runtime_model import build_runtime_model  # noqa: E402


VALID_REPO_SOURCE_KINDS = {"bind", "directory", "git", "manual"}
VALID_SYNC_MODES = {"external", "ensure-directory", "clone-if-missing", "manual"}
VALID_SKILL_SYNC_MODES = {"unpack-bundles"}
VALID_HEALTHCHECK_TYPES = {"http", "path_exists"}
VALID_CHECK_TYPES = {"path_exists"}
LOCKFILE_VERSION = 1


@dataclass
class CheckResult:
    status: str
    code: str
    message: str
    details: dict[str, Any] | None = None


def repo_rel(root_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root_dir))
    except ValueError:
        return str(path)


def run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def human_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0 or unit == "GiB":
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{num_bytes}B"


def resolve_root_dir(raw_root: str | None) -> Path:
    if raw_root:
        return Path(raw_root).resolve()
    return DEFAULT_ROOT_DIR


def normalize_active_profiles(raw_profiles: list[str] | None) -> set[str]:
    active_profiles = {value.strip() for value in raw_profiles or [] if value and value.strip()}
    active_profiles.add("core")
    return active_profiles


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
    return not item_profiles.isdisjoint(active_profiles)


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
    filtered_model["repos"] = [
        copy.deepcopy(repo)
        for repo in model["repos"]
        if item_matches_profiles(repo, active_profiles) and item_matches_clients(repo, active_clients)
    ]
    filtered_model["skills"] = [
        copy.deepcopy(skillset)
        for skillset in model["skills"]
        if item_matches_profiles(skillset, active_profiles) and item_matches_clients(skillset, active_clients)
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

    included_repo_ids = {repo["id"] for repo in filtered_model["repos"]}
    included_log_ids = {log_item["id"] for log_item in filtered_model["logs"]}

    required_repo_ids = {
        str(service["repo"])
        for service in filtered_model["services"]
        if service.get("repo")
    }
    required_log_ids = {
        str(service["log"])
        for service in filtered_model["services"]
        if service.get("log")
    }

    for repo in model["repos"]:
        repo_id = str(repo.get("id", "")).strip()
        if repo_id and repo_id in required_repo_ids and repo_id not in included_repo_ids:
            filtered_model["repos"].append(copy.deepcopy(repo))
            included_repo_ids.add(repo_id)

    for log_item in model["logs"]:
        log_id = str(log_item.get("id", "")).strip()
        if log_id and log_id in required_log_ids and log_id not in included_log_ids:
            filtered_model["logs"].append(copy.deepcopy(log_item))
            included_log_ids.add(log_id)

    return filtered_model


def find_duplicates(items: list[dict[str, Any]], field: str) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        value = str(item.get(field, "")).strip()
        if not value:
            continue
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def ensure_directory(path: Path, dry_run: bool) -> None:
    if dry_run:
        return
    path.mkdir(parents=True, exist_ok=True)


def remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def tree_hash(entries: list[tuple[str, str]]) -> str:
    hasher = hashlib.sha256()
    for rel_path, digest in sorted(entries):
        hasher.update(rel_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def directory_tree_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_dir():
        return None

    entries: list[tuple[str, str]] = []
    for file_path in sorted(child for child in path.rglob("*") if child.is_file()):
        rel_path = file_path.relative_to(path).as_posix()
        entries.append((rel_path, file_sha256(file_path)))
    return tree_hash(entries)


def read_manifest_skills(path: Path) -> list[str]:
    seen: set[str] = set()
    skills: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line in seen:
            continue
        skills.append(line)
        seen.add(line)
    return skills


def bundle_members(bundle_path: Path, expected_skill_name: str | None = None) -> tuple[str, list[tuple[str, str]]]:
    members: list[tuple[str, str]] = []
    top_levels: set[str] = set()

    with zipfile.ZipFile(bundle_path, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue

            member_path = PurePosixPath(info.filename.replace("\\", "/"))
            if member_path.is_absolute() or ".." in member_path.parts or len(member_path.parts) < 2:
                raise RuntimeError(f"Invalid bundle member in {bundle_path}: {info.filename}")

            top_level = member_path.parts[0]
            top_levels.add(top_level)
            if expected_skill_name and top_level != expected_skill_name:
                raise RuntimeError(
                    f"Bundle {bundle_path.name} does not unpack to the expected skill root {expected_skill_name}"
                )

            rel_path = PurePosixPath(*member_path.parts[1:]).as_posix()
            members.append((rel_path, digest_bytes(archive.read(info))))

    if not members:
        raise RuntimeError(f"Bundle {bundle_path} is empty")
    if len(top_levels) != 1:
        raise RuntimeError(f"Bundle {bundle_path} must contain exactly one top-level skill directory")

    return next(iter(top_levels)), members


def bundle_metadata(bundle_path: Path, expected_skill_name: str | None = None) -> dict[str, Any]:
    archive_root, members = bundle_members(bundle_path, expected_skill_name=expected_skill_name)
    return {
        "name": bundle_path.stem,
        "filename": bundle_path.name,
        "host_path": str(bundle_path),
        "bundle_sha256": file_sha256(bundle_path),
        "bundle_tree_sha256": tree_hash(members),
        "archive_root": archive_root,
        "file_count": len(members),
    }


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return raw


def write_json_file(path: Path, payload: dict[str, Any]) -> bool:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ensure_directory(path.parent, dry_run=False)
    if path.exists() and path.read_text(encoding="utf-8") == serialized:
        return False
    path.write_text(serialized, encoding="utf-8")
    return True


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
    actions: list[str] = []

    for skillset in model["skills"]:
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

    for section in ("repos", "skills", "services", "logs", "checks"):
        duplicates = find_duplicates(model[section], "id")
        if duplicates:
            issues.append(f"{section} contain duplicate ids: {', '.join(duplicates)}")

    duplicate_repo_paths = find_duplicates(model["repos"], "path")
    if duplicate_repo_paths:
        issues.append(f"repos contain duplicate paths: {', '.join(duplicate_repo_paths)}")

    duplicate_log_paths = find_duplicates(model["logs"], "path")
    if duplicate_log_paths:
        issues.append(f"logs contain duplicate paths: {', '.join(duplicate_log_paths)}")

    repo_ids = {repo.get("id") for repo in model["repos"]}
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

    for skillset in model["skills"]:
        if not skillset.get("id"):
            issues.append("every skills entry must have an id")
        if skillset.get("client") and skillset["client"] not in declared_client_ids:
            issues.append(f"skill set {skillset.get('id')} references unknown client {skillset['client']!r}")
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

    for service in model["services"]:
        if not service.get("id"):
            issues.append("every service entry must have an id")
        if service.get("client") and service["client"] not in declared_client_ids:
            issues.append(f"service {service.get('id')} references unknown client {service['client']!r}")
        if service.get("repo") and service["repo"] not in repo_ids:
            issues.append(f"service {service.get('id')} references unknown repo {service['repo']!r}")
        if service.get("log") and service["log"] not in log_ids:
            issues.append(f"service {service.get('id')} references unknown log {service['log']!r}")

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
                "skills": len(model["skills"]),
                "services": len(model["services"]),
                "logs": len(model["logs"]),
                "checks": len(model["checks"]),
            },
        )
    ]


def check_filesystem(model: dict[str, Any], root_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    missing_syncable_repo_paths: list[str] = []
    missing_required_repo_paths: list[str] = []
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


def doctor_results(model: dict[str, Any], root_dir: Path) -> list[CheckResult]:
    results = check_manifest(model)
    if any(result.status == "fail" for result in results):
        return results
    return results + check_filesystem(model, root_dir) + validate_skill_locks_and_state(model)


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

    for log_item in model["logs"]:
        path = Path(str(log_item["host_path"]))
        if path.exists():
            actions.append(f"exists: {path}")
            continue
        ensure_directory(path, dry_run)
        actions.append(f"ensure-directory: {path}")

    actions.extend(sync_skill_sets(model, dry_run=dry_run))
    return actions


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


def probe_service(service: dict[str, Any]) -> dict[str, Any]:
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

    return {"state": "unknown"}


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

    service_statuses: list[dict[str, Any]] = []
    for service in model["services"]:
        item = {
            "id": service["id"],
            "kind": service.get("kind", "service"),
            "profiles": service.get("profiles") or [],
        }
        item.update(probe_service(service))
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
        "skills": skill_statuses,
        "services": service_statuses,
        "logs": log_statuses,
        "checks": check_statuses,
    }


def print_render_text(model: dict[str, Any]) -> None:
    available_clients = ", ".join(client["id"] for client in model.get("clients") or []) or "(none)"
    default_client = (model.get("selection") or {}).get("default_client") or "(none)"
    active_clients = model.get("active_clients") or []
    print(f"clients: {available_clients}")
    print(f"default client: {default_client}")
    if active_clients:
        print(f"active clients: {', '.join(active_clients)}")
    active_profiles = model.get("active_profiles") or []
    if active_profiles:
        print(f"active profiles: {', '.join(active_profiles)}")
    print(f"runtime manifest: {model['manifest_file']}")
    print(f"repos: {len(model['repos'])}")
    for repo in model["repos"]:
        print(f"  - {repo['id']}: {repo.get('kind', 'repo')} @ {repo['path']}")
    print(f"skills: {len(model['skills'])}")
    for skillset in model["skills"]:
        print(f"  - {skillset['id']}: {skillset.get('kind', 'packaged-skill-set')} @ {skillset['bundle_dir']}")
    print(f"services: {len(model['services'])}")
    for service in model["services"]:
        profiles = ", ".join(service.get("profiles") or []) or "core"
        print(f"  - {service['id']}: {service.get('kind', 'service')} [{profiles}]")
    print(f"logs: {len(model['logs'])}")
    for log_item in model["logs"]:
        print(f"  - {log_item['id']}: {log_item['path']}")
    print(f"checks: {len(model['checks'])}")
    for check in model["checks"]:
        print(f"  - {check['id']}: {check['type']}")


def detail_lines(details: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in details.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            lines.append(f"{key}: {', '.join(str(item) for item in value)}")
        else:
            lines.append(f"{key}: {value}")
    return lines


def print_doctor_text(results: list[CheckResult]) -> None:
    for result in results:
        print(f"{result.status.upper():4} {result.code}: {result.message}")
        if result.details:
            for line in detail_lines(result.details):
                print(f"     {line}")

    counts = {
        "pass": sum(1 for item in results if item.status == "pass"),
        "warn": sum(1 for item in results if item.status == "warn"),
        "fail": sum(1 for item in results if item.status == "fail"),
    }
    print()
    print(
        "summary: "
        f"{counts['pass']} passed, "
        f"{counts['warn']} warnings, "
        f"{counts['fail']} failed"
    )


def print_status_text(status_payload: dict[str, Any]) -> None:
    available_clients = ", ".join(client["id"] for client in status_payload.get("clients") or []) or "(none)"
    print(f"clients: {available_clients}")
    default_client = status_payload.get("default_client") or "(none)"
    print(f"default client: {default_client}")
    active_clients = status_payload.get("active_clients") or []
    if active_clients:
        print(f"active clients: {', '.join(active_clients)}")
    active_profiles = status_payload.get("active_profiles") or []
    if active_profiles:
        print(f"active profiles: {', '.join(active_profiles)}")
    print("repos:")
    for repo in status_payload["repos"]:
        summary = "present" if repo["present"] else "missing"
        if repo.get("git"):
            summary = (
                f"{summary}, git {repo.get('branch', '(detached)')}, "
                f"{repo.get('dirty', 0)} dirty, {repo.get('untracked', 0)} untracked"
            )
        print(f"  - {repo['id']}: {summary}")

    print("skills:")
    for skillset in status_payload["skills"]:
        total_targets = 0
        healthy_targets = 0
        for skill_entry in skillset["skills"]:
            for target in skill_entry["targets"]:
                total_targets += 1
                if target["state"] == "ok":
                    healthy_targets += 1

        lock_summary = "invalid" if skillset.get("lock_error") else ("present" if skillset["lock_present"] else "missing")
        print(
            f"  - {skillset['id']}: lock {lock_summary}, "
            f"{len(skillset['skills'])} skills, {healthy_targets}/{total_targets} targets healthy"
        )

    print("services:")
    for service in status_payload["services"]:
        print(f"  - {service['id']}: {service.get('state', 'declared')}")

    print("logs:")
    for log_item in status_payload["logs"]:
        if log_item["present"]:
            print(
                f"  - {log_item['id']}: {log_item['files']} files, "
                f"{human_bytes(int(log_item['bytes']))}"
            )
        else:
            print(f"  - {log_item['id']}: missing")

    print("checks:")
    for check in status_payload["checks"]:
        state = "ok" if check.get("ok") else "missing"
        print(f"  - {check['id']}: {state}")


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the internal skillbox runtime graph.")
    parser.add_argument(
        "--root-dir",
        default=None,
        help="Override the repo root for testing or embedding.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_profile_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--profile",
            action="append",
            default=[],
            help="Activate a runtime profile. Can be repeated. Selecting any profile also includes `core`.",
        )

    def add_client_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--client",
            action="append",
            default=[],
            help="Activate a runtime client overlay. Can be repeated.",
        )

    render_parser = subparsers.add_parser("render", help="Print the resolved runtime graph.")
    render_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(render_parser)
    add_client_arg(render_parser)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Create managed runtime directories, repos, and installed skill state.",
    )
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(sync_parser)
    add_client_arg(sync_parser)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate runtime graph, filesystem readiness, and installed skill integrity.",
    )
    doctor_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(doctor_parser)
    add_client_arg(doctor_parser)

    status_parser = subparsers.add_parser(
        "status",
        help="Summarize repo, skill, service, log, and check state.",
    )
    status_parser.add_argument("--format", choices=("text", "json"), default="text")
    add_profile_arg(status_parser)
    add_client_arg(status_parser)

    args = parser.parse_args()
    root_dir = resolve_root_dir(args.root_dir)
    model = build_runtime_model(root_dir)
    active_profiles = normalize_active_profiles(getattr(args, "profile", []))
    active_clients = normalize_active_clients(model, getattr(args, "client", []))
    model = filter_model(model, active_profiles, active_clients)

    if args.command == "render":
        if args.format == "json":
            emit_json(model)
        else:
            print_render_text(model)
        return 0

    if args.command == "sync":
        actions = sync_runtime(model, dry_run=args.dry_run)
        if args.format == "json":
            emit_json({"actions": actions, "dry_run": args.dry_run})
        else:
            print("\n".join(actions))
        return 0

    if args.command == "doctor":
        results = doctor_results(model, root_dir)
        if args.format == "json":
            emit_json([asdict(result) for result in results])
        else:
            print_doctor_text(results)
        return 1 if any(result.status == "fail" for result in results) else 0

    status_payload = runtime_status(model)
    if args.format == "json":
        emit_json(status_payload)
    else:
        print_status_text(status_payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
