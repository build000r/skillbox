from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


RUNTIME_ENV_KEYS = [
    "SKILLBOX_NAME",
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
]
MANIFEST_ENV_KEYS = RUNTIME_ENV_KEYS + [
    "SKILLBOX_CLIENTS_HOST_ROOT",
    "SKILLBOX_MONOSERVER_HOST_ROOT",
]

PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def runtime_manifest_path(root_dir: Path) -> Path:
    return root_dir / "workspace" / "runtime.yaml"


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
        "ROOT_DIR": str(root_dir),
    }

    for key, value in derived_defaults.items():
        values.setdefault(key, value)
    values.setdefault(
        "SKILLBOX_CLIENTS_ROOT",
        f"{values['SKILLBOX_WORKSPACE_ROOT']}/workspace/clients",
    )
    values.setdefault("SKILLBOX_CLIENTS_HOST_ROOT", "./workspace/clients")
    values.setdefault("SKILLBOX_MONOSERVER_HOST_ROOT", "..")
    return values


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


def runtime_path_to_host_path(root_dir: Path, env_values: dict[str, str], raw_path: str) -> Path:
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


def _normalize_runtime_sections(
    resolved: dict[str, Any],
    overlay_clients: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    scoped_runtime = "core" in resolved or "clients" in resolved
    core = _normalized_mapping(resolved.get("core"), "core") if scoped_runtime else resolved
    selection = _normalized_mapping(resolved.get("selection"), "selection") if scoped_runtime else {}
    repos = _normalized_items(core.get("repos"), "core.repos" if scoped_runtime else "repos")
    artifacts = _normalized_items(core.get("artifacts"), "core.artifacts" if scoped_runtime else "artifacts")
    env_files = _normalized_items(core.get("env_files"), "core.env_files" if scoped_runtime else "env_files")
    skills = _normalized_items(core.get("skills"), "core.skills" if scoped_runtime else "skills")
    tasks = _normalized_items(core.get("tasks"), "core.tasks" if scoped_runtime else "tasks")
    services = _normalized_items(core.get("services"), "core.services" if scoped_runtime else "services")
    logs = _normalized_items(core.get("logs"), "core.logs" if scoped_runtime else "logs")
    checks = _normalized_items(core.get("checks"), "core.checks" if scoped_runtime else "checks")
    raw_clients = _normalized_items(resolved.get("clients"), "clients") if scoped_runtime else []
    raw_clients.extend(_normalized_items(overlay_clients, "client overlays"))
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
        label = str(client.get("label") or client_id)
        client_default_cwd = client.get("default_cwd")
        client_meta: dict[str, Any] = {
            "id": client_id,
            "label": label,
            "default_cwd": client_default_cwd,
        }
        if client.get("dcg"):
            client_meta["dcg"] = client["dcg"]
        if client.get("context"):
            client_meta["context"] = client["context"]
        clients_meta.append(client_meta)
        repos.extend(
            _normalize_client_repo_roots(
                client.get("repo_roots"),
                client_id=client_id,
                section=f"clients[{client_id}].repo_roots",
            )
        )
        repos.extend(_attach_client_scope(_normalized_items(client.get("repos"), f"clients[{client_id}].repos"), client_id))
        artifacts.extend(
            _attach_client_scope(
                _normalized_items(client.get("artifacts"), f"clients[{client_id}].artifacts"),
                client_id,
            )
        )
        env_files.extend(
            _attach_client_scope(
                _normalized_items(client.get("env_files"), f"clients[{client_id}].env_files"),
                client_id,
            )
        )
        skills.extend(
            _attach_client_scope(_normalized_items(client.get("skills"), f"clients[{client_id}].skills"), client_id)
        )
        tasks.extend(
            _attach_client_scope(_normalized_items(client.get("tasks"), f"clients[{client_id}].tasks"), client_id)
        )
        services.extend(
            _attach_client_scope(
                _normalized_items(client.get("services"), f"clients[{client_id}].services"),
                client_id,
            )
        )
        logs.extend(_attach_client_scope(_normalized_items(client.get("logs"), f"clients[{client_id}].logs"), client_id))
        checks.extend(
            _attach_client_scope(_normalized_items(client.get("checks"), f"clients[{client_id}].checks"), client_id)
        )

    return {
        "selection": selection,
        "clients": clients_meta,
        "repos": repos,
        "artifacts": artifacts,
        "env_files": env_files,
        "skills": skills,
        "tasks": tasks,
        "services": services,
        "logs": logs,
        "checks": checks,
    }


def build_runtime_model(root_dir: Path) -> dict[str, Any]:
    root_dir = root_dir.resolve()
    runtime_doc = load_yaml(runtime_manifest_path(root_dir))
    env_values = load_runtime_env(root_dir)
    resolved = resolve_placeholders(runtime_doc, env_values)
    overlay_clients = load_client_overlays(root_dir, env_values)
    normalized = _normalize_runtime_sections(resolved, overlay_clients=overlay_clients)

    model = {
        "root_dir": str(root_dir),
        "manifest_file": str(runtime_manifest_path(root_dir)),
        "version": resolved.get("version", 1),
        "env": {key: env_values.get(key, "") for key in MANIFEST_ENV_KEYS},
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
    }

    for repo in model["repos"]:
        repo.setdefault("kind", "repo")
        repo.setdefault("required", False)
        repo.setdefault("profiles", [])
        repo.setdefault("client", "")
        repo.setdefault("sync", {})
        repo.setdefault("source", {})
        if repo.get("path") and "host_path" not in repo:
            repo["host_path"] = str(runtime_path_to_host_path(root_dir, model["env"], str(repo["path"])))

    for artifact in model["artifacts"]:
        artifact.setdefault("kind", "artifact")
        artifact.setdefault("required", False)
        artifact.setdefault("profiles", [])
        artifact.setdefault("client", "")
        artifact.setdefault("sync", {})
        artifact.setdefault("source", {})
        if artifact.get("path"):
            artifact["host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(artifact["path"]))
            )
        source = artifact.get("source") or {}
        if source.get("kind") == "file" and source.get("path"):
            source["host_path"] = str(host_path_to_absolute_path(root_dir, str(source["path"])))
            artifact["source"] = source

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
                runtime_path_to_host_path(root_dir, model["env"], str(env_file["path"]))
            )
        source = env_file.get("source") or {}
        if source.get("kind") == "file" and source.get("path"):
            source["host_path"] = str(host_path_to_absolute_path(root_dir, str(source["path"])))
            env_file["source"] = source

    for skill in model["skills"]:
        skill.setdefault("kind", "packaged-skill-set")
        skill.setdefault("required", False)
        skill.setdefault("profiles", [])
        skill.setdefault("client", "")
        skill.setdefault("sync", {})
        skill.setdefault("install_targets", [])
        for field in ("bundle_dir", "manifest", "sources_config", "lock_path"):
            if skill.get(field):
                skill[f"{field}_host_path"] = str(
                    runtime_path_to_host_path(root_dir, model["env"], str(skill[field]))
                )
        normalized_targets: list[dict[str, Any]] = []
        for target in skill.get("install_targets") or []:
            if not isinstance(target, dict):
                raise RuntimeError("Expected every skills.install_targets item to be a mapping")
            target = dict(target)
            if target.get("path"):
                target["host_path"] = str(
                    runtime_path_to_host_path(root_dir, model["env"], str(target["path"]))
                )
            normalized_targets.append(target)
        skill["install_targets"] = normalized_targets

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
                runtime_path_to_host_path(root_dir, model["env"], str(success["path"]))
            )
            task["success"] = success

    for service in model["services"]:
        service.setdefault("required", False)
        service.setdefault("profiles", [])
        service.setdefault("client", "")
        if service.get("path"):
            service["host_path"] = str(runtime_path_to_host_path(root_dir, model["env"], str(service["path"])))
        healthcheck = service.get("healthcheck") or {}
        if healthcheck.get("path"):
            healthcheck["host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(healthcheck["path"]))
            )
            service["healthcheck"] = healthcheck

    for log_item in model["logs"]:
        log_item.setdefault("required", False)
        log_item.setdefault("profiles", [])
        log_item.setdefault("client", "")
        if log_item.get("path"):
            log_item["host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(log_item["path"]))
            )

    for check in model["checks"]:
        check.setdefault("required", False)
        check.setdefault("profiles", [])
        check.setdefault("client", "")
        if check.get("path"):
            check["host_path"] = str(runtime_path_to_host_path(root_dir, model["env"], str(check["path"])))

    for client in model["clients"]:
        client.setdefault("label", client.get("id", ""))
        if client.get("default_cwd"):
            client["default_cwd_host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(client["default_cwd"]))
            )

    return model
