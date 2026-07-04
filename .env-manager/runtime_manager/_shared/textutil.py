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
    file_sha256,
)

CLIENT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

BLUEPRINT_VARIABLE_PATTERN = re.compile(r"^[A-Z0-9_]+$")

SCAFFOLD_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")

def human_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0 or unit == "GiB":
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{num_bytes}B"

def titleize_client_id(client_id: str) -> str:
    return " ".join(part.capitalize() for part in client_id.split("-"))

def validate_client_id(client_id: str) -> str:
    """Reject a bad client slug at creation time (client-init / onboard).

    Decides accept/reject with the SAME canonical runtime-id grammar the
    model-load gate uses (``RUNTIME_ID_PATTERN`` ==
    ``^[a-z0-9][a-z0-9_-]{0,63}$``), so a slug accepted here is exactly a slug
    ``build_runtime_model`` will later accept — no client can be scaffolded
    that the model then rejects, and an ``a/b`` / ``../x`` / leading-dash /
    empty / 200-char / uppercase slug is refused before any directory is
    created.

    Surfaces via a plain ``RuntimeError`` carrying the established
    ``invalid_client_id`` recovery affordance (``classify_error`` keys off the
    "Invalid client id" message) rather than the lower-level
    ``RUNTIME_ID_INVALID`` model code, since this is the operator-facing
    create-time front door with its own stable code. The grammar is the single
    shared one (STEP 4) even though the surfaced code differs by surface.
    """
    normalized = client_id.strip()
    if not RUNTIME_ID_PATTERN.match(normalized):
        raise RuntimeError(
            f"Invalid client id {client_id!r}. Use lowercase letters, numbers, "
            f"'-' or '_' (must start alphanumeric, 1-64 chars): matches "
            f"{RUNTIME_ID_PATTERN_TEXT}."
        )
    return normalized

def parse_key_value_assignments(raw_assignments: list[str], option_name: str) -> list[tuple[str, str]]:
    assignments: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for raw_assignment in raw_assignments:
        if "=" not in raw_assignment:
            raise RuntimeError(f"{option_name} expects KEY=VALUE assignments, got {raw_assignment!r}.")
        raw_key, raw_value = raw_assignment.split("=", 1)
        key = raw_key.strip()
        if not key or not BLUEPRINT_VARIABLE_PATTERN.fullmatch(key):
            raise RuntimeError(
                f"{option_name} key {raw_key!r} is invalid. Use uppercase letters, numbers, and underscores."
            )
        if key in seen_keys:
            raise RuntimeError(f"Duplicate {option_name} assignment for {key}.")
        assignments.append((key, raw_value))
        seen_keys.add(key)
    return assignments

def resolve_known_placeholders(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        def replacer(match: re.Match[str]) -> str:
            key = match.group(1)
            return mapping.get(key, match.group(0))

        return SCAFFOLD_PLACEHOLDER_PATTERN.sub(replacer, value)
    if isinstance(value, list):
        return [resolve_known_placeholders(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: resolve_known_placeholders(item, mapping) for key, item in value.items()}
    return value

def split_csv_values(raw_value: Any) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        return [value.strip() for value in raw_value.split(",") if value.strip()]
    if isinstance(raw_value, list):
        values: list[str] = []
        for item in raw_value:
            if isinstance(item, str):
                values.extend(split_csv_values(item))
                continue
            text = str(item).strip()
            if text:
                values.append(text)
        return values
    text = str(raw_value).strip()
    return [text] if text else []

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

def unique_string_field_values(item: dict[str, Any], field: str) -> list[str]:
    raw_values = item.get(field) or []
    if not isinstance(raw_values, list):
        return []

    values: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        value = str(raw_value).strip()
        if not value or value in seen:
            continue
        values.append(value)
        seen.add(value)
    return values

def json_or_none(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None

def walk_values(value: Any) -> list[Any]:
    values = [value]
    if isinstance(value, dict):
        for nested in value.values():
            values.extend(walk_values(nested))
    elif isinstance(value, list):
        for nested in value:
            values.extend(walk_values(nested))
    return values

def directory_file_entries(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if not path.is_dir():
        return entries

    for file_path in sorted(child for child in path.rglob("*") if child.is_file()):
        entries.append((file_path.relative_to(path).as_posix(), file_sha256(file_path)))
    return entries

def normalize_bundle_rel_path(raw_value: Any, *, label: str) -> str:
    value = str(raw_value or "").strip()
    if not value:
        raise RuntimeError(f"{label} is missing")

    rel_path = PurePosixPath(value)
    if rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.parts:
        raise RuntimeError(f"{label} must be a relative path inside the bundle")
    return rel_path.as_posix()
