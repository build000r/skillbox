from __future__ import annotations

import copy
import os
import re
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


RUNTIME_ENV_KEYS = [
    "SKILLBOX_NAME",
    "SKILLBOX_STORAGE_PROVIDER",
    "SKILLBOX_STATE_ROOT",
    "SKILLBOX_STORAGE_FILESYSTEM",
    "SKILLBOX_STORAGE_REQUIRED",
    "SKILLBOX_STORAGE_MIN_FREE_GB",
    "SKILLBOX_WORKSPACE_ROOT",
    "SKILLBOX_REPOS_ROOT",
    "SKILLBOX_SKILLS_ROOT",
    "SKILLBOX_LOG_ROOT",
    "SKILLBOX_HOME_ROOT",
    "SKILLBOX_MONOSERVER_ROOT",
    "SKILLBOX_CLIENTS_ROOT",
    "SKILLBOX_API_PORT",
    "SKILLBOX_WEB_PORT",
    "SKILLBOX_SWIMMERS_PORT",
    "SKILLBOX_SWIMMERS_PUBLISH_HOST",
    "SKILLBOX_SWIMMERS_REPO",
    "SKILLBOX_SWIMMERS_INSTALL_DIR",
    "SKILLBOX_SWIMMERS_BIN",
    "SKILLBOX_SWIMMERS_DOWNLOAD_URL",
    "SKILLBOX_SWIMMERS_DOWNLOAD_SHA256",
    "SKILLBOX_SWIMMERS_AUTH_MODE",
    "SKILLBOX_SWIMMERS_AUTH_TOKEN",
    "SKILLBOX_SWIMMERS_OBSERVER_TOKEN",
    "SKILLBOX_DCG_BIN",
    "SKILLBOX_DCG_DOWNLOAD_URL",
    "SKILLBOX_DCG_DOWNLOAD_SHA256",
    "SKILLBOX_DCG_PACKS",
    "SKILLBOX_DCG_MCP_PORT",
    "SKILLBOX_FWC_BIN",
    "SKILLBOX_FWC_DOWNLOAD_URL",
    "SKILLBOX_FWC_DOWNLOAD_SHA256",
    "SKILLBOX_FWC_MCP_PORT",
    "SKILLBOX_FWC_ZONE",
    "SKILLBOX_FWC_CONNECTORS",
    "SKILLBOX_PULSE_INTERVAL",
    "SKILLBOX_INGRESS_PUBLIC_HOST",
    "SKILLBOX_INGRESS_PUBLIC_PORT",
    "SKILLBOX_INGRESS_PUBLIC_BASE_URL",
    "SKILLBOX_INGRESS_PRIVATE_HOST",
    "SKILLBOX_INGRESS_PRIVATE_PORT",
    "SKILLBOX_INGRESS_PRIVATE_BASE_URL",
    "SKILLBOX_INGRESS_ROUTE_FILE",
    "SKILLBOX_INGRESS_NGINX_CONFIG",
]
MANIFEST_ENV_KEYS = RUNTIME_ENV_KEYS + [
    "SKILLBOX_CLIENTS_HOST_ROOT",
    "SKILLBOX_MONOSERVER_HOST_ROOT",
]

PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
RESERVED_TOP_LEVEL_RUNTIME_KEYS = {"version", "selection", "core", "clients"}

PERSISTENCE_CONFIG_INVALID = "PERSISTENCE_CONFIG_INVALID"
PERSISTENCE_CLASS_UNKNOWN = "PERSISTENCE_CLASS_UNKNOWN"
PERSISTENCE_BINDING_CONFLICT = "PERSISTENCE_BINDING_CONFLICT"
DO_VOLUME_LAYOUT_MISSING = "DO_VOLUME_LAYOUT_MISSING"
STATE_ROOT_MISSING = "STATE_ROOT_MISSING"
STATE_ROOT_WRONG_FILESYSTEM = "STATE_ROOT_WRONG_FILESYSTEM"
STATE_ROOT_WRONG_OWNERSHIP = "STATE_ROOT_WRONG_OWNERSHIP"
STATE_ROOT_LOW_SPACE = "STATE_ROOT_LOW_SPACE"
PERSISTENT_PATH_OFF_STATE_ROOT = "PERSISTENT_PATH_OFF_STATE_ROOT"

PERSISTENCE_ERROR_CODES: frozenset[str] = frozenset({
    PERSISTENCE_CONFIG_INVALID,
    PERSISTENCE_CLASS_UNKNOWN,
    PERSISTENCE_BINDING_CONFLICT,
    DO_VOLUME_LAYOUT_MISSING,
    STATE_ROOT_MISSING,
    STATE_ROOT_WRONG_FILESYSTEM,
    STATE_ROOT_WRONG_OWNERSHIP,
    STATE_ROOT_LOW_SPACE,
    PERSISTENT_PATH_OFF_STATE_ROOT,
})
PERSISTENCE_STORAGE_CLASSES: frozenset[str] = frozenset({"persistent", "ephemeral", "external"})
PERSISTENCE_PROVIDERS: frozenset[str] = frozenset({"local", "digitalocean"})
PERSISTENCE_BINDING_ENV_OVERRIDES: dict[str, str] = {
    "clients-root": "SKILLBOX_CLIENTS_HOST_ROOT",
    "monoserver-root": "SKILLBOX_MONOSERVER_HOST_ROOT",
}


# ---------------------------------------------------------------------------
# local_runtime_core_cutover canonical contract
# ---------------------------------------------------------------------------
#
# The records below match `clients/personal/plans/released/
# local_runtime_core_cutover/schema.mmd` and shared.md:256-281. They are the
# canonical shape downstream WG nodes (focus, up, doctor, parity ledger)
# agree on. The runtime manifest / client overlays are free to use nested
# YAML shapes; load time flattens them into these flat records so callers do
# not have to special-case overlay vs. runtime-manifest shapes.

