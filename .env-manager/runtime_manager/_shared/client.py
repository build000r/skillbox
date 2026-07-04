from __future__ import annotations

# Generated mechanically from runtime_manager/shared.py; keep logic changes out of this split.
# ruff: noqa: F401
import argparse as argparse
import copy
import datetime
import fcntl
import hashlib
import json
import os
import re
import selectors as selectors
import shlex
import signal as signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

PACKAGE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PACKAGE_DIR.parent
DEFAULT_ROOT_DIR = SCRIPT_DIR.parent.resolve()
SCRIPTS_DIR = DEFAULT_ROOT_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from runtime_manager.errors import DEPRECATION_MARKER  # noqa: E402
except ImportError:  # loaded standalone without a package
    if str(PACKAGE_DIR) not in sys.path:
        sys.path.insert(0, str(PACKAGE_DIR))
    from errors import DEPRECATION_MARKER  # type: ignore[no-redef]  # noqa: E402

from lib.runtime_model import (  # noqa: E402
    LOOPBACK_BIND_HOSTS as LOOPBACK_BIND_HOSTS,
    PERSISTENCE_ERROR_CODES,
    PersistenceContractError,
    RUNTIME_ID_INVALID,
    RUNTIME_ID_PATTERN,
    RUNTIME_ID_PATTERN_TEXT,
    RuntimeIdValidationError as RuntimeIdValidationError,
    WILDCARD_BIND_HOSTS as WILDCARD_BIND_HOSTS,
    build_runtime_model,
    classify_bind_scope as classify_bind_scope,
    client_config_host_dir,
    client_config_runtime_dir,
    client_configs_host_root,
    compile_persistence_summary,
    extract_command_port as extract_command_port,
    extract_host_port as extract_host_port,
    host_path_to_absolute_path,
    load_yaml,
    load_runtime_env,
    runtime_manifest_path,
    runtime_path_to_host_path as runtime_path_to_host_path,
    storage_binding_by_id,
    validate_runtime_id as validate_runtime_id,
)
from lib.redaction import REDACTION_MARKER as REDACTION_MARKER  # noqa: E402
from lib.redaction import SECRET_KEY_PATTERN as SECRET_KEY_PATTERN  # noqa: E402
from lib.redaction import is_secret_key as is_secret_key  # noqa: E402
from lib.redaction import redact_text as redact_text  # noqa: E402
from lib.redaction import redact_value as redact_value  # noqa: E402
from .digest import (
    digest_bytes,
    file_sha256,
    normalize_sha256,
    tree_hash,
)

from .textutil import (
    directory_file_entries,
    normalize_bundle_rel_path,
    titleize_client_id,
    validate_client_id,
)

from .proc import (
    run_command,
)

from .next_actions import (
    next_actions_for_client_project,
    next_actions_for_private_init,
)

from .fs import (
    atomic_write_bytes,
    atomic_write_text,
    copy_tree_atomic,
    ensure_directory,
    load_json_file,
    normalize_host_rel_path,
    remove_path,
    repo_rel,
    write_json_file,
    write_text_file,
)

from .envio import (
    render_yaml_document,
    upsert_env_file_values,
)

from .client_scaffold import (
    HARDENED_CLIENT_PLAN_PATHS,
    HARDENED_CLIENT_SKILL_BUILDER_CONTEXT,
    base_client_overlay,
    client_scaffold_keep_files,
    client_scaffold_pack,
    ensure_client_scaffold_skill_sources,
    render_client_scaffold_skill_repos,
    sync_client_scaffold_seed_files,
)

CLIENT_PROJECTS_REL = Path("builds") / "clients"

CLIENT_OPEN_ROOT_REL = Path("sand")

CLIENT_PROJECTION_VERSION = 1

CLIENT_PROJECT_RUNTIME_MODEL_REL = Path("runtime-model.json")

CLIENT_PROJECTION_METADATA_REL = Path("projection.json")

CLIENT_PUBLISH_VERSION = 1

CLIENT_ACCEPTANCE_VERSION = 1

CLIENT_DEPLOY_VERSION = 1

CLIENT_PUBLISH_ROOT_REL = Path("clients")

CLIENT_PUBLISH_CURRENT_REL = Path("current")

CLIENT_PUBLISH_METADATA_REL = Path("publish.json")

CLIENT_ACCEPTANCE_METADATA_REL = Path("acceptance.json")

CLIENT_DEPLOY_METADATA_REL = Path("deploy.json")

CLIENT_DEPLOY_ARTIFACTS_REL = Path("artifacts")

DEFAULT_PRIVATE_REPO_REL = Path("..") / "skillbox-config"

CLIENT_OVERLAY_PROJECTION_ROOT_FILES = (
    "skill-repos.lock.json",
)

CLIENT_OVERLAY_PROJECTION_DIRS = (
    "skills",
    "plans",
    "workflows",
    "evaluations",
    "invocations",
    "observability",
)

def resolve_client_projection_output_dir(
    root_dir: Path,
    client_id: str,
    raw_output_dir: str | None,
) -> Path:
    if raw_output_dir:
        output_dir = Path(raw_output_dir).expanduser()
        if not output_dir.is_absolute():
            output_dir = (root_dir / output_dir).resolve()
        else:
            output_dir = output_dir.resolve()
        return output_dir
    return (root_dir / CLIENT_PROJECTS_REL / client_id).resolve()

