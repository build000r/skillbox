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
from .textutil import (
    resolve_known_placeholders,
    split_csv_values,
    titleize_client_id,
    validate_client_id,
)

from .fs import (
    atomic_write_text,
    copy_tree_atomic,
    ensure_directory,
    repo_rel,
    write_text_file,
)

from .envio import (
    render_yaml_document,
    require_yaml,
)

CLIENT_PLANNING_SKILL_TEMPLATE_REL = Path("workspace") / "client-planning-skills"

CLIENT_SKILL_BUILDER_TEMPLATE_REL = Path("workspace") / "client-skill-builder-skills"

BLUEPRINT_VARIABLE_PATTERN = re.compile(r"^[A-Z0-9_]+$")

SCAFFOLD_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")

GLOBAL_HOME_ROOT_ENV = "SKILLBOX_HOME_ROOT"

GLOBAL_HOME_SURFACES = ("claude", "codex")

def managed_home_install_targets() -> list[dict[str, str]]:
    """Install targets for the managed home's global skill surfaces.

    Each target is a ``${SKILLBOX_HOME_ROOT}/.<surface>/skills`` placeholder,
    derived from the canonical env-var name and surface list rather than being
    hand-spelled, so model defaults and runtime resolution can never drift.
    """
    return [
        {"id": surface, "path": f"${{{GLOBAL_HOME_ROOT_ENV}}}/.{surface}/skills"}
        for surface in GLOBAL_HOME_SURFACES
    ]

HARDENED_SHARED_DEFAULT_SKILLS = [
    "divide-and-conquer",
    "lube",
    "mmdx",
    "project-status-mmdx",
    "skill-issue",
    "smart",
]

HARDENED_CLIENT_PLANNING_SKILLS = [
    "domain-planner",
    "domain-reviewer",
    "domain-scaffolder",
    "divide-and-conquer",
]

HARDENED_CLIENT_SKILL_BUILDER_SKILLS = [
    "skill-issue",
    "prompt-reviewer",
]

HARDENED_CLIENT_HYBRID_SKILLS = (
    HARDENED_CLIENT_PLANNING_SKILLS
    + HARDENED_CLIENT_SKILL_BUILDER_SKILLS
)

HARDENED_CLIENT_PLAN_PATHS = {
    "plan_root": "plans/released",
    "plan_draft": "plans/draft",
    "plan_index": "plans/INDEX.md",
    "session_plans": "plans/sessions",
}

HARDENED_CLIENT_SKILL_BUILDER_CONTEXT = {
    "workflow_builder": {
        "workflow_root": "workflows",
        "workflow_index": "workflows/INDEX.md",
        "evaluation_root": "evaluations",
        "evaluation_notes": "evaluations/README.md",
        "invocation_root": "invocations",
        "invocation_notes": "invocations/README.md",
        "observability_root": "observability",
        "observability_notes": "observability/README.md",
        "extraction_rule": "workflows/EXTRACTION.md",
    }
}

def client_blueprint_dir(root_dir: Path) -> Path:
    return root_dir / "workspace" / "client-blueprints"

def normalize_client_connector_entries(
    raw_connectors: Any,
    *,
    client_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if raw_connectors is None:
        return [], []
    if isinstance(raw_connectors, str):
        return [{"id": connector_id} for connector_id in split_csv_values(raw_connectors)], []
    if not isinstance(raw_connectors, list):
        return [], [f"client {client_id} connectors must be a comma-separated string or a list"]

    entries: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, raw_entry in enumerate(raw_connectors, start=1):
        if isinstance(raw_entry, str):
            connector_id = raw_entry.strip()
            if not connector_id:
                issues.append(f"client {client_id} connectors[{index}] is empty")
                continue
            entries.append({"id": connector_id})
            continue
        if not isinstance(raw_entry, dict):
            issues.append(
                f"client {client_id} connectors[{index}] must be a string or mapping, got {type(raw_entry).__name__}"
            )
            continue

        entry = copy.deepcopy(raw_entry)
        connector_id = str(entry.get("id", "")).strip()
        if not connector_id:
            issues.append(f"client {client_id} connectors[{index}] is missing id")
            continue
        entry["id"] = connector_id

        capabilities = entry.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, list):
                issues.append(f"client {client_id} connector {connector_id!r} capabilities must be a list")
            else:
                entry["capabilities"] = split_csv_values(capabilities)

        scopes = entry.get("scopes")
        if scopes is not None and not isinstance(scopes, dict):
            issues.append(f"client {client_id} connector {connector_id!r} scopes must be a mapping")

        entries.append(entry)

    return entries, issues