LOCAL_RUNTIME_ENV_BRIDGE_FAILED = "LOCAL_RUNTIME_ENV_BRIDGE_FAILED"
LOCAL_RUNTIME_ENV_OUTPUT_MISSING = "LOCAL_RUNTIME_ENV_OUTPUT_MISSING"
LOCAL_RUNTIME_PROFILE_UNKNOWN = "LOCAL_RUNTIME_PROFILE_UNKNOWN"
LOCAL_RUNTIME_START_BLOCKED = "LOCAL_RUNTIME_START_BLOCKED"
LOCAL_RUNTIME_SERVICE_DEFERRED = "LOCAL_RUNTIME_SERVICE_DEFERRED"
LOCAL_RUNTIME_MODE_UNSUPPORTED = "LOCAL_RUNTIME_MODE_UNSUPPORTED"
LOCAL_RUNTIME_COVERAGE_GAP = "LOCAL_RUNTIME_COVERAGE_GAP"

LOCAL_RUNTIME_ERROR_CODES: frozenset[str] = frozenset({
    LOCAL_RUNTIME_ENV_BRIDGE_FAILED,
    LOCAL_RUNTIME_ENV_OUTPUT_MISSING,
    LOCAL_RUNTIME_PROFILE_UNKNOWN,
    LOCAL_RUNTIME_START_BLOCKED,
    LOCAL_RUNTIME_SERVICE_DEFERRED,
    LOCAL_RUNTIME_MODE_UNSUPPORTED,
    LOCAL_RUNTIME_COVERAGE_GAP,
})

LOCAL_RUNTIME_START_MODES: tuple[str, ...] = ("reuse", "prod", "fresh")
PARITY_LEDGER_ACTIONS: frozenset[str] = frozenset({"declare", "bridge", "build", "drop"})
PARITY_OWNERSHIP_STATES: frozenset[str] = frozenset({"covered", "bridge-only", "deferred", "external"})

CANONICAL_RUNTIME_RECORDS: dict[str, tuple[str, ...]] = {
    "local_runtime_profile": ("id", "label", "service_ids", "default_mode"),
    "legacy_env_bridge": (
        "id",
        "env_tier",
        "legacy_targets",
        "output_root",
        "emit_stubs",
        "profiles",
    ),
    "local_runtime_repo": ("id", "repo_path", "notes"),
    "managed_env_file": (
        "id",
        "repo_id",
        "target_path",
        "source_kind",
        "source_path",
        "required",
        "profiles",
    ),
    # bootstrap_task enforces a XOR between `repo_id` and `bridge_id` at load
    # time (see _validate_bootstrap_task_owner_xor).
    "bootstrap_task": (
        "id",
        "repo_id",
        "bridge_id",
        "kind",
        "command",
        "success_check",
        "profiles",
    ),
    "managed_service": (
        "id",
        "repo_id",
        "kind",
        "health_type",
        "health_target",
        "depends_on",
        "bootstrap_tasks",
        "profiles",
    ),
    "service_mode_command": ("id", "service_id", "mode", "command"),
    "ingress_route": (
        "id",
        "service_id",
        "listener",
        "path",
        "match",
    ),
    "parity_ledger_item": (
        "id",
        "legacy_surface",
        "surface_type",
        "action",
        "ownership_state",
        "intended_profiles",
        "bridge_dependency",
        "request_error",
        "notes",
    ),
}


class LocalRuntimeContractError(RuntimeError):
    """Raised when the canonical contract is violated at load time.

    Carries a stable error-code tag (one of ``LOCAL_RUNTIME_ERROR_CODES``)
    so downstream CLI formatting can map the exception back to the shared
    contract without string-sniffing the message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PersistenceContractError(RuntimeError):
    """Raised when workspace/persistence.yaml violates the declared contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def runtime_manifest_path(root_dir: Path) -> Path:
    return root_dir / "workspace" / "runtime.yaml"


def persistence_manifest_path(root_dir: Path) -> Path:
    return root_dir / "workspace" / "persistence.yaml"


def client_configs_runtime_root(env_values: dict[str, str]) -> Path:
    raw_root = str(env_values.get("SKILLBOX_CLIENTS_ROOT") or "").strip()
    if raw_root:
        return Path(raw_root)
    workspace_root = Path(env_values.get("SKILLBOX_WORKSPACE_ROOT", "/workspace"))
    return workspace_root / "workspace" / "clients"


def client_configs_host_root(root_dir: Path, env_values: dict[str, str]) -> Path:
    raw_root = str(env_values.get("SKILLBOX_CLIENTS_HOST_ROOT") or "").strip()
    if raw_root:
        return host_path_to_absolute_path(root_dir, raw_root)
    try:
        storage = compile_persistence_summary(root_dir, env_values)
    except RuntimeError:
        storage = None
    binding = storage_binding_by_id(storage, "clients-root")
    if binding is not None:
        return Path(str(binding["resolved_host_path"]))
    return root_dir / "workspace" / "clients"


def client_config_runtime_dir(env_values: dict[str, str], client_id: str) -> Path:
    return client_configs_runtime_root(env_values) / client_id


def client_config_host_dir(root_dir: Path, env_values: dict[str, str], client_id: str) -> Path:
    return client_configs_host_root(root_dir, env_values) / client_id