def resolve_client_open_output_dir(
    root_dir: Path,
    client_id: str,
    raw_output_dir: str | None,
) -> Path:
    return resolve_optional_host_dir(
        root_dir,
        raw_output_dir,
        default_rel=CLIENT_OPEN_ROOT_REL / client_id,
    )

def runtime_path_to_projection_rel_path(env_values: dict[str, str], raw_path: str) -> Path:
    workspace_root = Path(env_values["SKILLBOX_WORKSPACE_ROOT"])
    runtime_path = Path(raw_path)
    try:
        relative = runtime_path.relative_to(workspace_root)
    except ValueError as exc:
        raise RuntimeError(
            "client-project only supports runtime files that live under "
            f"{workspace_root}, got {runtime_path}"
        ) from exc
    return Path(relative.as_posix())

def prepare_client_projection_output_dir(
    root_dir: Path,
    output_dir: Path,
    *,
    dry_run: bool,
    force: bool,
) -> list[str]:
    actions: list[str] = []
    default_root = (root_dir / CLIENT_PROJECTS_REL).resolve()
    protected_paths = {
        root_dir.resolve(),
        (root_dir / "workspace").resolve(),
        (root_dir / "default-skills").resolve(),
        (root_dir / ".env-manager").resolve(),
    }

    if output_dir in protected_paths:
        raise RuntimeError(f"Refusing to use protected output directory for client-project: {output_dir}")

    if output_dir.exists():
        if output_dir.is_dir():
            has_contents = any(output_dir.iterdir())
        else:
            has_contents = True

        if has_contents and not force:
            raise RuntimeError(
                f"client-project output already exists at {output_dir}. Re-run with --force to replace it."
            )

        if has_contents and force:
            allow_replace = (output_dir / CLIENT_PROJECTION_METADATA_REL).is_file()
            try:
                output_dir.relative_to(default_root)
                allow_replace = True
            except ValueError:
                pass
            if not allow_replace:
                raise RuntimeError(
                    "client-project output already exists at "
                    f"{output_dir} and is not a projection directory under the default build root."
                )
            actions.append(f"remove-output-dir: {repo_rel(root_dir, output_dir)}")
            if not dry_run:
                remove_path(output_dir)

    if not dry_run:
        ensure_directory(output_dir, dry_run=False)
    return actions

def add_projection_source_file(
    files: dict[str, dict[str, Any]],
    destination_rel: Path,
    source_path: Path,
) -> None:
    normalized_dest = destination_rel.as_posix()
    if normalized_dest in files:
        existing = files[normalized_dest]
        if existing.get("type") == "copy" and Path(str(existing["source_path"])) == source_path:
            return
        raise RuntimeError(f"client-project attempted to write duplicate output file {normalized_dest}")
    if not source_path.is_file():
        raise RuntimeError(f"Required projection source file missing: {source_path}")
    files[normalized_dest] = {
        "type": "copy",
        "destination_rel": normalized_dest,
        "source_path": source_path,
    }

def add_projection_source_tree(
    files: dict[str, dict[str, Any]],
    destination_root_rel: Path,
    source_root: Path,
) -> None:
    if not source_root.exists():
        return
    if source_root.is_file():
        add_projection_source_file(files, destination_root_rel, source_root)
        return
    for source_path in sorted(child for child in source_root.rglob("*") if child.is_file()):
        add_projection_source_file(
            files,
            destination_root_rel / source_path.relative_to(source_root),
            source_path,
        )

def add_projection_text_file(
    files: dict[str, dict[str, Any]],
    destination_rel: Path,
    content: str,
) -> None:
    normalized_dest = destination_rel.as_posix()
    if normalized_dest in files:
        existing = files[normalized_dest]
        if existing.get("type") == "text" and existing.get("content") == content:
            return
        raise RuntimeError(f"client-project attempted to write duplicate output file {normalized_dest}")
    files[normalized_dest] = {
        "type": "text",
        "destination_rel": normalized_dest,
        "content": content,
    }