def scaffold_connector_entries(raw_connectors: Any, values: dict[str, str], *, client_id: str) -> list[dict[str, Any]]:
    entries, issues = normalize_client_connector_entries(raw_connectors, client_id=client_id)
    if issues:
        raise RuntimeError("Invalid client connector declaration in blueprint: " + "; ".join(issues))

    slack_capabilities = split_csv_values(values.get("SLACK_CAPABILITIES", ""))
    slack_channels = split_csv_values(values.get("SLACK_CHANNELS", ""))

    normalized_entries: list[dict[str, Any]] = []
    for entry in entries:
        normalized_entry = copy.deepcopy(entry)
        if normalized_entry["id"] == "slack":
            if slack_capabilities and "capabilities" not in normalized_entry:
                normalized_entry["capabilities"] = slack_capabilities
            if slack_channels:
                scopes = copy.deepcopy(normalized_entry.get("scopes") or {})
                scopes["channels"] = slack_channels
                normalized_entry["scopes"] = scopes
        normalized_entries.append(normalized_entry)
    return normalized_entries

def list_client_blueprints(root_dir: Path) -> list[dict[str, Any]]:
    blueprint_root = client_blueprint_dir(root_dir)
    if not blueprint_root.is_dir():
        return []

    blueprints: list[dict[str, Any]] = []
    for path in sorted(blueprint_root.glob("*.yaml")):
        blueprints.append(load_client_blueprint(path))
    return blueprints

def resolve_client_blueprint_path(root_dir: Path, raw_blueprint: str) -> Path:
    candidate = Path(raw_blueprint).expanduser()
    blueprint_root = client_blueprint_dir(root_dir)
    attempts: list[Path] = []

    if candidate.is_absolute():
        attempts.append(candidate)
    else:
        attempts.append((root_dir / candidate).resolve())
        attempts.append((blueprint_root / candidate).resolve())
        if not candidate.suffix:
            attempts.append((blueprint_root / f"{candidate}.yaml").resolve())

    for path in attempts:
        if path.is_file():
            return path

    available = ", ".join(item["id"] for item in list_client_blueprints(root_dir)) or "(none)"
    raise RuntimeError(
        f"Client blueprint {raw_blueprint!r} was not found. Available blueprints: {available}"
    )

def load_client_blueprint(path: Path) -> dict[str, Any]:
    yaml_mod = require_yaml("use client blueprints")

    try:
        raw = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Client blueprint not found: {path}") from exc
    except Exception as exc:  # pragma: no cover - defensive parse path
        raise RuntimeError(f"Failed to parse client blueprint {path}: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Expected a YAML object in client blueprint {path}")

    version = raw.get("version", 1)
    if version != 1:
        raise RuntimeError(f"Unsupported client blueprint version in {path}: {version!r}")

    raw_client = raw.get("client")
    if raw_client is None:
        client = {}
    elif isinstance(raw_client, dict):
        client = raw_client
    else:
        raise RuntimeError(f"Expected `client` to be a mapping in {path}")

    raw_variables = raw.get("variables") or []
    if not isinstance(raw_variables, list):
        raise RuntimeError(f"Expected `variables` to be a list in {path}")

    raw_scaffold = raw.get("scaffold") or {}
    if raw_scaffold is None:
        scaffold = {}
    elif isinstance(raw_scaffold, dict):
        scaffold = copy.deepcopy(raw_scaffold)
    else:
        raise RuntimeError(f"Expected `scaffold` to be a mapping in {path}")

    variables: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw_variable in raw_variables:
        if not isinstance(raw_variable, dict):
            raise RuntimeError(f"Expected every variable entry to be a mapping in {path}")
        name = str(raw_variable.get("name", "")).strip()
        if not name or not BLUEPRINT_VARIABLE_PATTERN.fullmatch(name):
            raise RuntimeError(
                f"Invalid client blueprint variable {name!r} in {path}. "
                "Use uppercase letters, numbers, and underscores."
            )
        if name in seen_names:
            raise RuntimeError(f"Duplicate client blueprint variable {name!r} in {path}")
        seen_names.add(name)
        variables.append(
            {
                "name": name,
                "required": bool(raw_variable.get("required")),
                "default": None if "default" not in raw_variable else str(raw_variable.get("default", "")),
                "description": str(raw_variable.get("description", "")).strip(),
            }
        )

    return {
        "id": path.stem,
        "path": str(path),
        "description": str(raw.get("description", "")).strip(),
        "variables": variables,
        "scaffold": scaffold,
        "client": client,
    }