def client_overlay_paths(root_dir: Path, env_values: dict[str, str] | None = None) -> list[Path]:
    if env_values is None:
        env_values = load_runtime_env(root_dir)
    overlays_root = client_configs_host_root(root_dir, env_values)
    if not overlays_root.is_dir():
        return []
    return sorted(path for path in overlays_root.glob("*/overlay.yaml") if path.is_file())


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError(
            "Missing PyYAML. Install `python3-yaml` or `pip install pyyaml` to use runtime commands."
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required file missing: {path}") from exc
    except Exception as exc:  # pragma: no cover - defensive parse path
        raise RuntimeError(f"Failed to parse {path}: {exc}") from exc

    def normalize_scalars(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: normalize_scalars(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize_scalars(item) for item in value]
        if isinstance(value, tuple):
            return [normalize_scalars(item) for item in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    raw = normalize_scalars(raw)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Expected a YAML object in {path}")
    return raw


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"Invalid env line in {path}: {raw_line}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_runtime_env(root_dir: Path) -> dict[str, str]:
    defaults = load_env_file(root_dir / ".env.example")
    live = load_env_file(root_dir / ".env")

    values = defaults | live
    for key in MANIFEST_ENV_KEYS:
        env_value = os.environ.get(key)
        if env_value is not None:
            values[key] = env_value

    derived_defaults = {
        "SKILLBOX_NAME": "skillbox",
        "SKILLBOX_STORAGE_PROVIDER": "local",
        "SKILLBOX_STATE_ROOT": "./.skillbox-state",
        "SKILLBOX_STORAGE_FILESYSTEM": "",
        "SKILLBOX_STORAGE_REQUIRED": "false",
        "SKILLBOX_STORAGE_MIN_FREE_GB": "0",
        "SKILLBOX_WORKSPACE_ROOT": "/workspace",
        "SKILLBOX_REPOS_ROOT": "/workspace/repos",
        "SKILLBOX_SKILLS_ROOT": "/workspace/skills",
        "SKILLBOX_LOG_ROOT": "/workspace/logs",
        "SKILLBOX_HOME_ROOT": "/home/sandbox",
        "SKILLBOX_MONOSERVER_ROOT": "/monoserver",
        "SKILLBOX_API_PORT": "8000",
        "SKILLBOX_WEB_PORT": "3000",
        "SKILLBOX_SWIMMERS_PORT": "3210",
        "SKILLBOX_SWIMMERS_PUBLISH_HOST": "127.0.0.1",
        "SKILLBOX_SWIMMERS_REPO": "/monoserver/swimmers",
        "SKILLBOX_SWIMMERS_INSTALL_DIR": "/home/sandbox/.local/bin",
        "SKILLBOX_SWIMMERS_BIN": "/home/sandbox/.local/bin/swimmers",
        "SKILLBOX_SWIMMERS_DOWNLOAD_URL": "",
        "SKILLBOX_SWIMMERS_DOWNLOAD_SHA256": "",
        "SKILLBOX_SWIMMERS_AUTH_MODE": "",
        "SKILLBOX_SWIMMERS_AUTH_TOKEN": "",
        "SKILLBOX_SWIMMERS_OBSERVER_TOKEN": "",
        "SKILLBOX_DCG_BIN": "/home/sandbox/.local/bin/dcg",
        "SKILLBOX_DCG_DOWNLOAD_URL": "",
        "SKILLBOX_DCG_DOWNLOAD_SHA256": "",
        "SKILLBOX_DCG_PACKS": "core.git,core.filesystem",
        "SKILLBOX_DCG_MCP_PORT": "3220",
        "SKILLBOX_FWC_BIN": "/home/sandbox/.local/bin/fwc",
        "SKILLBOX_FWC_DOWNLOAD_URL": "",
        "SKILLBOX_FWC_DOWNLOAD_SHA256": "",
        "SKILLBOX_FWC_MCP_PORT": "3221",
        "SKILLBOX_FWC_ZONE": "work",
        "SKILLBOX_FWC_CONNECTORS": "github,slack,linear",
        "SKILLBOX_PULSE_INTERVAL": "30",
        "SKILLBOX_INGRESS_PUBLIC_HOST": "127.0.0.1",
        "SKILLBOX_INGRESS_PUBLIC_PORT": "8080",
        "SKILLBOX_INGRESS_PUBLIC_BASE_URL": "",
        "SKILLBOX_INGRESS_PRIVATE_HOST": "127.0.0.1",
        "SKILLBOX_INGRESS_PRIVATE_PORT": "9080",
        "SKILLBOX_INGRESS_PRIVATE_BASE_URL": "",
        "SKILLBOX_INGRESS_ROUTE_FILE": "/workspace/logs/runtime/ingress-routes.json",
        "SKILLBOX_INGRESS_NGINX_CONFIG": "/workspace/logs/runtime/ingress-nginx.conf",
        "ROOT_DIR": str(root_dir),
    }

    for key, value in derived_defaults.items():
        values.setdefault(key, value)
    values.setdefault(
        "SKILLBOX_CLIENTS_ROOT",
        f"{values['SKILLBOX_WORKSPACE_ROOT']}/workspace/clients",
    )
    values.setdefault("SKILLBOX_CLIENTS_HOST_ROOT", "./.skillbox-state/clients")
    values.setdefault("SKILLBOX_MONOSERVER_HOST_ROOT", "./.skillbox-state/monoserver")
    return values


def _persistence_error(code: str, message: str) -> PersistenceContractError:
    return PersistenceContractError(code, message)


def _persistence_path(value: str, *, field_name: str) -> PurePosixPath:
    text = str(value or "").strip()
    if not text:
        raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"{field_name} is required")
    path = PurePosixPath(text)
    if not path.is_absolute():
        raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"{field_name} must be an absolute runtime path")
    return path


def _persistence_relative_path(value: str, *, field_name: str) -> PurePosixPath:
    text = str(value or "").strip()
    if not text:
        raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"{field_name} is required")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"{field_name} must be relative to SKILLBOX_STATE_ROOT")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"{field_name} must not contain traversal segments")
    return path


def _env_bool(env_values: dict[str, str], key: str, default: bool) -> bool:
    raw = str(env_values.get(key, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(env_values: dict[str, str], key: str, default: float) -> float:
    raw = str(env_values.get(key, "")).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"{key} must be numeric") from exc


def storage_binding_by_id(storage: dict[str, Any] | None, binding_id: str) -> dict[str, Any] | None:
    if not storage:
        return None
    for binding in storage.get("bindings") or []:
        if str(binding.get("id") or "").strip() == binding_id:
            return binding
    return None


def _resolve_persistence_source_ref(root_dir: Path, source_ref: str) -> Path:
    if source_ref == "root_dir":
        return root_dir.resolve()
    raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"Unsupported persistence source_ref {source_ref!r}")


