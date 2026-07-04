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
from .fs import (
    atomic_write_text,
)

VALID_REPO_SOURCE_KINDS = {"bind", "directory", "git", "manual"}

VALID_SYNC_MODES = {"external", "ensure-directory", "clone-if-missing", "manual"}

VALID_ARTIFACT_SOURCE_KINDS = {"file", "manual", "url"}

VALID_ARTIFACT_SYNC_MODES = {"copy-if-missing", "download-if-missing", "manual"}

VALID_ENV_FILE_SOURCE_KINDS = {"file", "manual"}

VALID_ENV_FILE_SYNC_MODES = {"write", "manual"}

VALID_HEALTHCHECK_TYPES = {"http", "path_exists", "process_running", "port"}

VALID_CHECK_TYPES = {"path_exists"}

VALID_TASK_SUCCESS_TYPES = {"path_exists", "all_outputs_exist", "port_listening"}

LOCKFILE_VERSION = 1

DEFAULT_SERVICE_START_WAIT_SECONDS = 30.0

DEFAULT_SERVICE_STOP_WAIT_SECONDS = 5.0

DEFAULT_LOG_TAIL_LINES = 40

DEFAULT_TASK_TIMEOUT_SECONDS = 1800.0  # 30 minutes; overridable via task.timeout_seconds

PATH_LIKE_ENV_KEYS = {
    "SKILLBOX_WORKSPACE_ROOT",
    "SKILLBOX_REPOS_ROOT",
    "SKILLBOX_SKILLS_ROOT",
    "SKILLBOX_LOG_ROOT",
    "SKILLBOX_HOME_ROOT",
    "SKILLBOX_MONOSERVER_ROOT",
    "SKILLBOX_CLIENTS_ROOT",
    "SKILLBOX_SWIMMERS_REPO",
    "SKILLBOX_SWIMMERS_INSTALL_DIR",
    "SKILLBOX_SWIMMERS_BIN",
    "SKILLBOX_DCG_BIN",
    "SKILLBOX_RCH_BIN",
    "SKILLBOX_RCHD_BIN",
    "SKILLBOX_RCH_WORKER_BIN",
    "SKILLBOX_RCH_WORKERS_CONFIG",
    "SKILLBOX_SBH_BIN",
    "SKILLBOX_SBH_CONFIG",
    "SKILLBOX_CASS_BIN",
    "SKILLBOX_CM_BIN",
    "SKILLBOX_UBS_BIN",
    "SKILLBOX_APR_BIN",
    "SKILLBOX_INGRESS_ROUTE_FILE",
    "SKILLBOX_INGRESS_NGINX_CONFIG",
}

def upsert_env_file_values(path: Path, updates: dict[str, str]) -> bool:
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing_text.splitlines()
    updated_keys: set[str] = set()
    new_lines: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            raw_key, _raw_value = stripped.split("=", 1)
            key = raw_key.strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(raw_line)

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    serialized = "\n".join(new_lines).rstrip()
    if serialized:
        serialized += "\n"
    if serialized == existing_text:
        return False
    atomic_write_text(path, serialized)
    return True

def require_yaml(feature: str) -> Any:
    if yaml is None:
        raise RuntimeError(
            f"Missing PyYAML. Install `python3-yaml` or `pip install pyyaml` to {feature}."
        )
    return yaml

def render_yaml_document(document: Any) -> str:
    yaml_mod = require_yaml("render client blueprints")
    return yaml_mod.safe_dump(document, sort_keys=False).rstrip() + "\n"