def base_client_overlay(
    client_id: str,
    client_label: str,
    client_root: str,
    client_default_cwd: str,
    *,
    scaffold_pack: str = "planning",
) -> dict[str, Any]:
    overlay = {
        "id": client_id,
        "label": client_label,
        "default_cwd": client_default_cwd,
        "scaffold": {
            "pack": scaffold_pack,
        },
        "repo_roots": [
            {
                "id": f"{client_id}-root",
                "kind": "repo-root",
                "path": client_root,
                "required": True,
                "profiles": ["core"],
                "source": {"kind": "bind"},
                "sync": {"mode": "external"},
                "notes": "Client root mounted from the shared monoserver tree.",
            }
        ],
        "skills": [
            {
                "id": f"{client_id}-skills",
                "kind": "skill-repo-set",
                "required": False,
                "profiles": ["core"],
                "skill_repos_config": f"${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skill-repos.yaml",
                "lock_path": f"${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skill-repos.lock.json",
                "clone_root": "${SKILLBOX_WORKSPACE_ROOT}/workspace/skill-repos",
                "sync": {"mode": "clone-and-install"},
                "install_targets": managed_home_install_targets(),
                "notes": "Client-scoped skills layered on top of the shared defaults.",
            }
        ],
        "logs": [
            {
                "id": client_id,
                "path": f"${{SKILLBOX_LOG_ROOT}}/clients/{client_id}",
                "required": False,
                "profiles": ["core"],
                "retention_days": 14,
                "notes": f"Client-scoped logs for the {client_id} overlay.",
            }
        ],
        "context": {
            "cwd_match": [client_default_cwd],
        },
        "checks": [
            {
                "id": f"{client_id}-root",
                "type": "path_exists",
                "path": client_root,
                "required": True,
                "profiles": ["core"],
                "notes": f"The {client_id} overlay expects the client root to be mounted.",
            }
        ],
    }
    from .client import ensure_client_overlay_context_shape

    ensure_client_overlay_context_shape(overlay, client_default_cwd, scaffold_pack)
    return overlay

def render_client_plan_index(client_label: str) -> str:
    return (
        f"# {client_label} Plan Index\n"
        "\n"
        "| Slice | Tag | Status | Summary |\n"
        "|---|---|---|---|\n"
    )

def client_plan_seed_files(overlay_dir: Path, client_label: str) -> dict[Path, str]:
    plan_dir = overlay_dir / "plans"
    return {
        plan_dir / "INDEX.md": render_client_plan_index(client_label),
        plan_dir / "draft" / ".gitkeep": "",
        plan_dir / "released" / ".gitkeep": "",
        plan_dir / "sessions" / ".gitkeep": "",
    }

def client_planning_skill_template_root() -> Path:
    return (DEFAULT_ROOT_DIR / CLIENT_PLANNING_SKILL_TEMPLATE_REL).resolve()

def render_skill_builder_index(client_label: str) -> str:
    return (
        f"# {client_label} Workflow Index\n"
        "\n"
        "| Workflow | Status | Scope | Notes |\n"
        "|---|---|---|---|\n"
    )