def compile_persistence_summary(root_dir: Path, env_values: dict[str, str]) -> dict[str, Any]:
    doc = load_yaml(persistence_manifest_path(root_dir))
    state_root_env = str(doc.get("state_root_env") or "SKILLBOX_STATE_ROOT").strip() or "SKILLBOX_STATE_ROOT"
    provider = str(env_values.get("SKILLBOX_STORAGE_PROVIDER") or "local").strip().lower() or "local"
    if provider not in PERSISTENCE_PROVIDERS:
        raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"Unsupported persistence provider {provider!r}")

    raw_targets = doc.get("targets")
    if not isinstance(raw_targets, dict):
        raise _persistence_error(PERSISTENCE_CONFIG_INVALID, "workspace/persistence.yaml must define targets")
    target = raw_targets.get(provider)
    if not isinstance(target, dict):
        raise _persistence_error(DO_VOLUME_LAYOUT_MISSING, f"workspace/persistence.yaml is missing target {provider!r}")

    default_state_root = str(target.get("default_state_root") or "").strip()
    raw_state_root = str(env_values.get(state_root_env) or default_state_root).strip()
    if not raw_state_root:
        raise _persistence_error(STATE_ROOT_MISSING, f"{state_root_env} is required for persistence target {provider!r}")
    state_root = host_path_to_absolute_path(root_dir, raw_state_root)

    raw_bindings = doc.get("bindings")
    if not isinstance(raw_bindings, list):
        raise _persistence_error(PERSISTENCE_CONFIG_INVALID, "workspace/persistence.yaml must define bindings as a list")

    seen_runtime_paths: set[str] = set()
    seen_persistent_relative_paths: set[str] = set()
    bindings: list[dict[str, Any]] = []

    for index, raw_binding in enumerate(raw_bindings, start=1):
        if not isinstance(raw_binding, dict):
            raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"bindings[{index}] must be a mapping")
        binding_id = str(raw_binding.get("id") or "").strip()
        if not binding_id:
            raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"bindings[{index}] is missing id")

        runtime_path = _persistence_path(str(raw_binding.get("runtime_path") or ""), field_name=f"bindings[{binding_id}].runtime_path")
        runtime_path_text = runtime_path.as_posix()
        if runtime_path_text in seen_runtime_paths:
            raise _persistence_error(PERSISTENCE_BINDING_CONFLICT, f"Duplicate persistence runtime_path {runtime_path_text!r}")
        seen_runtime_paths.add(runtime_path_text)

        storage_class = str(raw_binding.get("storage_class") or "").strip().lower()
        if storage_class not in PERSISTENCE_STORAGE_CLASSES:
            raise _persistence_error(PERSISTENCE_CLASS_UNKNOWN, f"Unsupported storage_class {storage_class!r} for binding {binding_id!r}")

        relative_path_text = ""
        source_ref_text = ""
        override_env = PERSISTENCE_BINDING_ENV_OVERRIDES.get(binding_id, "")
        resolved_host_path: Path

        if storage_class == "external":
            source_ref_text = str(raw_binding.get("source_ref") or "").strip()
            if not source_ref_text:
                raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"binding {binding_id!r} requires source_ref")
            if raw_binding.get("relative_path"):
                raise _persistence_error(PERSISTENCE_CONFIG_INVALID, f"binding {binding_id!r} cannot mix source_ref with relative_path")
            resolved_host_path = _resolve_persistence_source_ref(root_dir, source_ref_text)
        else:
            relative_path = _persistence_relative_path(
                str(raw_binding.get("relative_path") or (f"ephemeral/{binding_id}" if storage_class == "ephemeral" else "")),
                field_name=f"binding {binding_id!r} relative_path",
            )
            relative_path_text = relative_path.as_posix()
            if storage_class == "persistent":
                if relative_path_text in seen_persistent_relative_paths:
                    raise _persistence_error(PERSISTENCE_BINDING_CONFLICT, f"Duplicate persistent relative_path {relative_path_text!r}")
                seen_persistent_relative_paths.add(relative_path_text)
            override_value = str(env_values.get(override_env) or "").strip() if override_env else ""
            if override_value:
                resolved_host_path = host_path_to_absolute_path(root_dir, override_value)
            else:
                resolved_host_path = (state_root / Path(relative_path_text)).resolve()

        bindings.append({
            "id": binding_id,
            "runtime_path": runtime_path_text,
            "storage_class": storage_class,
            "relative_path": relative_path_text,
            "source_ref": source_ref_text,
            "resolved_host_path": str(resolved_host_path),
            "override_env": override_env or "",
        })

    return {
        "version": int(doc.get("version", 1) or 1),
        "provider": provider,
        "state_root_env": state_root_env,
        "state_root": str(state_root),
        "raw_state_root": raw_state_root,
        "default_state_root": default_state_root,
        "filesystem": str(env_values.get("SKILLBOX_STORAGE_FILESYSTEM") or "").strip(),
        "required": _env_bool(env_values, "SKILLBOX_STORAGE_REQUIRED", default=provider == "digitalocean"),
        "min_free_gb": _env_float(env_values, "SKILLBOX_STORAGE_MIN_FREE_GB", default=0.0),
        "bindings": bindings,
    }