def sanitize_projection_env(env_values: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in env_values.items():
        key_upper = str(key).upper()
        if key_upper in {"SKILLBOX_CLIENTS_HOST_ROOT", "SKILLBOX_MONOSERVER_HOST_ROOT"}:
            continue
        if any(marker in key_upper for marker in ("TOKEN", "SECRET", "PASSWORD")):
            continue
        sanitized[str(key)] = value
    return sanitized

def sanitize_projection_source(source: dict[str, Any]) -> dict[str, Any]:
    kind = str(source.get("kind") or "").strip()
    sanitized: dict[str, Any] = {}
    for key, value in source.items():
        key_text = str(key)
        key_upper = key_text.upper()
        if key_text == "host_path":
            continue
        if key_text == "path" and kind in {"bind", "directory", "file", "local", "manual"}:
            continue
        if any(marker in key_upper for marker in ("TOKEN", "SECRET", "PASSWORD")):
            continue
        sanitized[key_text] = sanitize_projection_value(value, key=key_text)
    return sanitized

def sanitize_projection_value(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        if key == "env":
            return sanitize_projection_env(value)
        if key == "source":
            return sanitize_projection_source(value)

        sanitized: dict[str, Any] = {}
        for child_key, child_value in value.items():
            child_key_text = str(child_key)
            child_key_upper = child_key_text.upper()
            if child_key_text.startswith("_"):
                continue
            if child_key_text in {"root_dir", "manifest_file", "host_path"} or child_key_text.endswith("_host_path"):
                continue
            if any(marker in child_key_upper for marker in ("TOKEN", "SECRET", "PASSWORD")):
                continue
            sanitized[child_key_text] = sanitize_projection_value(child_value, key=child_key_text)
        return sanitized

    if isinstance(value, list):
        return [sanitize_projection_value(item, key=key) for item in value]

    return value

def build_projected_runtime_manifest(
    root_dir: Path,
    client_id: str,
    *,
    overlay_present: bool,
) -> dict[str, Any]:
    runtime_doc = copy.deepcopy(load_yaml(runtime_manifest_path(root_dir)))
    selection = runtime_doc.get("selection")
    if selection is None:
        selection = {}
    if not isinstance(selection, dict):
        raise RuntimeError("Expected runtime manifest selection to be a mapping")

    selection_copy = copy.deepcopy(selection)
    selection_copy["default_client"] = client_id
    runtime_doc["selection"] = selection_copy

    raw_clients = runtime_doc.get("clients")
    if raw_clients is not None:
        if not isinstance(raw_clients, list):
            raise RuntimeError("Expected runtime manifest clients to be a list")
        if overlay_present:
            runtime_doc.pop("clients", None)
        else:
            filtered_clients = [
                copy.deepcopy(item)
                for item in raw_clients
                if isinstance(item, dict) and str(item.get("id", "")).strip() == client_id
            ]
            if filtered_clients:
                runtime_doc["clients"] = filtered_clients
            else:
                runtime_doc.pop("clients", None)

    return runtime_doc

def _add_projection_runtime_manifest(
    files: dict[str, dict[str, Any]],
    root_dir: Path,
    client_id: str,
    *,
    overlay_present: bool,
) -> None:
    runtime_doc = build_projected_runtime_manifest(root_dir, client_id, overlay_present=overlay_present)
    add_projection_text_file(files, Path("workspace") / "runtime.yaml", render_yaml_document(runtime_doc))

def _add_projection_optional_root_files(
    files: dict[str, dict[str, Any]],
    root_dir: Path,
) -> None:
    for optional_rel_path in (
        Path(".env.example"),
        Path("workspace") / "sandbox.yaml",
        Path("workspace") / "dependencies.yaml",
        Path("workspace") / "persistence.yaml",
    ):
        source_path = root_dir / optional_rel_path
        if source_path.is_file():
            add_projection_source_file(files, optional_rel_path, source_path)

def _add_overlay_projection_files(
    files: dict[str, dict[str, Any]],
    env_values: dict[str, str],
    client_id: str,
    client_overlay_host_path: Path,
) -> None:
    client_overlay_host_dir = client_overlay_host_path.parent
    overlay_runtime_dir = client_config_runtime_dir(env_values, client_id)
    overlay_projection_dir = runtime_path_to_projection_rel_path(env_values, str(overlay_runtime_dir))
    add_projection_source_file(
        files,
        runtime_path_to_projection_rel_path(env_values, str(overlay_runtime_dir / "overlay.yaml")),
        client_overlay_host_path,
    )
    for file_name in CLIENT_OVERLAY_PROJECTION_ROOT_FILES:
        source_path = client_overlay_host_dir / file_name
        if source_path.is_file():
            add_projection_source_file(files, overlay_projection_dir / file_name, source_path)
    for dir_name in CLIENT_OVERLAY_PROJECTION_DIRS:
        source_dir = client_overlay_host_dir / dir_name
        if source_dir.exists():
            add_projection_source_tree(files, overlay_projection_dir / dir_name, source_dir)

def _add_skill_repo_set_projection_files(
    files: dict[str, dict[str, Any]],
    env_values: dict[str, str],
    skillset: dict[str, Any],
) -> None:
    config_host_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
    if config_host_path.is_file():
        add_projection_source_file(
            files,
            runtime_path_to_projection_rel_path(env_values, str(skillset["skill_repos_config"])),
            config_host_path,
        )
    lock_host_path = Path(str(skillset.get("lock_path_host_path", "")))
    if lock_host_path.is_file():
        add_projection_source_file(
            files,
            runtime_path_to_projection_rel_path(env_values, str(skillset["lock_path"])),
            lock_host_path,
        )

def _add_packaged_skillset_projection_files(
    files: dict[str, dict[str, Any]],
    env_values: dict[str, str],
    skillset: dict[str, Any],
) -> None:
    from ..validation import collect_skill_inventory

    inventory = collect_skill_inventory(skillset)
    add_projection_source_file(
        files,
        runtime_path_to_projection_rel_path(env_values, str(skillset["manifest"])),
        Path(str(skillset["manifest_host_path"])),
    )
    add_projection_source_file(
        files,
        runtime_path_to_projection_rel_path(env_values, str(skillset["sources_config"])),
        Path(str(skillset["sources_config_host_path"])),
    )

    bundle_dir_runtime_path = PurePosixPath(str(skillset["bundle_dir"]))
    bundle_dir_host_path = Path(str(skillset["bundle_dir_host_path"]))
    bundle_readme_path = bundle_dir_host_path / "README.md"
    if bundle_readme_path.is_file():
        add_projection_source_file(
            files,
            runtime_path_to_projection_rel_path(env_values, str(bundle_dir_runtime_path / "README.md")),
            bundle_readme_path,
        )

    missing_bundles = [
        skill_name
        for skill_name in inventory["expected_skills"]
        if skill_name not in inventory["bundles"]
    ]
    if missing_bundles:
        raise RuntimeError(
            f"Skill set {skillset['id']} is missing bundles for: {', '.join(sorted(missing_bundles))}"
        )
    for skill_name in inventory["expected_skills"]:
        bundle_record = inventory["bundles"][skill_name]
        bundle_filename = str(bundle_record["filename"])
        add_projection_source_file(
            files,
            runtime_path_to_projection_rel_path(env_values, str(bundle_dir_runtime_path / bundle_filename)),
            Path(str(bundle_record["host_path"])),
        )

def _add_projection_skillset_files(
    files: dict[str, dict[str, Any]],
    env_values: dict[str, str],
    model: dict[str, Any],
) -> None:
    for skillset in model.get("skills") or []:
        if skillset.get("kind") == "skill-repo-set":
            _add_skill_repo_set_projection_files(files, env_values, skillset)
        else:
            _add_packaged_skillset_projection_files(files, env_values, skillset)

def _projection_runtime_model_payload(root_dir: Path, model: dict[str, Any]) -> dict[str, Any]:
    sanitized_model = sanitize_projection_value(copy.deepcopy(model))
    if isinstance(sanitized_model.get("storage"), dict):
        storage_summary = sanitized_model["storage"]
        raw_state_root = str(storage_summary.get("raw_state_root") or "").strip()
        if raw_state_root:
            storage_summary["state_root"] = raw_state_root
        else:
            storage_summary.pop("state_root", None)
    persistence_manifest = root_dir / "workspace" / "persistence.yaml"
    if persistence_manifest.is_file():
        sanitized_model["persistence_manifest_file"] = "/workspace/persistence.yaml"
    else:
        sanitized_model.pop("persistence_manifest_file", None)
    return sanitized_model

def collect_client_projection_files(
    root_dir: Path,
    model: dict[str, Any],
    client_id: str,
) -> tuple[dict[str, dict[str, Any]], str]:
    env_values = load_runtime_env(root_dir)
    files: dict[str, dict[str, Any]] = {}
    client_overlay_host_path = client_config_host_dir(root_dir, env_values, client_id) / "overlay.yaml"
    overlay_present = client_overlay_host_path.is_file()

    _add_projection_runtime_manifest(files, root_dir, client_id, overlay_present=overlay_present)
    _add_projection_optional_root_files(files, root_dir)
    if overlay_present:
        _add_overlay_projection_files(files, env_values, client_id, client_overlay_host_path)
    _add_projection_skillset_files(files, env_values, model)
    add_projection_text_file(
        files,
        CLIENT_PROJECT_RUNTIME_MODEL_REL,
        json.dumps(_projection_runtime_model_payload(root_dir, model), indent=2, sort_keys=True) + "\n",
    )

    overlay_mode = "overlay" if overlay_present else "inline"
    return files, overlay_mode

def materialize_client_projection(
    root_dir: Path,
    output_dir: Path,
    files: dict[str, dict[str, Any]],
    *,
    dry_run: bool,
    force: bool,
) -> tuple[list[str], list[tuple[str, str]]]:
    actions = prepare_client_projection_output_dir(
        root_dir,
        output_dir,
        dry_run=dry_run,
        force=force,
    )
    entries: list[tuple[str, str]] = []

    for destination_rel, spec in sorted(files.items()):
        destination_path = output_dir / destination_rel
        ensure_directory(destination_path.parent, dry_run)
        if spec["type"] == "copy":
            source_path = Path(str(spec["source_path"]))
            digest = file_sha256(source_path)
            actions.append(
                f"copy-file: {repo_rel(root_dir, source_path)} -> {repo_rel(root_dir, destination_path)}"
            )
            if not dry_run:
                shutil.copy2(source_path, destination_path)
        else:
            content = str(spec["content"])
            digest = digest_bytes(content.encode("utf-8"))
            actions.append(f"write-file: {repo_rel(root_dir, destination_path)}")
            if not dry_run:
                write_text_file(destination_path, content, dry_run=False)
        entries.append((destination_rel, digest))

    return actions, entries

def project_client_bundle(
    root_dir: Path,
    client_id: str,
    *,
    profiles: list[str] | None = None,
    output_dir_arg: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    from ..validation import filter_model, normalize_active_clients, normalize_active_profiles

    cid = validate_client_id(client_id)
    model = build_runtime_model(root_dir)
    active_profiles = normalize_active_profiles(profiles or [])
    active_clients = normalize_active_clients(model, [cid])
    filtered_model = filter_model(model, active_profiles, active_clients)
    output_dir = resolve_client_projection_output_dir(root_dir, cid, output_dir_arg)
    files, overlay_mode = collect_client_projection_files(root_dir, filtered_model, cid)
    actions, payload_entries = materialize_client_projection(
        root_dir,
        output_dir,
        files,
        dry_run=dry_run,
        force=force,
    )
    payload_tree_sha256 = tree_hash(payload_entries)
    projection_payload: dict[str, Any] = {
        "version": CLIENT_PROJECTION_VERSION,
        "client_id": cid,
        "active_profiles": filtered_model.get("active_profiles", []),
        "active_clients": filtered_model.get("active_clients", []),
        "default_client": str((filtered_model.get("selection") or {}).get("default_client") or cid),
        "overlay_mode": overlay_mode,
        "runtime_manifest": "workspace/runtime.yaml",
        "runtime_model": CLIENT_PROJECT_RUNTIME_MODEL_REL.as_posix(),
        "payload_tree_sha256": payload_tree_sha256,
        "files": [
            {"path": rel_path, "sha256": digest}
            for rel_path, digest in sorted(payload_entries)
        ],
    }

    metadata_path = output_dir / CLIENT_PROJECTION_METADATA_REL
    actions.append(f"write-file: {repo_rel(root_dir, metadata_path)}")
    if not dry_run:
        write_json_file(metadata_path, projection_payload)

    return {
        "client_id": cid,
        "output_dir": str(output_dir),
        "dry_run": dry_run,
        "force": force,
        "overlay_mode": overlay_mode,
        "active_profiles": filtered_model.get("active_profiles", []),
        "active_clients": filtered_model.get("active_clients", []),
        "file_count": len(payload_entries),
        "payload_tree_sha256": payload_tree_sha256,
        "files": projection_payload["files"],
        "actions": actions,
        "next_actions": next_actions_for_client_project(cid),
    }

def resolve_optional_host_dir(root_dir: Path, raw_path: str | None, *, default_rel: Path) -> Path:
    value = str(raw_path or "").strip()
    resolved = Path(value) if value else default_rel
    resolved = resolved.expanduser()
    if not resolved.is_absolute():
        return (root_dir / resolved).resolve()
    return resolved.resolve()

def inferred_private_target_dir(root_dir: Path, env_values: dict[str, str] | None = None) -> Path | None:
    resolved_env = env_values or load_runtime_env(root_dir)
    clients_root = client_configs_host_root(root_dir, resolved_env).resolve()
    default_clients_roots = {
        (root_dir / "workspace" / "clients").resolve(),
    }
    try:
        storage = compile_persistence_summary(root_dir, resolved_env)
    except RuntimeError:
        storage = None
    binding = storage_binding_by_id(storage, "clients-root")
    if binding is not None:
        relative_path = str(binding.get("relative_path") or "").strip()
        state_root = str(storage.get("state_root") or "").strip() if storage else ""
        if relative_path and state_root:
            default_clients_roots.add((Path(state_root) / Path(relative_path)).resolve())
    if clients_root in default_clients_roots:
        return None
    return clients_root.parent

def ensure_git_repo(path: Path) -> bool:
    from ..runtime_ops import git_repo_state

    ensure_directory(path, dry_run=False)
    state = git_repo_state(path)
    if state.get("git"):
        return False

    init_result = run_command(["git", "init"], cwd=path)
    if init_result.returncode != 0:
        raise RuntimeError(init_result.stderr.strip() or init_result.stdout.strip() or f"git init failed for {path}")

    branch_result = run_command(["git", "branch", "-M", "main"], cwd=path)
    if branch_result.returncode != 0:
        raise RuntimeError(
            branch_result.stderr.strip() or branch_result.stdout.strip() or f"git branch setup failed for {path}"
        )
    return True

def migrate_client_overlay_tree(root_dir: Path, source_root: Path, target_root: Path) -> tuple[list[str], list[str]]:
    actions: list[str] = []
    migrated_clients: list[str] = []
    ensure_directory(target_root, dry_run=False)
    if not source_root.is_dir() or source_root.resolve() == target_root.resolve():
        return actions, migrated_clients

    for child in sorted(source_root.iterdir()):
        if not child.is_dir():
            continue
        dest = target_root / child.name
        if dest.exists():
            actions.append(f"skip-client-existing: {repo_rel(root_dir, dest)}")
            continue
        copy_tree_atomic(child, dest)
        migrated_clients.append(child.name)
        actions.append(f"copy-client: {repo_rel(root_dir, dest)}")
    return actions, migrated_clients

def migrate_client_subtree(
    root_dir: Path,
    source_root: Path,
    target_clients_root: Path,
    *,
    subdir_name: str,
) -> list[str]:
    actions: list[str] = []
    ensure_directory(target_clients_root, dry_run=False)
    if not source_root.is_dir():
        return actions

    for child in sorted(source_root.iterdir()):
        if not child.is_dir():
            continue
        dest = target_clients_root / child.name / subdir_name
        ensure_directory(dest, dry_run=False)
        copied_any = False
        for entry in sorted(child.iterdir()):
            entry_dest = dest / entry.name
            if entry_dest.exists():
                actions.append(f"skip-client-{subdir_name}-entry-existing: {repo_rel(root_dir, entry_dest)}")
                continue
            if entry.is_dir():
                copy_tree_atomic(entry, entry_dest)
            else:
                atomic_write_bytes(entry_dest, entry.read_bytes())
            copied_any = True
            actions.append(f"copy-client-{subdir_name}-entry: {repo_rel(root_dir, entry_dest)}")
        if not copied_any and not any(dest.iterdir()):
            actions.append(f"ensure-client-{subdir_name}: {repo_rel(root_dir, dest)}")
    return actions

def ensure_client_overlay_skillset_shape(client_doc: dict[str, Any], client_id: str) -> None:
    skillset_template = copy.deepcopy(base_client_overlay(
        client_id=client_id,
        client_label=titleize_client_id(client_id),
        client_root=f"${{SKILLBOX_MONOSERVER_ROOT}}/{client_id}",
        client_default_cwd=f"${{SKILLBOX_MONOSERVER_ROOT}}/{client_id}",
    )["skills"][0])

    raw_skills = client_doc.setdefault("skills", [])
    if not isinstance(raw_skills, list):
        raise RuntimeError("Expected client.skills to be a list.")

    target_skillset: dict[str, Any] | None = None
    for skillset in raw_skills:
        if not isinstance(skillset, dict):
            continue
        skillset_id = str(skillset.get("id") or "").strip()
        if skillset_id == f"{client_id}-skills" or str(skillset.get("kind") or "").strip() == "packaged-skill-set":
            target_skillset = skillset
            break

    if target_skillset is None:
        target_skillset = {}
        raw_skills.append(target_skillset)

    for key, value in skillset_template.items():
        if key in {"install_targets", "sync"}:
            target_skillset[key] = copy.deepcopy(value)
        elif key not in target_skillset:
            target_skillset[key] = copy.deepcopy(value)
        else:
            target_skillset[key] = copy.deepcopy(value)

def ensure_client_overlay_scaffold_shape(client_doc: dict[str, Any]) -> str:
    raw_scaffold = client_doc.get("scaffold") or {}
    if raw_scaffold is None:
        raw_scaffold = {}
    if not isinstance(raw_scaffold, dict):
        raise RuntimeError("Expected client.scaffold to be a mapping.")

    scaffold_pack = client_scaffold_pack(raw_scaffold.get("pack"))
    raw_scaffold["pack"] = scaffold_pack
    client_doc["scaffold"] = raw_scaffold
    return scaffold_pack

def ensure_client_overlay_context_shape(
    client_doc: dict[str, Any],
    client_default_cwd: str,
    scaffold_pack: str,
) -> None:
    raw_context = client_doc.setdefault("context", {})
    if not isinstance(raw_context, dict):
        raise RuntimeError("Expected client.context to be a mapping.")

    raw_cwd_match = raw_context.get("cwd_match")
    if not isinstance(raw_cwd_match, list):
        raw_cwd_match = []
    normalized_cwd_match = [
        str(value).strip()
        for value in raw_cwd_match
        if str(value).strip()
    ]
    if client_default_cwd not in normalized_cwd_match:
        normalized_cwd_match.append(client_default_cwd)
    raw_context["cwd_match"] = normalized_cwd_match or [client_default_cwd]

    scaffold_pack = client_scaffold_pack(scaffold_pack)
    if scaffold_pack in {"planning", "hybrid"}:
        raw_plans = raw_context.setdefault("plans", {})
        if not isinstance(raw_plans, dict):
            raise RuntimeError("Expected client.context.plans to be a mapping.")
        for key, value in HARDENED_CLIENT_PLAN_PATHS.items():
            raw_plans[key] = value
        if scaffold_pack == "planning":
            raw_context.pop("workflow_builder", None)
            return

    if scaffold_pack in {"skill-builder", "hybrid"}:
        raw_workflow_builder = raw_context.setdefault("workflow_builder", {})
        if not isinstance(raw_workflow_builder, dict):
            raise RuntimeError("Expected client.context.workflow_builder to be a mapping.")
        for key, value in HARDENED_CLIENT_SKILL_BUILDER_CONTEXT["workflow_builder"].items():
            raw_workflow_builder[key] = value
        if scaffold_pack == "skill-builder":
            raw_context.pop("plans", None)

def normalize_client_overlay_shape(root_dir: Path, overlay_dir: Path) -> list[str]:
    overlay_path = overlay_dir / "overlay.yaml"
    if not overlay_path.is_file():
        return []

    overlay_doc = load_yaml(overlay_path)
    if not isinstance(overlay_doc, dict):
        raise RuntimeError(f"Expected a mapping in {overlay_path}")
    client_doc = overlay_doc.setdefault("client", {})
    if not isinstance(client_doc, dict):
        raise RuntimeError(f"Expected a mapping at client in {overlay_path}")

    client_id = validate_client_id(str(client_doc.get("id") or overlay_dir.name))
    client_label = str(client_doc.get("label") or titleize_client_id(client_id)).strip() or titleize_client_id(client_id)
    client_default_cwd = str(
        client_doc.get("default_cwd")
        or "${SKILLBOX_MONOSERVER_ROOT}"
    ).strip()
    client_doc["id"] = client_id
    client_doc["label"] = client_label
    client_doc["default_cwd"] = client_default_cwd

    scaffold_pack = ensure_client_overlay_scaffold_shape(client_doc)
    ensure_client_overlay_skillset_shape(client_doc, client_id)
    ensure_client_overlay_context_shape(client_doc, client_default_cwd, scaffold_pack)

    actions: list[str] = []
    rendered_overlay = render_yaml_document(overlay_doc)
    existing_overlay = overlay_path.read_text(encoding="utf-8")
    if existing_overlay != rendered_overlay:
        atomic_write_text(overlay_path, rendered_overlay)
        actions.append(f"normalize-overlay: {repo_rel(root_dir, overlay_path)}")

    skill_repos_path = overlay_dir / "skill-repos.yaml"
    skill_repos_text = render_client_scaffold_skill_repos(client_label, scaffold_pack)
    if not skill_repos_path.is_file() or skill_repos_path.read_text(encoding="utf-8") != skill_repos_text:
        ensure_directory(skill_repos_path.parent, dry_run=False)
        atomic_write_text(skill_repos_path, skill_repos_text)
        actions.append(f"write-file: {repo_rel(root_dir, skill_repos_path)}")

    for keep_path, keep_content in client_scaffold_keep_files(overlay_dir, scaffold_pack).items():
        ensure_directory(keep_path.parent, dry_run=False)
        if keep_path.exists():
            continue
        atomic_write_text(keep_path, keep_content)
        actions.append(f"write-file: {repo_rel(root_dir, keep_path)}")

    actions.extend(
        ensure_client_scaffold_skill_sources(
            root_dir,
            overlay_dir,
            scaffold_pack,
            dry_run=False,
        )
    )

    actions.extend(
        sync_client_scaffold_seed_files(
            root_dir,
            overlay_dir,
            client_label,
            scaffold_pack,
            dry_run=False,
        )
    )

    return actions

def init_private_repo(root_dir: Path, *, target_dir_arg: str | None = None) -> dict[str, Any]:
    env_values = load_runtime_env(root_dir)
    current_clients_root = client_configs_host_root(root_dir, env_values).resolve()
    target_dir = resolve_optional_host_dir(root_dir, target_dir_arg, default_rel=DEFAULT_PRIVATE_REPO_REL)
    target_clients_root = (target_dir / "clients").resolve()

    actions: list[str] = []
    ensure_directory(target_dir, dry_run=False)
    actions.append(f"ensure-dir: {repo_rel(root_dir, target_dir)}")
    if ensure_git_repo(target_dir):
        actions.append(f"git-init: {repo_rel(root_dir, target_dir)}")
    else:
        actions.append(f"git-repo-present: {repo_rel(root_dir, target_dir)}")

    ensure_directory(target_clients_root, dry_run=False)
    actions.append(f"ensure-dir: {repo_rel(root_dir, target_clients_root)}")

    migrate_actions, migrated_clients = migrate_client_overlay_tree(
        root_dir,
        current_clients_root,
        target_clients_root,
    )
    actions.extend(migrate_actions)
    actions.extend(
        migrate_client_subtree(
            root_dir,
            root_dir / "default-skills" / "clients",
            target_clients_root,
            subdir_name="bundles",
        )
    )
    actions.extend(
        migrate_client_subtree(
            root_dir,
            root_dir / "skills" / "clients",
            target_clients_root,
            subdir_name="skills",
        )
    )
    shared_skills_root = current_clients_root / "_shared"
    target_shared_skills_root = target_clients_root / "_shared"
    if shared_skills_root.is_dir() and not target_shared_skills_root.exists():
        copy_tree_atomic(shared_skills_root, target_shared_skills_root)
        actions.append(f"copy-client-shared: {repo_rel(root_dir, target_shared_skills_root)}")
    for child in sorted(target_clients_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_"):
            continue
        actions.extend(normalize_client_overlay_shape(root_dir, child))

    clients_host_root_value = normalize_host_rel_path(root_dir, target_clients_root)
    env_changed = upsert_env_file_values(
        root_dir / ".env",
        {"SKILLBOX_CLIENTS_HOST_ROOT": clients_host_root_value},
    )
    actions.append(f"{'write' if env_changed else 'keep'}-env: .env")

    return {
        "target_dir": str(target_dir),
        "clients_host_root": str(target_clients_root),
        "env_updates": {"SKILLBOX_CLIENTS_HOST_ROOT": clients_host_root_value},
        "migrated_clients": migrated_clients,
        "actions": actions,
        "next_actions": next_actions_for_private_init(),
    }

def resolve_client_publish_target_dir(root_dir: Path, raw_target_dir: str | None) -> Path:
    target_value = str(raw_target_dir or "").strip()
    if target_value:
        return resolve_optional_host_dir(root_dir, target_value, default_rel=DEFAULT_PRIVATE_REPO_REL)

    inferred = inferred_private_target_dir(root_dir)
    if inferred is None:
        raise RuntimeError(
            "No private publish target configured. Run private-init to attach a private repo or pass --target-dir."
        )
    return inferred

def resolve_client_publish_bundle_dir(root_dir: Path, raw_bundle_dir: str) -> Path:
    bundle_dir = Path(raw_bundle_dir).expanduser()
    if not bundle_dir.is_absolute():
        bundle_dir = (root_dir / bundle_dir).resolve()
    else:
        bundle_dir = bundle_dir.resolve()
    return bundle_dir

def git_head_commit(path: Path) -> str | None:
    result = run_command(["git", "rev-parse", "HEAD"], cwd=path)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None

def git_dirty_paths(path: Path) -> list[str]:
    result = run_command(["git", "status", "--short"], cwd=path)
    if result.returncode != 0:
        return []

    paths: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1].strip()
        if entry:
            paths.append(entry)
    return paths

def load_client_projection_bundle(bundle_dir: Path, *, expected_client_id: str) -> dict[str, Any]:
    if not bundle_dir.is_dir():
        raise RuntimeError(f"Bundle directory not found: {bundle_dir}")

    projection_path = bundle_dir / CLIENT_PROJECTION_METADATA_REL
    if not projection_path.is_file():
        raise RuntimeError(f"Bundle directory is missing projection.json: {bundle_dir}")

    projection_payload = load_json_file(projection_path)
    bundle_client_id = str(projection_payload.get("client_id") or "").strip()
    if bundle_client_id != expected_client_id:
        raise RuntimeError(
            f"Bundle at {bundle_dir} is for client {bundle_client_id or '(unknown)'!r}, "
            f"not {expected_client_id!r}"
        )

    payload_tree_sha256 = normalize_sha256(
        projection_payload.get("payload_tree_sha256"),
        label=f"bundle {bundle_dir} payload_tree_sha256",
    )

    raw_files = projection_payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise RuntimeError(f"Bundle projection metadata is missing files[]: {projection_path}")

    payload_entries: list[tuple[str, str]] = []
    for index, raw_item in enumerate(raw_files):
        if not isinstance(raw_item, dict):
            raise RuntimeError(f"Bundle projection file entry {index} must be an object")

        rel_path = normalize_bundle_rel_path(
            raw_item.get("path"),
            label=f"bundle {bundle_dir} files[{index}].path",
        )
        expected_sha = normalize_sha256(
            raw_item.get("sha256"),
            label=f"bundle {bundle_dir} files[{index}].sha256",
        )
        file_path = bundle_dir / Path(*PurePosixPath(rel_path).parts)
        if not file_path.is_file():
            raise RuntimeError(f"Bundle payload file is missing: {rel_path}")

        actual_sha = file_sha256(file_path)
        if actual_sha != expected_sha:
            raise RuntimeError(f"Bundle payload file hash mismatch for {rel_path}")

        payload_entries.append((rel_path, actual_sha))

    if tree_hash(payload_entries) != payload_tree_sha256:
        raise RuntimeError(f"Bundle payload tree hash mismatch for {bundle_dir}")

    runtime_manifest_rel = normalize_bundle_rel_path(
        projection_payload.get("runtime_manifest", Path("workspace") / "runtime.yaml"),
        label=f"bundle {bundle_dir} runtime_manifest",
    )
    runtime_model_rel = normalize_bundle_rel_path(
        projection_payload.get("runtime_model", CLIENT_PROJECT_RUNTIME_MODEL_REL),
        label=f"bundle {bundle_dir} runtime_model",
    )

    for required_rel in (
        CLIENT_PROJECTION_METADATA_REL.as_posix(),
        runtime_manifest_rel,
        runtime_model_rel,
    ):
        required_path = bundle_dir / Path(*PurePosixPath(required_rel).parts)
        if not required_path.is_file():
            raise RuntimeError(f"Bundle file is missing: {required_rel}")

    all_entries = directory_file_entries(bundle_dir)
    if not all_entries:
        raise RuntimeError(f"Bundle directory is empty: {bundle_dir}")

    return {
        "bundle_dir": str(bundle_dir),
        "client_id": expected_client_id,
        "projection": projection_payload,
        "payload_entries": payload_entries,
        "payload_tree_sha256": payload_tree_sha256,
        "runtime_manifest_rel": runtime_manifest_rel,
        "runtime_model_rel": runtime_model_rel,
        "all_entries": all_entries,
    }

CLIENT_RUNTIME_DIFF_SECTIONS = (
    "clients",
    "repos",
    "artifacts",
    "env_files",
    "skills",
    "tasks",
    "services",
    "logs",
    "checks",
)

CLIENT_PUBLISH_METADATA_COMPARE_FIELDS = (
    "version",
    "client_id",
    "source_commit",
    "projection_version",
    "overlay_mode",
    "active_profiles",
    "active_clients",
    "default_client",
    "payload_tree_sha256",
    "file_count",
    "current_dir",
    "projection",
    "runtime_manifest",
    "runtime_model",
)

CLIENT_ACCEPTANCE_MATCH_FIELDS = (
    "version",
    "client_id",
    "source_commit",
    "payload_tree_sha256",
    "active_profiles",
    "ready",
    "doctor_post",
    "services",
    "mcp_servers",
)