def render_skill_builder_extraction_rule() -> str:
    return (
        "# Workflow Extraction Rule\n"
        "\n"
        "Use this rule when deciding whether a workflow stays product-local or moves upward.\n"
        "\n"
        "## Keep In The Product Repo\n"
        "\n"
        "- The workflow uses product-specific nouns, data contracts, or client policies.\n"
        "- The workflow is only proven in one product or one client.\n"
        "- The runtime depends on product-specific repo structure or business logic.\n"
        "\n"
        "## Extract To `opensource/skills`\n"
        "\n"
        "- The reusable part is a portable agent workflow, review loop, or operator playbook.\n"
        "- A second real consumer exists, or reuse pressure is already causing duplicated maintenance.\n"
        "- The skill contract can be described without product-specific business nouns.\n"
        "\n"
        "## Keep In `skillbox`\n"
        "\n"
        "- The problem is runtime behavior: installation, sync, bundle curation, client overlays, box behavior, or operator tooling.\n"
        "- The reusable piece is connector/runtime delivery rather than the portable skill contract itself.\n"
        "\n"
        "## Use A Cross-Repo Slice\n"
        "\n"
        "- Put the portable skill contract in `opensource/skills`.\n"
        "- Put runtime/distribution/FWC integration in `skillbox`.\n"
        "- Keep product-specific workflow execution and business data in the product repo.\n"
    )

def client_skill_builder_seed_files(overlay_dir: Path, client_label: str) -> dict[Path, str]:
    return {
        overlay_dir / "workflows" / "INDEX.md": render_skill_builder_index(client_label),
        overlay_dir / "workflows" / "EXTRACTION.md": render_skill_builder_extraction_rule(),
        overlay_dir / "evaluations" / "README.md": (
            "# Evaluations\n"
            "\n"
            "Store scorecards, evaluation runs, regression notes, and acceptance snapshots here.\n"
        ),
        overlay_dir / "invocations" / "README.md": (
            "# Invocations\n"
            "\n"
            "Track copied transcript slices, invocation summaries, or pointers to raw workflow runs here.\n"
        ),
        overlay_dir / "observability" / "README.md": (
            "# Observability\n"
            "\n"
            "Record connector probes, health notes, drift findings, and workflow diagnostics here.\n"
        ),
    }

def client_skill_builder_template_root() -> Path:
    return (DEFAULT_ROOT_DIR / CLIENT_SKILL_BUILDER_TEMPLATE_REL).resolve()

def client_scaffold_pack(pack_name: str | None) -> str:
    pack = str(pack_name or "planning").strip() or "planning"
    if pack in {"planning", "skill-builder", "hybrid"}:
        return pack
    raise RuntimeError(
        f"Unknown client scaffold pack: {pack}. Supported packs: planning, skill-builder, hybrid."
    )

def client_scaffold_pack_required_skills(pack_name: str | None) -> list[str]:
    pack = client_scaffold_pack(pack_name)
    if pack == "planning":
        return copy.deepcopy(HARDENED_CLIENT_PLANNING_SKILLS)
    if pack == "skill-builder":
        return copy.deepcopy(HARDENED_CLIENT_SKILL_BUILDER_SKILLS)
    return copy.deepcopy(HARDENED_CLIENT_HYBRID_SKILLS)

def client_scaffold_pack_skill_templates(pack_name: str | None) -> list[tuple[str, Path, str]]:
    pack = client_scaffold_pack(pack_name)
    template_pairs: list[tuple[str, Path, str]] = []
    if pack in {"planning", "hybrid"}:
        planning_root = client_planning_skill_template_root()
        template_pairs.extend(
            (skill_name, planning_root / skill_name, "shared")
            for skill_name in HARDENED_CLIENT_PLANNING_SKILLS
        )
    if pack in {"skill-builder", "hybrid"}:
        skill_builder_root = client_skill_builder_template_root()
        template_pairs.extend(
            (skill_name, skill_builder_root / skill_name, "local")
            for skill_name in HARDENED_CLIENT_SKILL_BUILDER_SKILLS
        )
    return template_pairs

def client_scaffold_shared_skills_dir(overlay_dir: Path) -> Path:
    return overlay_dir.parent / "_shared" / "skills"

def client_scaffold_local_skills_dir(overlay_dir: Path) -> Path:
    return overlay_dir / "skills"