def resolve_storage_host_path(storage: dict[str, Any] | None, raw_path: str) -> Path | None:
    if not storage:
        return None
    runtime_path = PurePosixPath(str(raw_path))
    matches: list[tuple[int, Path]] = []
    for binding in storage.get("bindings") or []:
        binding_runtime = PurePosixPath(str(binding.get("runtime_path") or ""))
        try:
            relative = runtime_path.relative_to(binding_runtime)
        except ValueError:
            continue
        host_root = Path(str(binding["resolved_host_path"]))
        if relative.parts:
            matches.append((len(binding_runtime.parts), (host_root / Path(*relative.parts)).resolve()))
        else:
            matches.append((len(binding_runtime.parts), host_root.resolve()))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def resolve_placeholders(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        def replacer(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in mapping:
                raise RuntimeError(f"Unknown placeholder {key!r} in runtime manifest")
            return mapping[key]

        return PLACEHOLDER_PATTERN.sub(replacer, value)
    if isinstance(value, list):
        return [resolve_placeholders(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: resolve_placeholders(item, mapping) for key, item in value.items()}
    return value


def runtime_path_to_host_path(
    root_dir: Path,
    env_values: dict[str, str],
    raw_path: str,
    *,
    storage: dict[str, Any] | None = None,
) -> Path:
    resolved_storage = storage
    if resolved_storage is None:
        try:
            resolved_storage = compile_persistence_summary(root_dir, env_values)
        except RuntimeError:
            resolved_storage = None

    storage_match = resolve_storage_host_path(resolved_storage, raw_path)
    if storage_match is not None:
        return storage_match

    path = Path(raw_path)
    workspace_root = Path(env_values["SKILLBOX_WORKSPACE_ROOT"])
    home_root = Path(env_values["SKILLBOX_HOME_ROOT"])
    monoserver_root = Path(env_values.get("SKILLBOX_MONOSERVER_ROOT", "/monoserver"))
    clients_root = client_configs_runtime_root(env_values)
    clients_host_root = client_configs_host_root(root_dir, env_values)
    monoserver_host_root = host_path_to_absolute_path(
        root_dir,
        env_values.get("SKILLBOX_MONOSERVER_HOST_ROOT", ".."),
    )

    try:
        relative = path.relative_to(clients_root)
        return clients_host_root / relative
    except ValueError:
        pass

    try:
        relative = path.relative_to(workspace_root)
        return root_dir / relative
    except ValueError:
        pass

    try:
        relative = path.relative_to(home_root)
        return root_dir / "home" / relative
    except ValueError:
        pass

    try:
        relative = path.relative_to(monoserver_root)
        return monoserver_host_root / relative
    except ValueError:
        pass

    return path


def host_path_to_absolute_path(root_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (root_dir / path).resolve()


def _normalized_items(raw_items: Any, section: str) -> list[dict[str, Any]]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise RuntimeError(f"Expected {section} to be a list in runtime manifest")

    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise RuntimeError(f"Expected every {section} item to be a mapping")
        normalized.append(item)
    return normalized


def _normalized_mapping(raw_value: Any, section: str) -> dict[str, Any]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise RuntimeError(f"Expected {section} to be a mapping in runtime manifest")
    return dict(raw_value)


def _attach_client_scope(items: list[dict[str, Any]], client_id: str) -> list[dict[str, Any]]:
    scoped_items: list[dict[str, Any]] = []
    for item in items:
        scoped_item = dict(item)
        scoped_item["client"] = client_id
        scoped_items.append(scoped_item)
    return scoped_items


def _normalize_client_repo_roots(raw_items: Any, client_id: str, section: str) -> list[dict[str, Any]]:
    repo_roots = []
    for item in _normalized_items(raw_items, section):
        repo_root = dict(item)
        repo_root.setdefault("kind", "repo-root")
        repo_root.setdefault("source", {"kind": "bind"})
        repo_root.setdefault("sync", {"mode": "external"})
        repo_root["client"] = client_id
        repo_roots.append(repo_root)
    return repo_roots


def _normalize_profile_items(raw_items: Any, profile_id: str, section: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in _normalized_items(raw_items, section):
        profiled_item = dict(item)
        profiles = [str(value).strip() for value in profiled_item.get("profiles") or [] if str(value).strip()]
        if not profiles:
            profiled_item["profiles"] = [profile_id]
        normalized.append(profiled_item)
    return normalized


def _empty_runtime_sections() -> dict[str, list[dict[str, Any]]]:
    return {
        "repos": [],
        "artifacts": [],
        "env_files": [],
        "skills": [],
        "tasks": [],
        "services": [],
        "logs": [],
        "checks": [],
        "bridges": [],
        "service_mode_commands": [],
        "ingress_routes": [],
        "parity_ledger": [],
    }


def _collect_core_sections(resolved: dict[str, Any], scoped_runtime: bool) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[dict[str, Any]]]]:
    core = _normalized_mapping(resolved.get("core"), "core") if scoped_runtime else resolved
    selection = _normalized_mapping(resolved.get("selection"), "selection") if scoped_runtime else {}
    sections = _empty_runtime_sections()
    for name in sections:
        section_name = f"core.{name}" if scoped_runtime else name
        sections[name] = _normalized_items(core.get(name), section_name)
    return core, selection, sections


def _extend_sections_with_profiles(
    sections: dict[str, list[dict[str, Any]]],
    resolved: dict[str, Any],
    scoped_runtime: bool,
) -> None:
    if not scoped_runtime:
        return

    for profile_id, raw_profile in resolved.items():
        if profile_id in RESERVED_TOP_LEVEL_RUNTIME_KEYS:
            continue
        profile = _normalized_mapping(raw_profile, profile_id)
        for name, items in sections.items():
            items.extend(_normalize_profile_items(profile.get(name), profile_id, f"{profile_id}.{name}"))


def _resolved_clients(
    resolved: dict[str, Any],
    scoped_runtime: bool,
    overlay_clients: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    raw_clients = _normalized_items(resolved.get("clients"), "clients") if scoped_runtime else []
    raw_clients.extend(_normalized_items(overlay_clients, "client overlays"))
    return raw_clients


def _extend_sections_with_client_items(
    sections: dict[str, list[dict[str, Any]]],
    client_id: str,
    client: dict[str, Any],
) -> None:
    sections["repos"].extend(
        _normalize_client_repo_roots(
            client.get("repo_roots"),
            client_id=client_id,
            section=f"clients[{client_id}].repo_roots",
        )
    )
    for name, items in sections.items():
        if name == "repos":
            extra = _normalized_items(client.get(name), f"clients[{client_id}].{name}")
            items.extend(_attach_client_scope(extra, client_id))
            continue
        extra = _normalized_items(client.get(name), f"clients[{client_id}].{name}")
        items.extend(_attach_client_scope(extra, client_id))


def _collect_client_metadata(
    raw_clients: list[dict[str, Any]],
    sections: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    clients_meta: list[dict[str, Any]] = []
    seen_client_ids: set[str] = set()

    for client in raw_clients:
        client_id = str(client.get("id", "")).strip()
        if not client_id:
            source = str(client.get("_overlay_path") or "runtime manifest")
            raise RuntimeError(f"Client definition in {source} is missing id")
        if client_id in seen_client_ids:
            raise RuntimeError(f"Duplicate runtime client id: {client_id}")
        seen_client_ids.add(client_id)

        client_meta: dict[str, Any] = {
            "id": client_id,
            "label": str(client.get("label") or client_id),
            "default_cwd": client.get("default_cwd"),
        }
        if client.get("_overlay_path"):
            client_meta["_overlay_path"] = client["_overlay_path"]
        if "connectors" in client:
            client_meta["connectors"] = copy.deepcopy(client.get("connectors"))
        if client.get("dcg"):
            client_meta["dcg"] = client["dcg"]
        if client.get("context"):
            client_meta["context"] = client["context"]
        clients_meta.append(client_meta)
        _extend_sections_with_client_items(sections, client_id, client)

    return clients_meta


def load_client_overlays(root_dir: Path, env_values: dict[str, str]) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    for path in client_overlay_paths(root_dir, env_values):
        overlay_doc = load_yaml(path)
        raw_client = overlay_doc.get("client")
        if raw_client is None:
            raise RuntimeError(f"Expected top-level `client` mapping in {path}")
        if not isinstance(raw_client, dict):
            raise RuntimeError(f"Expected `client` to be a mapping in {path}")
        resolved_client = resolve_placeholders(raw_client, env_values)
        resolved_client["_overlay_path"] = str(path)
        overlays.append(resolved_client)
    return overlays


def _flatten_env_file_record(env_file: dict[str, Any]) -> None:
    """Flatten nested overlay YAML shapes onto a managed_env_file record.

    Overlay authors write ``source.kind`` / ``source.source_path`` /
    ``source.path``. The canonical record stores ``source_kind`` and
    ``source_path`` at the top level. The nested ``source`` dict is kept
    intact for backward compatibility with existing helpers.
    """
    source = env_file.get("source") or {}
    if isinstance(source, dict):
        if "source_kind" not in env_file and source.get("kind") is not None:
            env_file["source_kind"] = source.get("kind")
        if "source_path" not in env_file:
            raw_source_path = source.get("source_path") or source.get("path")
            if raw_source_path is not None:
                env_file["source_path"] = raw_source_path
    if "target_path" not in env_file and env_file.get("path"):
        env_file["target_path"] = env_file.get("path")
    if "repo_id" not in env_file and env_file.get("repo"):
        env_file["repo_id"] = env_file.get("repo")


def _flatten_service_record(service: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten nested overlay YAML shapes onto a managed_service record and
    extract any ``commands.<mode>`` entries into ``service_mode_command``
    records. Returns the list of extracted service_mode_command dicts.
    """
    healthcheck = service.get("healthcheck") or {}
    if isinstance(healthcheck, dict):
        if "health_type" not in service and healthcheck.get("type") is not None:
            service["health_type"] = healthcheck.get("type")
        if "health_target" not in service:
            target = (
                healthcheck.get("url")
                or healthcheck.get("port")
                or healthcheck.get("path")
                or healthcheck.get("pattern")
            )
            if target is not None:
                service["health_target"] = target
    if "repo_id" not in service and service.get("repo"):
        service["repo_id"] = service.get("repo")

    extracted: list[dict[str, Any]] = []
    raw_commands = service.get("commands")
    if isinstance(raw_commands, dict) and raw_commands:
        service_id = str(service.get("id", "")).strip()
        for mode, command in raw_commands.items():
            mode_str = str(mode).strip()
            if not mode_str:
                continue
            extracted.append(
                {
                    "id": f"{service_id}:{mode_str}" if service_id else mode_str,
                    "service_id": service_id,
                    "mode": mode_str,
                    "command": command,
                    "profiles": list(service.get("profiles") or []),
                    "client": service.get("client", ""),
                }
            )
    return extracted


def _flatten_bootstrap_task_record(task: dict[str, Any]) -> None:
    """Flatten the overlay shape onto a bootstrap_task record."""
    if "repo_id" not in task and task.get("repo"):
        task["repo_id"] = task.get("repo")
    # bridge_id is already the canonical key; nothing to flatten.


def _validate_bootstrap_task_owner_xor(tasks: list[dict[str, Any]]) -> None:
    """Enforce the XOR rule from shared.md:274-277 and backend.md Rule 6.

    Every ``bootstrap_task`` declares exactly one of ``repo_id`` or
    ``bridge_id``. Declaring both or neither is a coverage gap and is
    reported with the stable ``LOCAL_RUNTIME_COVERAGE_GAP`` code before any
    mutation happens.

    Only tasks that actually look like bootstrap tasks are checked — a task
    that neither has ``kind == "bootstrap"`` nor declares ``bridge_id`` /
    ``repo_id`` is ignored so the existing core-runtime tasks (which have
    neither owner and are not bootstrap tasks) keep validating cleanly.
    """
    for task in tasks:
        kind = str(task.get("kind") or "").strip()
        has_repo = bool(str(task.get("repo_id") or "").strip())
        has_bridge = bool(str(task.get("bridge_id") or "").strip())
        is_bootstrap = kind == "bootstrap" or has_repo or has_bridge
        if not is_bootstrap:
            continue
        if has_repo and has_bridge:
            raise LocalRuntimeContractError(
                LOCAL_RUNTIME_COVERAGE_GAP,
                (
                    f"bootstrap_task {task.get('id', '(missing id)')!r} declares "
                    "both repo_id and bridge_id; exactly one is allowed"
                ),
            )
        if not has_repo and not has_bridge:
            raise LocalRuntimeContractError(
                LOCAL_RUNTIME_COVERAGE_GAP,
                (
                    f"bootstrap_task {task.get('id', '(missing id)')!r} declares "
                    "neither repo_id nor bridge_id; exactly one is required"
                ),
            )


def _flatten_parity_ledger_record(item: dict[str, Any]) -> None:
    """Normalize a parity_ledger_item record in place.

    Ensures ``bridge_dependency`` is either ``None`` or a string; the
    contract (shared.md:279-281) forbids bootstrap_task ids here, so a
    cross-reference check runs during validation once the task id set is
    known.
    """
    bridge_dep = item.get("bridge_dependency")
    if bridge_dep is not None and not isinstance(bridge_dep, str):
        item["bridge_dependency"] = str(bridge_dep)
    if "intended_profiles" in item and item["intended_profiles"] is None:
        item["intended_profiles"] = []


def _flatten_ingress_route_record(route: dict[str, Any]) -> None:
    """Normalize ingress_route records in place."""
    if "service_id" not in route and route.get("service"):
        route["service_id"] = route.get("service")
    if "path" in route and route["path"] is None:
        route["path"] = ""
    if "match" in route and route["match"] is None:
        route["match"] = "exact"
    if "listener" in route and route["listener"] is None:
        route["listener"] = "public"


def _post_process_runtime_sections(sections: dict[str, list[dict[str, Any]]]) -> None:
    """Run flatten + XOR validation after every source has been merged in."""
    for env_file in sections["env_files"]:
        _flatten_env_file_record(env_file)
    for service in sections["services"]:
        extracted = _flatten_service_record(service)
        if extracted:
            sections["service_mode_commands"].extend(extracted)
    for task in sections["tasks"]:
        _flatten_bootstrap_task_record(task)
    for item in sections["parity_ledger"]:
        _flatten_parity_ledger_record(item)
    for route in sections["ingress_routes"]:
        _flatten_ingress_route_record(route)
    _validate_bootstrap_task_owner_xor(sections["tasks"])


def _normalize_runtime_sections(
    resolved: dict[str, Any],
    overlay_clients: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    scoped_runtime = "core" in resolved or "clients" in resolved
    _, selection, sections = _collect_core_sections(resolved, scoped_runtime)
    _extend_sections_with_profiles(sections, resolved, scoped_runtime)
    clients_meta = _collect_client_metadata(_resolved_clients(resolved, scoped_runtime, overlay_clients), sections)
    _post_process_runtime_sections(sections)

    return {
        "selection": selection,
        "clients": clients_meta,
        "repos": sections["repos"],
        "artifacts": sections["artifacts"],
        "env_files": sections["env_files"],
        "skills": sections["skills"],
        "tasks": sections["tasks"],
        "services": sections["services"],
        "logs": sections["logs"],
        "checks": sections["checks"],
        "bridges": sections["bridges"],
        "service_mode_commands": sections["service_mode_commands"],
        "ingress_routes": sections["ingress_routes"],
        "parity_ledger": sections["parity_ledger"],
    }


def _base_runtime_model(
    root_dir: Path,
    resolved: dict[str, Any],
    env_values: dict[str, str],
    normalized: dict[str, Any],
) -> dict[str, Any]:
    storage = compile_persistence_summary(root_dir, env_values)
    return {
        "root_dir": str(root_dir),
        "manifest_file": str(runtime_manifest_path(root_dir)),
        "persistence_manifest_file": str(persistence_manifest_path(root_dir)),
        "version": resolved.get("version", 1),
        "env": {key: env_values.get(key, "") for key in MANIFEST_ENV_KEYS},
        "storage": storage,
        "selection": normalized["selection"],
        "clients": normalized["clients"],
        "repos": normalized["repos"],
        "artifacts": normalized["artifacts"],
        "env_files": normalized["env_files"],
        "skills": normalized["skills"],
        "tasks": normalized["tasks"],
        "services": normalized["services"],
        "logs": normalized["logs"],
        "checks": normalized["checks"],
        "bridges": normalized["bridges"],
        "service_mode_commands": normalized.get("service_mode_commands", []),
        "ingress_routes": normalized.get("ingress_routes", []),
        "parity_ledger": normalized.get("parity_ledger", []),
    }


def _populate_repo_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for repo in model["repos"]:
        repo.setdefault("kind", "repo")
        repo.setdefault("required", False)
        repo.setdefault("profiles", [])
        repo.setdefault("client", "")
        repo.setdefault("sync", {})
        repo.setdefault("source", {})
        if repo.get("path") and "host_path" not in repo:
            repo["host_path"] = str(runtime_path_to_host_path(root_dir, model["env"], str(repo["path"]), storage=model.get("storage")))


def _populate_artifact_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for artifact in model["artifacts"]:
        artifact.setdefault("kind", "artifact")
        artifact.setdefault("required", False)
        artifact.setdefault("profiles", [])
        artifact.setdefault("client", "")
        artifact.setdefault("sync", {})
        artifact.setdefault("source", {})
        if artifact.get("path"):
            artifact["host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(artifact["path"]), storage=model.get("storage"))
            )
        source = artifact.get("source") or {}
        if source.get("kind") == "file" and source.get("path"):
            source["host_path"] = str(host_path_to_absolute_path(root_dir, str(source["path"])))
            artifact["source"] = source


def _populate_env_file_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for env_file in model["env_files"]:
        env_file.setdefault("kind", "env-file")
        env_file.setdefault("required", False)
        env_file.setdefault("profiles", [])
        env_file.setdefault("client", "")
        env_file.setdefault("sync", {})
        env_file.setdefault("source", {})
        env_file.setdefault("repo", "")
        env_file.setdefault("mode", "0600")
        if env_file.get("path"):
            env_file["host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(env_file["path"]), storage=model.get("storage"))
            )
        source = env_file.get("source") or {}
        if source.get("kind") == "file" and source.get("path"):
            source["host_path"] = str(host_path_to_absolute_path(root_dir, str(source["path"])))
            env_file["source"] = source


def _populate_skill_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for skill in model["skills"]:
        skill.setdefault("kind", "skill-repo-set")
        skill.setdefault("required", False)
        skill.setdefault("profiles", [])
        skill.setdefault("client", "")
        skill.setdefault("sync", {})
        skill.setdefault("install_targets", [])
        for field in ("bundle_dir", "manifest", "sources_config", "lock_path", "skill_repos_config", "clone_root"):
            if skill.get(field):
                skill[f"{field}_host_path"] = str(
                    runtime_path_to_host_path(root_dir, model["env"], str(skill[field]), storage=model.get("storage"))
                )
        normalized_targets: list[dict[str, Any]] = []
        for target in skill.get("install_targets") or []:
            if not isinstance(target, dict):
                raise RuntimeError("Expected every skills.install_targets item to be a mapping")
            target = dict(target)
            if target.get("path"):
                target["host_path"] = str(
                    runtime_path_to_host_path(root_dir, model["env"], str(target["path"]), storage=model.get("storage"))
                )
            normalized_targets.append(target)
        skill["install_targets"] = normalized_targets


def _populate_task_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for task in model["tasks"]:
        task.setdefault("kind", "task")
        task.setdefault("required", False)
        task.setdefault("profiles", [])
        task.setdefault("client", "")
        task.setdefault("repo", "")
        task.setdefault("log", "")
        task.setdefault("depends_on", [])
        task.setdefault("inputs", [])
        task.setdefault("outputs", [])
        success = task.get("success") or {}
        if success.get("path"):
            success["host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(success["path"]), storage=model.get("storage"))
            )
            task["success"] = success


def _populate_service_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for service in model["services"]:
        service.setdefault("required", False)
        service.setdefault("profiles", [])
        service.setdefault("client", "")
        if service.get("path"):
            service["host_path"] = str(runtime_path_to_host_path(root_dir, model["env"], str(service["path"]), storage=model.get("storage")))
        healthcheck = service.get("healthcheck") or {}
        if healthcheck.get("path"):
            healthcheck["host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(healthcheck["path"]), storage=model.get("storage"))
            )
            service["healthcheck"] = healthcheck


def _populate_log_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for log_item in model["logs"]:
        log_item.setdefault("required", False)
        log_item.setdefault("profiles", [])
        log_item.setdefault("client", "")
        if log_item.get("path"):
            log_item["host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(log_item["path"]), storage=model.get("storage"))
            )


def _populate_bridge_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for bridge in model["bridges"]:
        bridge.setdefault("profiles", [])
        bridge.setdefault("client", "")
        bridge.setdefault("env_tier", "local")
        bridge.setdefault("legacy_targets", [])
        bridge.setdefault("emit_stubs", False)
        if bridge.get("output_root"):
            bridge["output_root_host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(bridge["output_root"]), storage=model.get("storage"))
            )


def _populate_check_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for check in model["checks"]:
        check.setdefault("required", False)
        check.setdefault("profiles", [])
        check.setdefault("client", "")
        if check.get("path"):
            check["host_path"] = str(runtime_path_to_host_path(root_dir, model["env"], str(check["path"]), storage=model.get("storage")))


def _populate_service_mode_command_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for entry in model.get("service_mode_commands") or []:
        entry.setdefault("profiles", [])
        entry.setdefault("client", "")
        entry.setdefault("service_id", "")
        entry.setdefault("mode", "")
        entry.setdefault("command", "")


def _populate_ingress_route_defaults(model: dict[str, Any], root_dir: Path) -> None:
    del root_dir  # reserved for future host-path expansions
    for route in model.get("ingress_routes") or []:
        route.setdefault("profiles", [])
        route.setdefault("client", "")
        route.setdefault("service_id", "")
        route.setdefault("listener", "public")
        route.setdefault("path", "")
        route.setdefault("match", "exact")


def _populate_parity_ledger_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for item in model.get("parity_ledger") or []:
        item.setdefault("profiles", [])
        item.setdefault("intended_profiles", [])
        item.setdefault("client", "")
        item.setdefault("ownership_state", "deferred")
        item.setdefault("action", "build")
        item.setdefault("bridge_dependency", None)
        item.setdefault("request_error", "")
        item.setdefault("notes", "")
        item.setdefault("surface_type", "")
        item.setdefault("legacy_surface", "")


def _populate_client_defaults(model: dict[str, Any], root_dir: Path) -> None:
    for client in model["clients"]:
        client.setdefault("label", client.get("id", ""))
        if client.get("default_cwd"):
            client["default_cwd_host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(client["default_cwd"]), storage=model.get("storage"))
            )


def _populate_runtime_model_defaults(model: dict[str, Any], root_dir: Path) -> None:
    _populate_repo_defaults(model, root_dir)
    _populate_artifact_defaults(model, root_dir)
    _populate_env_file_defaults(model, root_dir)
    _populate_skill_defaults(model, root_dir)
    _populate_task_defaults(model, root_dir)
    _populate_service_defaults(model, root_dir)
    _populate_log_defaults(model, root_dir)
    _populate_check_defaults(model, root_dir)
    _populate_bridge_defaults(model, root_dir)
    _populate_service_mode_command_defaults(model, root_dir)
    _populate_ingress_route_defaults(model, root_dir)
    _populate_parity_ledger_defaults(model, root_dir)
    _populate_client_defaults(model, root_dir)


def build_runtime_model(root_dir: Path) -> dict[str, Any]:
    root_dir = root_dir.resolve()
    runtime_doc = load_yaml(runtime_manifest_path(root_dir))
    env_values = load_runtime_env(root_dir)
    resolved = resolve_placeholders(runtime_doc, env_values)
    overlay_clients = load_client_overlays(root_dir, env_values)
    normalized = _normalize_runtime_sections(resolved, overlay_clients=overlay_clients)
    model = _base_runtime_model(root_dir, resolved, env_values, normalized)
    _populate_runtime_model_defaults(model, root_dir)
    return model
