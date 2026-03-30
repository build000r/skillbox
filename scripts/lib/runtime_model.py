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
    "SKILLBOX_API_PORT",
    "SKILLBOX_WEB_PORT",
]

PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def runtime_manifest_path(root_dir: Path) -> Path:
    return root_dir / "workspace" / "runtime.yaml"


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
    for key in RUNTIME_ENV_KEYS:
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
        "SKILLBOX_API_PORT": "8000",
        "SKILLBOX_WEB_PORT": "3000",
        "ROOT_DIR": str(root_dir),
    }

    for key, value in derived_defaults.items():
        values.setdefault(key, value)
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

    return path


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


def build_runtime_model(root_dir: Path) -> dict[str, Any]:
    root_dir = root_dir.resolve()
    runtime_doc = load_yaml(runtime_manifest_path(root_dir))
    env_values = load_runtime_env(root_dir)
    resolved = resolve_placeholders(runtime_doc, env_values)

    model = {
        "root_dir": str(root_dir),
        "manifest_file": str(runtime_manifest_path(root_dir)),
        "version": resolved.get("version", 1),
        "env": {key: env_values.get(key, "") for key in RUNTIME_ENV_KEYS},
        "repos": _normalized_items(resolved.get("repos"), "repos"),
        "skills": _normalized_items(resolved.get("skills"), "skills"),
        "services": _normalized_items(resolved.get("services"), "services"),
        "logs": _normalized_items(resolved.get("logs"), "logs"),
        "checks": _normalized_items(resolved.get("checks"), "checks"),
    }

    for repo in model["repos"]:
        repo.setdefault("kind", "repo")
        repo.setdefault("required", False)
        repo.setdefault("profiles", [])
        repo.setdefault("sync", {})
        repo.setdefault("source", {})
        if repo.get("path"):
            repo["host_path"] = str(runtime_path_to_host_path(root_dir, model["env"], str(repo["path"])))

    for skill in model["skills"]:
        skill.setdefault("kind", "packaged-skill-set")
        skill.setdefault("required", False)
        skill.setdefault("profiles", [])
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

    for service in model["services"]:
        service.setdefault("required", False)
        service.setdefault("profiles", [])
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
        if log_item.get("path"):
            log_item["host_path"] = str(
                runtime_path_to_host_path(root_dir, model["env"], str(log_item["path"]))
            )

    for check in model["checks"]:
        check.setdefault("required", False)
        if check.get("path"):
            check["host_path"] = str(runtime_path_to_host_path(root_dir, model["env"], str(check["path"])))

    return model