def client_scaffold_skill_repo_entries(pack_name: str | None) -> list[dict[str, Any]]:
    pack = client_scaffold_pack(pack_name)
    entries: list[dict[str, Any]] = []
    if pack in {"planning", "hybrid"}:
        entries.append(
            {
                "path": "../_shared/skills",
                "pick": copy.deepcopy(HARDENED_CLIENT_PLANNING_SKILLS),
            }
        )
    if pack in {"skill-builder", "hybrid"}:
        entries.append(
            {
                "path": "./skills",
                "pick": copy.deepcopy(HARDENED_CLIENT_SKILL_BUILDER_SKILLS),
            }
        )
    return entries

def render_client_scaffold_skill_repos(client_label: str, pack_name: str | None) -> str:
    lines = [
        f"# {client_label} client-specific skill repos.",
        "",
        "version: 2",
        "",
        "skill_repos:",
    ]
    for entry in client_scaffold_skill_repo_entries(pack_name):
        lines.append(f"  - path: {entry['path']}")
        pick_line = ", ".join(str(item) for item in entry.get("pick") or [])
        lines.append(f"    pick: [{pick_line}]")
    return "\n".join(lines) + "\n"

def client_scaffold_keep_files(overlay_dir: Path, pack_name: str | None) -> dict[Path, str]:
    keep_files: dict[Path, str] = {client_scaffold_local_skills_dir(overlay_dir) / ".gitkeep": ""}
    if client_scaffold_pack(pack_name) in {"planning", "hybrid"}:
        keep_files[client_scaffold_shared_skills_dir(overlay_dir) / ".gitkeep"] = ""
    return keep_files

def client_scaffold_seed_files(
    overlay_dir: Path,
    client_label: str,
    pack_name: str | None,
) -> dict[Path, str]:
    pack = client_scaffold_pack(pack_name)
    if pack == "planning":
        return client_plan_seed_files(overlay_dir, client_label)
    if pack == "skill-builder":
        return client_skill_builder_seed_files(overlay_dir, client_label)
    seed_files = client_plan_seed_files(overlay_dir, client_label)
    seed_files.update(client_skill_builder_seed_files(overlay_dir, client_label))
    return seed_files

def sync_client_scaffold_seed_files(
    root_dir: Path,
    overlay_dir: Path,
    client_label: str,
    scaffold_pack: str,
    *,
    dry_run: bool,
) -> list[str]:
    actions: list[str] = []
    for seed_path, content in client_scaffold_seed_files(overlay_dir, client_label, scaffold_pack).items():
        ensure_directory(seed_path.parent, dry_run=dry_run)
        if seed_path.is_file():
            existing = seed_path.read_text(encoding="utf-8")
            if existing == content:
                continue
            if existing.strip():
                continue
        if not dry_run:
            atomic_write_text(seed_path, content)
        actions.append(f"write-file: {repo_rel(root_dir, seed_path)}")
    return actions

def ensure_client_scaffold_skill_sources(
    root_dir: Path,
    overlay_dir: Path,
    scaffold_pack: str,
    *,
    dry_run: bool,
) -> list[str]:
    actions: list[str] = []
    ensure_directory(client_scaffold_local_skills_dir(overlay_dir), dry_run=dry_run)
    if client_scaffold_pack(scaffold_pack) in {"planning", "hybrid"}:
        ensure_directory(client_scaffold_shared_skills_dir(overlay_dir), dry_run=dry_run)
    for skill_name, source_dir, placement in client_scaffold_pack_skill_templates(scaffold_pack):
        if not source_dir.is_dir():
            raise RuntimeError(f"Missing scaffold skill template for {skill_name} at {source_dir}")
        if placement == "shared":
            target_root = client_scaffold_shared_skills_dir(overlay_dir)
        else:
            target_root = client_scaffold_local_skills_dir(overlay_dir)
        target_dir = target_root / skill_name
        if target_dir.exists():
            continue
        if not dry_run:
            copy_tree_atomic(source_dir, target_dir)
        actions.append(f"copy-skill-template: {repo_rel(root_dir, target_dir)}")
    return actions

def default_client_scaffold_files(
    root_dir: Path,
    env_values: dict[str, str],
    client_id: str,
    client_label: str,
    client_root: str,
    client_default_cwd: str,
) -> tuple[dict[Path, str], str]:
    scaffold_pack = "planning"
    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)

    overlay_path = overlay_dir / "overlay.yaml"
    skill_repos_path = overlay_dir / "skill-repos.yaml"
    overlay_client = base_client_overlay(
        client_id=client_id,
        client_label=client_label,
        client_root=client_root,
        client_default_cwd=client_default_cwd,
        scaffold_pack=scaffold_pack,
    )

    target_files = {
        overlay_path: render_yaml_document({"version": 1, "client": overlay_client}),
        skill_repos_path: render_client_scaffold_skill_repos(client_label, scaffold_pack),
    }
    target_files.update(client_scaffold_keep_files(overlay_dir, scaffold_pack))
    target_files.update(client_scaffold_seed_files(overlay_dir, client_label, scaffold_pack))
    return target_files, scaffold_pack

def merge_client_overlay(base_client: dict[str, Any], blueprint_client: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base_client)
    additive_sections = (
        "repo_roots",
        "repos",
        "artifacts",
        "env_files",
        "skills",
        "tasks",
        "services",
        "logs",
        "checks",
        "ingress_routes",
    )
    scalar_items = dict(blueprint_client)

    for key in additive_sections:
        if key not in scalar_items:
            continue
        raw_items = scalar_items.pop(key)
        if raw_items is None:
            continue
        if not isinstance(raw_items, list):
            raise RuntimeError(f"Expected blueprint client.{key} to be a list.")
        merged.setdefault(key, [])
        merged[key].extend(copy.deepcopy(raw_items))

    for key, value in scalar_items.items():
        merged[key] = copy.deepcopy(value)

    return merged

def build_blueprinted_client_scaffold_files(
    root_dir: Path,
    env_values: dict[str, str],
    client_id: str,
    client_label: str,
    client_root: str,
    client_default_cwd: str,
    explicit_label: bool,
    explicit_default_cwd: bool,
    blueprint: dict[str, Any],
    blueprint_assignments: list[tuple[str, str]],
) -> tuple[dict[Path, str], str]:
    values = {
        "CLIENT_ID": client_id,
        "CLIENT_LABEL": client_label,
        "CLIENT_ROOT": client_root,
        "CLIENT_DEFAULT_CWD": client_default_cwd,
    }
    for key, raw_value in blueprint_assignments:
        values[key] = str(resolve_known_placeholders(raw_value, values))

    missing_required: list[str] = []
    for variable in blueprint["variables"]:
        name = str(variable["name"])
        if name in values and values[name].strip():
            continue
        default = variable.get("default")
        if default is not None:
            values[name] = str(resolve_known_placeholders(default, values))
            continue
        if variable.get("required"):
            missing_required.append(name)
            continue
        values[name] = ""

    if missing_required:
        raise RuntimeError(
            "Client blueprint is missing required values for: "
            + ", ".join(sorted(missing_required))
        )

    rendered_client = resolve_known_placeholders(copy.deepcopy(blueprint["client"]), values)
    if not isinstance(rendered_client, dict):
        raise RuntimeError("Expected rendered blueprint client to be a mapping.")

    overlay_client = merge_client_overlay(
        base_client_overlay(
            client_id=client_id,
            client_label=client_label,
            client_root=client_root,
            client_default_cwd=client_default_cwd,
        ),
        rendered_client,
    )
    scaffold = copy.deepcopy(blueprint.get("scaffold") or {})
    if scaffold:
        overlay_client["scaffold"] = scaffold
    overlay_client["id"] = client_id
    if explicit_label:
        overlay_client["label"] = client_label
    else:
        overlay_client.setdefault("label", client_label)
    if explicit_default_cwd:
        overlay_client["default_cwd"] = client_default_cwd
    else:
        overlay_client.setdefault("default_cwd", client_default_cwd)
    if "connectors" in overlay_client:
        scaffolded_connectors = scaffold_connector_entries(
            overlay_client.get("connectors"),
            values,
            client_id=client_id,
        )
        if scaffolded_connectors:
            overlay_client["connectors"] = scaffolded_connectors
        else:
            overlay_client.pop("connectors", None)
    from .client import (
        ensure_client_overlay_context_shape,
        ensure_client_overlay_scaffold_shape,
        ensure_client_overlay_skillset_shape,
    )

    scaffold_pack = ensure_client_overlay_scaffold_shape(overlay_client)
    ensure_client_overlay_skillset_shape(overlay_client, client_id)
    ensure_client_overlay_context_shape(
        overlay_client,
        str(overlay_client["default_cwd"]),
        scaffold_pack,
    )

    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)

    overlay_path = overlay_dir / "overlay.yaml"
    skill_repos_path = overlay_dir / "skill-repos.yaml"
    target_files = {
        overlay_path: render_yaml_document({"version": 1, "client": overlay_client}),
        skill_repos_path: render_client_scaffold_skill_repos(str(overlay_client["label"]), scaffold_pack),
    }
    target_files.update(client_scaffold_keep_files(overlay_dir, scaffold_pack))
    target_files.update(
        client_scaffold_seed_files(
            overlay_dir,
            str(overlay_client["label"]),
            scaffold_pack,
        )
    )
    return target_files, scaffold_pack

def scaffold_client_overlay(
    root_dir: Path,
    client_id: str,
    label: str | None,
    default_cwd: str | None,
    root_path: str | None,
    blueprint_name: str | None,
    blueprint_assignments: list[tuple[str, str]],
    dry_run: bool,
    force: bool,
) -> tuple[list[str], dict[str, Any] | None]:
    client_id = validate_client_id(client_id)
    env_values = load_runtime_env(root_dir)
    client_label = (label or titleize_client_id(client_id)).strip()
    client_root = (root_path or "${SKILLBOX_MONOSERVER_ROOT}").strip()
    client_default_cwd = (default_cwd or client_root).strip()

    if blueprint_name and not blueprint_assignments:
        blueprint_assignments = []

    blueprint_metadata: dict[str, Any] | None = None
    if blueprint_name:
        blueprint_path = resolve_client_blueprint_path(root_dir, blueprint_name)
        blueprint = load_client_blueprint(blueprint_path)
        target_files, scaffold_pack = build_blueprinted_client_scaffold_files(
            root_dir=root_dir,
            env_values=env_values,
            client_id=client_id,
            client_label=client_label,
            client_root=client_root,
            client_default_cwd=client_default_cwd,
            explicit_label=label is not None,
            explicit_default_cwd=default_cwd is not None,
            blueprint=blueprint,
            blueprint_assignments=blueprint_assignments,
        )
        blueprint_metadata = {
            "id": blueprint["id"],
            "path": blueprint["path"],
        }
    else:
        if blueprint_assignments:
            raise RuntimeError("`--set` requires `--blueprint`.")
        target_files, scaffold_pack = default_client_scaffold_files(
            root_dir=root_dir,
            env_values=env_values,
            client_id=client_id,
            client_label=client_label,
            client_root=client_root,
            client_default_cwd=client_default_cwd,
        )

    overlay_dir = client_config_host_dir(root_dir, env_values, client_id).resolve()
    existing_paths = sorted(
        repo_rel(root_dir, path)
        for path in target_files
        if path.exists()
        and (
            path.resolve() == overlay_dir
            or overlay_dir in path.resolve().parents
        )
    )
    if existing_paths and not force:
        raise RuntimeError(
            "Client scaffold already exists for "
            f"{client_id}: {', '.join(existing_paths)}. Re-run with --force to overwrite."
        )

    actions: list[str] = []
    for path, content in target_files.items():
        write_text_file(path, content, dry_run=dry_run)
        actions.append(f"write-file: {repo_rel(root_dir, path)}")

    overlay_dir = client_config_host_dir(root_dir, env_values, client_id)
    actions.extend(
        ensure_client_scaffold_skill_sources(
            root_dir,
            overlay_dir,
            scaffold_pack,
            dry_run=dry_run,
        )
    )

    return actions, blueprint_metadata
