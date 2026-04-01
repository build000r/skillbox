#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

from lib.runtime_model import build_runtime_model, host_path_to_absolute_path


ROOT_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = ROOT_DIR / "workspace"
DEFAULT_SKILL_SYNC_SCRIPT = ROOT_DIR / "scripts" / "03-skill-sync.sh"
DEFAULT_PACKAGER = ROOT_DIR / "scripts" / "package_skill.py"
EXPECTED_FILES = [
    ".env.example",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.monoserver.yml",
    "docker-compose.swimmers.yml",
    ".env-manager/README.md",
    ".env-manager/manage.py",
    "docker/sandbox-entrypoint.sh",
    "scripts/01-bootstrap-do.sh",
    "scripts/02-install-tailscale.sh",
    "scripts/03-skill-sync.sh",
    "scripts/05-swimmers.sh",
    "scripts/package_skill.py",
    "scripts/quick_validate.py",
    "scripts/lib/__init__.py",
    "scripts/lib/runtime_model.py",
    "scripts/lib/skill_bundle_filter.py",
    "workspace/sandbox.yaml",
    "workspace/dependencies.yaml",
    "workspace/runtime.yaml",
    "workspace/default-skills.manifest",
    "workspace/default-skills.sources.yaml",
    "workspace/client-blueprints/git-repo.yaml",
    "workspace/client-blueprints/git-repo-http-service.yaml",
    "workspace/clients/personal/overlay.yaml",
    "workspace/clients/personal/skills.manifest",
    "workspace/clients/personal/skills.sources.yaml",
    "workspace/clients/vibe-coding-client/overlay.yaml",
    "workspace/clients/vibe-coding-client/skills.manifest",
    "workspace/clients/vibe-coding-client/skills.sources.yaml",
]
EXPECTED_DIRECTORIES = [
    ".env-manager",
    "default-skills",
    "default-skills/clients",
    "docker",
    "home/.claude",
    "home/.codex",
    "logs",
    "repos",
    "scripts",
    "skills",
    "skills/clients",
    "workspace",
    "workspace/client-blueprints",
    "workspace/clients",
]


@dataclass
class CheckResult:
    status: str
    code: str
    message: str
    details: dict[str, Any] | None = None


def repo_rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError(
            "Missing PyYAML. Install `python3-yaml` or `pip install pyyaml` to use reconcile commands."
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required file missing: {repo_rel(path)}") from exc
    except Exception as exc:  # pragma: no cover - defensive parse path
        raise RuntimeError(f"Failed to parse {repo_rel(path)}: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Expected a YAML object in {repo_rel(path)}")
    return raw


def load_env_defaults(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"Invalid env line in {repo_rel(path)}: {raw_line}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_manifest_skills(path: Path) -> list[str]:
    skills: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            skills.append(line)
    return skills


def read_bundle_names(path: Path) -> list[str]:
    if not path.exists():
        return []
    return sorted(bundle.stem for bundle in path.glob("*.skill"))


def build_model() -> dict[str, Any]:
    sandbox_doc = load_yaml(WORKSPACE_DIR / "sandbox.yaml")
    dependencies_doc = load_yaml(WORKSPACE_DIR / "dependencies.yaml")
    sources_doc = load_yaml(WORKSPACE_DIR / "default-skills.sources.yaml")
    runtime_model = build_runtime_model(ROOT_DIR)
    env_defaults = load_env_defaults(ROOT_DIR / ".env.example")
    manifest_skills = load_manifest_skills(WORKSPACE_DIR / "default-skills.manifest")

    sandbox = sandbox_doc.get("sandbox") or {}
    runtime = sandbox.get("runtime") or {}
    paths = sandbox.get("paths") or {}
    ports = sandbox.get("ports") or {}
    packaged_skill_bundles = dependencies_doc.get("packaged_skill_bundles") or []
    default_bundle = next(
        (item for item in packaged_skill_bundles if item.get("id") == "default-skills"),
        packaged_skill_bundles[0] if packaged_skill_bundles else {},
    )

    home_root = str(paths.get("claude_root", "")).rsplit("/.claude", 1)[0]
    monoserver_root = str(paths.get("monoserver_root", ""))
    expected_env = {
        "SKILLBOX_NAME": str(sandbox.get("name", "")),
        "SKILLBOX_WORKSPACE_ROOT": str(paths.get("workspace_root", "")),
        "SKILLBOX_REPOS_ROOT": str(paths.get("repos_root", "")),
        "SKILLBOX_SKILLS_ROOT": str(paths.get("skills_root", "")),
        "SKILLBOX_LOG_ROOT": str(paths.get("log_root", "")),
        "SKILLBOX_HOME_ROOT": home_root,
        "SKILLBOX_MONOSERVER_ROOT": monoserver_root,
        "SKILLBOX_CLIENTS_ROOT": f"{paths.get('workspace_root', '')}/workspace/clients",
        "SKILLBOX_CLIENTS_HOST_ROOT": "./workspace/clients",
        "SKILLBOX_API_PORT": str(ports.get("api", "")),
        "SKILLBOX_WEB_PORT": str(ports.get("web", "")),
        "SKILLBOX_SWIMMERS_PORT": str(ports.get("swimmers", "")),
        "SKILLBOX_SWIMMERS_PUBLISH_HOST": "127.0.0.1",
        "SKILLBOX_SWIMMERS_REPO": f"{monoserver_root}/swimmers",
        "SKILLBOX_SWIMMERS_INSTALL_DIR": f"{home_root}/.local/bin",
        "SKILLBOX_SWIMMERS_BIN": f"{home_root}/.local/bin/swimmers",
        "SKILLBOX_SWIMMERS_DOWNLOAD_URL": "",
        "SKILLBOX_SWIMMERS_DOWNLOAD_SHA256": "",
        "SKILLBOX_SWIMMERS_AUTH_MODE": "",
        "SKILLBOX_SWIMMERS_AUTH_TOKEN": "",
        "SKILLBOX_SWIMMERS_OBSERVER_TOKEN": "",
        "SKILLBOX_DCG_BIN": f"{home_root}/.local/bin/dcg",
        "SKILLBOX_DCG_DOWNLOAD_URL": "",
        "SKILLBOX_DCG_DOWNLOAD_SHA256": "",
        "SKILLBOX_DCG_PACKS": "core.git,core.filesystem",
        "SKILLBOX_DCG_MCP_PORT": "3220",
        "SKILLBOX_FWC_BIN": f"{home_root}/.local/bin/fwc",
        "SKILLBOX_FWC_DOWNLOAD_URL": "",
        "SKILLBOX_FWC_DOWNLOAD_SHA256": "",
        "SKILLBOX_FWC_MCP_PORT": "3221",
        "SKILLBOX_FWC_ZONE": "work",
        "SKILLBOX_FWC_CONNECTORS": "github,slack,linear",
        "SKILLBOX_PULSE_INTERVAL": "30",
    }
    runtime_env = {
        key: value
        for key, value in expected_env.items()
        if key not in {"SKILLBOX_NAME", "SKILLBOX_CLIENTS_HOST_ROOT"}
    }
    runtime_env["SKILLBOX_CLIENTS_HOST_ROOT"] = expected_env["SKILLBOX_CLIENTS_ROOT"]
    clients_host_root = host_path_to_absolute_path(
        ROOT_DIR,
        str(
            (runtime_model.get("env") or {}).get("SKILLBOX_CLIENTS_HOST_ROOT")
            or env_defaults.get("SKILLBOX_CLIENTS_HOST_ROOT", "./workspace/clients")
        ),
    )
    base_mounts = [
        {"source": str(ROOT_DIR), "target": paths.get("workspace_root")},
        {"source": str(clients_host_root), "target": expected_env["SKILLBOX_CLIENTS_ROOT"]},
        {"source": str(ROOT_DIR / "home" / ".claude"), "target": paths.get("claude_root")},
        {"source": str(ROOT_DIR / "home" / ".codex"), "target": paths.get("codex_root")},
    ]

    # Per-client overrides replace the fat monoserver mount with individual repo mounts.
    monoserver_layer = _resolve_monoserver_layer()
    if monoserver_layer != "docker-compose.monoserver.yml":
        # Client-focused: read the override file to extract expected volume mounts.
        override_path = ROOT_DIR / monoserver_layer
        try:
            override_doc = yaml.safe_load(override_path.read_text(encoding="utf-8")) or {}
            ws_volumes = (override_doc.get("services", {}).get("workspace", {}).get("volumes") or [])
            for vol_str in ws_volumes:
                parts = str(vol_str).split(":", 1)
                if len(parts) == 2:
                    base_mounts.append({"source": parts[0], "target": parts[1]})
        except (OSError, Exception):
            base_mounts.append({"source": str(ROOT_DIR.parent), "target": paths.get("monoserver_root")})
    else:
        base_mounts.append({"source": str(ROOT_DIR.parent), "target": paths.get("monoserver_root")})

    expected_mounts = base_mounts

    return {
        "root_dir": str(ROOT_DIR),
        "sandbox": {
            "name": sandbox.get("name"),
            "purpose": sandbox.get("purpose"),
            "runtime": runtime,
            "paths": paths,
            "ports": ports,
            "entrypoints": sandbox.get("entrypoints") or [],
        },
        "dependencies": dependencies_doc,
        "env_defaults": env_defaults,
        "expected_env": expected_env,
        "runtime_env": runtime_env,
        "expected_mounts": expected_mounts,
        "skill_sync": {
            "script": str(DEFAULT_SKILL_SYNC_SCRIPT),
            "manifest_file": str(WORKSPACE_DIR / "default-skills.manifest"),
            "sources_file": str(WORKSPACE_DIR / "default-skills.sources.yaml"),
            "output_dir": str(ROOT_DIR / "default-skills"),
            "packager": str(DEFAULT_PACKAGER),
            "sources": sources_doc.get("sources") or [],
            "manifest_skills": manifest_skills,
            "present_bundles": read_bundle_names(ROOT_DIR / "default-skills"),
            "bundle_dependency": default_bundle,
        },
        "runtime_manager": {
            "script": str(ROOT_DIR / ".env-manager" / "manage.py"),
            "manifest_file": runtime_model["manifest_file"],
            "clients": runtime_model.get("clients") or [],
            "repos": runtime_model["repos"],
            "skills": runtime_model["skills"],
            "services": runtime_model["services"],
            "logs": runtime_model["logs"],
            "checks": runtime_model["checks"],
        },
    }


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_monoserver_layer() -> str:
    """Return the compose file path for the monoserver layer."""
    focus_path = ROOT_DIR / "workspace" / ".focus.json"
    if focus_path.is_file():
        try:
            focus = json.loads(focus_path.read_text(encoding="utf-8"))
            client_id = focus.get("client_id", "")
            override = ROOT_DIR / "workspace" / ".compose-overrides" / f"docker-compose.client-{client_id}.yml"
            if client_id and override.is_file():
                return str(override.relative_to(ROOT_DIR))
        except (json.JSONDecodeError, OSError):
            pass
    return "docker-compose.monoserver.yml"


def compose_config(include_surfaces: bool, include_swimmers: bool = False) -> dict[str, Any]:
    if shutil.which("docker") is None:
        raise RuntimeError("`docker` is not installed or not on PATH")

    monoserver_layer = _resolve_monoserver_layer()
    args = ["docker", "compose", "-f", "docker-compose.yml", "-f", monoserver_layer]
    if include_swimmers:
        args.extend(["-f", "docker-compose.swimmers.yml"])
    if include_surfaces:
        args.extend(["--profile", "surfaces"])
    args.extend(["config", "--format", "json"])

    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "docker compose config failed")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse docker compose JSON output: {exc}") from exc


def volume_map(service: dict[str, Any]) -> dict[str, str]:
    volumes = service.get("volumes") or []
    return {item.get("target", ""): item.get("source", "") for item in volumes}


def runtime_env_view(environment: dict[str, Any], expected_runtime_env: dict[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in (environment or {}).items()
        if key in expected_runtime_env
    }


def expected_runtime_paths(model: dict[str, Any]) -> dict[str, str]:
    paths = model["sandbox"]["paths"]
    return {
        "claude-config": str(paths.get("claude_root")),
        "codex-config": str(paths.get("codex_root")),
        "sandbox-root": str(paths.get("repos_root")),
        "monoserver-root": str(paths.get("monoserver_root")),
        "local-skills": str(paths.get("skills_root")),
    }


def check_required_files() -> CheckResult:
    missing = [path for path in EXPECTED_FILES if not (ROOT_DIR / path).is_file()]
    if missing:
        return CheckResult(
            status="fail",
            code="required-files",
            message="required files are missing",
            details={"missing": missing},
        )
    return CheckResult(status="pass", code="required-files", message="required files are present")


def check_expected_directories() -> CheckResult:
    missing = [path for path in EXPECTED_DIRECTORIES if not (ROOT_DIR / path).is_dir()]
    if missing:
        return CheckResult(
            status="fail",
            code="expected-directories",
            message="expected workspace directories are missing",
            details={"missing": missing},
        )
    return CheckResult(status="pass", code="expected-directories", message="expected workspace directories are present")


def check_manifest_alignment(model: dict[str, Any]) -> CheckResult:
    issues: list[str] = []
    dependencies = model["dependencies"]
    paths = model["sandbox"]["paths"]
    dependency_paths = expected_runtime_paths(model)

    home_mounts = {item.get("id"): item.get("path") for item in dependencies.get("home_mounts") or []}
    repo_workspaces = {item.get("id"): item.get("path") for item in dependencies.get("repo_workspaces") or []}
    skill_roots = {item.get("id"): item.get("path") for item in dependencies.get("skill_roots") or []}

    if home_mounts.get("claude-config") != dependency_paths["claude-config"]:
        issues.append("home_mounts.claude-config does not match sandbox.paths.claude_root")
    if home_mounts.get("codex-config") != dependency_paths["codex-config"]:
        issues.append("home_mounts.codex-config does not match sandbox.paths.codex_root")
    if repo_workspaces.get("sandbox-root") != dependency_paths["sandbox-root"]:
        issues.append("repo_workspaces.sandbox-root does not match sandbox.paths.repos_root")
    if repo_workspaces.get("monoserver-root") != dependency_paths["monoserver-root"]:
        issues.append("repo_workspaces.monoserver-root does not match sandbox.paths.monoserver_root")
    if skill_roots.get("local-skills") != dependency_paths["local-skills"]:
        issues.append("skill_roots.local-skills does not match sandbox.paths.skills_root")

    bundle = model["skill_sync"]["bundle_dependency"] or {}
    expected_bundle_path = f"{paths.get('workspace_root')}/default-skills"
    expected_manifest_path = f"{paths.get('workspace_root')}/workspace/default-skills.manifest"
    expected_sources_path = f"{paths.get('workspace_root')}/workspace/default-skills.sources.yaml"
    if bundle.get("path") != expected_bundle_path:
        issues.append("packaged_skill_bundles.default-skills path does not match workspace_root")
    if bundle.get("source_manifest") != expected_manifest_path:
        issues.append("packaged_skill_bundles.default-skills source_manifest does not match workspace_root")
    if bundle.get("sources_config") != expected_sources_path:
        issues.append("packaged_skill_bundles.default-skills sources_config does not match workspace_root")

    if issues:
        return CheckResult(
            status="fail",
            code="manifest-alignment",
            message="manifest files disagree on runtime paths",
            details={"issues": issues},
        )
    return CheckResult(status="pass", code="manifest-alignment", message="manifest files agree on runtime paths")


def check_env_defaults(model: dict[str, Any]) -> CheckResult:
    mismatches: list[str] = []
    env_defaults = model["env_defaults"]
    for key, expected in model["expected_env"].items():
        actual = env_defaults.get(key)
        if actual != expected:
            mismatches.append(f"{key}: expected {expected!r}, found {actual!r}")

    if mismatches:
        return CheckResult(
            status="fail",
            code="env-defaults",
            message=".env.example is out of sync with the manifest",
            details={"mismatches": mismatches},
        )
    return CheckResult(status="pass", code="env-defaults", message=".env.example matches manifest defaults")


def _compose_config_failure(exc: RuntimeError) -> list[CheckResult]:
    return [
        CheckResult(
            status="fail",
            code="compose-config",
            message="docker compose config could not be resolved",
            details={"error": str(exc)},
        )
    ]


def _compose_config_success() -> CheckResult:
    return CheckResult(
        status="pass",
        code="compose-config",
        message="docker compose config resolved for default, surfaces, and swimmers overlay variants",
    )


def _workspace_compose_issues(base_config: dict[str, Any], model: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if base_config.get("name") != model["expected_env"]["SKILLBOX_NAME"]:
        issues.append("compose project name does not match sandbox.name / SKILLBOX_NAME")
    if (base_config.get("x-runtime-env") or {}) != model["runtime_env"]:
        issues.append("x-runtime-env does not match manifest-derived runtime env")

    workspace = (base_config.get("services") or {}).get("workspace") or {}
    if workspace.get("working_dir") != model["sandbox"]["paths"].get("workspace_root"):
        issues.append("workspace working_dir does not match sandbox.paths.workspace_root")
    if workspace.get("tty") is not True:
        issues.append("workspace tty should be enabled")
    if workspace.get("stdin_open") is not True:
        issues.append("workspace stdin_open should be enabled")
    if runtime_env_view(workspace.get("environment") or {}, model["runtime_env"]) != model["runtime_env"]:
        issues.append("workspace environment does not match manifest-derived runtime env")

    mounts = volume_map(workspace)
    for expected_mount in model["expected_mounts"]:
        target = expected_mount["target"]
        source = expected_mount["source"]
        if mounts.get(target) != source:
            issues.append(f"workspace bind mount {source} -> {target} is missing or different")
    return issues


def _surface_compose_issues(surfaces_config: dict[str, Any], model: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for service_name, port_key in (("api", "api"), ("web", "web")):
        service = (surfaces_config.get("services") or {}).get(service_name) or {}
        if service.get("profiles") != ["surfaces"]:
            issues.append(f"{service_name} service is not scoped to the surfaces profile")
        if runtime_env_view(service.get("environment") or {}, model["runtime_env"]) != model["runtime_env"]:
            issues.append(f"{service_name} environment does not match manifest-derived runtime env")

        expected_port = int(model["sandbox"]["ports"].get(port_key))
        ports = service.get("ports") or []
        if len(ports) != 1:
            issues.append(f"{service_name} should publish exactly one port")
            continue

        published = ports[0]
        if published.get("host_ip") != "127.0.0.1":
            issues.append(f"{service_name} should bind only to 127.0.0.1")
        if int(published.get("target", 0)) != expected_port:
            issues.append(f"{service_name} container port does not match sandbox.ports.{port_key}")
        if str(published.get("published")) != str(expected_port):
            issues.append(f"{service_name} published port does not match sandbox.ports.{port_key}")
    return issues


def _swimmers_compose_issues(swimmers_config: dict[str, Any], model: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    swimmers_workspace = (swimmers_config.get("services") or {}).get("workspace") or {}
    if runtime_env_view(swimmers_workspace.get("environment") or {}, model["runtime_env"]) != model["runtime_env"]:
        issues.append("swimmers workspace environment does not match manifest-derived runtime env")

    expected_swimmers_port = int(model["sandbox"]["ports"].get("swimmers"))
    expected_swimmers_host = model["expected_env"]["SKILLBOX_SWIMMERS_PUBLISH_HOST"]
    swimmers_ports = swimmers_workspace.get("ports") or []
    if len(swimmers_ports) != 1:
        issues.append("swimmers workspace overlay should publish exactly one port")
        return issues

    published = swimmers_ports[0]
    if published.get("host_ip") != expected_swimmers_host:
        issues.append("swimmers workspace overlay host_ip does not match SKILLBOX_SWIMMERS_PUBLISH_HOST")
    if int(published.get("target", 0)) != expected_swimmers_port:
        issues.append("swimmers workspace overlay target port does not match sandbox.ports.swimmers")
    if str(published.get("published")) != str(expected_swimmers_port):
        issues.append("swimmers workspace overlay published port does not match sandbox.ports.swimmers")
    return issues


def _compose_check_result(code: str, success_message: str, failure_message: str, issues: list[str]) -> CheckResult:
    if issues:
        return CheckResult(
            status="fail",
            code=code,
            message=failure_message,
            details={"issues": issues},
        )
    return CheckResult(status="pass", code=code, message=success_message)


def check_compose_model(model: dict[str, Any]) -> list[CheckResult]:
    try:
        base_config = compose_config(include_surfaces=False)
        surfaces_config = compose_config(include_surfaces=True)
        swimmers_config = compose_config(include_surfaces=False, include_swimmers=True)
    except RuntimeError as exc:
        return _compose_config_failure(exc)

    return [
        _compose_config_success(),
        _compose_check_result(
            "compose-workspace",
            "workspace service matches manifest-derived env and mounts",
            "workspace service drifted from the manifests",
            _workspace_compose_issues(base_config, model),
        ),
        _compose_check_result(
            "compose-surfaces",
            "api/web services match manifest-derived env, profile, and ports",
            "api/web surface services drifted from the manifests",
            _surface_compose_issues(surfaces_config, model),
        ),
        _compose_check_result(
            "compose-swimmers",
            "workspace swimmers overlay matches manifest-derived env and port publishing",
            "workspace swimmers overlay drifted from the manifests",
            _swimmers_compose_issues(swimmers_config, model),
        ),
    ]


def check_skill_sync_packager(model: dict[str, Any]) -> CheckResult:
    packager = Path(model["skill_sync"]["packager"])
    if not packager.is_file():
        return CheckResult(
            status="fail",
            code="skill-sync-packager",
            message="skill packager is missing",
            details={"expected_path": repo_rel(packager) if packager.is_relative_to(ROOT_DIR) else str(packager)},
        )
    return CheckResult(
        status="pass",
        code="skill-sync-packager",
        message="skill packager is available",
        details={"path": str(packager)},
    )


def check_skill_sync_dry_run(model: dict[str, Any]) -> CheckResult:
    result = run_command(["bash", model["skill_sync"]["script"], "--dry-run"])
    if result.returncode != 0:
        return CheckResult(
            status="fail",
            code="skill-sync-dry-run",
            message="03-skill-sync.sh --dry-run failed",
            details={
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )

    preview = [line for line in result.stdout.splitlines() if line.strip()]
    return CheckResult(
        status="pass",
        code="skill-sync-dry-run",
        message="03-skill-sync.sh can resolve the configured default skills",
        details={"preview": preview[:4]},
    )


def check_bundle_state(model: dict[str, Any]) -> CheckResult:
    expected = set(model["skill_sync"]["manifest_skills"])
    present = set(model["skill_sync"]["present_bundles"])
    missing = sorted(expected - present)
    extra = sorted(present - expected)

    if missing or extra:
        return CheckResult(
            status="warn",
            code="bundle-state",
            message="default-skills contents do not exactly match the manifest",
            details={"missing": missing, "extra": extra},
        )
    return CheckResult(
        status="pass",
        code="bundle-state",
        message="default-skills contents match the manifest",
        details={"bundles": sorted(present)},
    )


def check_reference_drift() -> CheckResult:
    hits: list[str] = []
    ignored_prefixes = {
        ".cache",
        "logs",
    }
    ignored_files = {
        "scripts/04-reconcile.py",
    }

    for path in ROOT_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = repo_rel(path)
        if rel in ignored_files:
            continue
        if any(rel == prefix or rel.startswith(f"{prefix}/") for prefix in ignored_prefixes):
            continue
        if path.suffix == ".skill":
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for index, line in enumerate(lines, start=1):
            if "00-skill-sync.sh" in line:
                hits.append(f"{rel}:{index}")

    if hits:
        return CheckResult(
            status="fail",
            code="reference-drift",
            message="stale references to 00-skill-sync.sh remain in the repo",
            details={"hits": hits},
        )
    return CheckResult(
        status="pass",
        code="reference-drift",
        message="no stale 00-skill-sync.sh references were found",
    )


def check_runtime_manager_model(model: dict[str, Any]) -> CheckResult:
    runtime_manager = model["runtime_manager"]
    return CheckResult(
        status="pass",
        code="runtime-manager-model",
        message="internal runtime manager manifest resolved successfully",
        details={
            "manifest": repo_rel(Path(runtime_manager["manifest_file"])),
            "clients": len(runtime_manager.get("clients") or []),
            "repos": len(runtime_manager["repos"]),
            "skills": len(runtime_manager["skills"]),
            "services": len(runtime_manager["services"]),
            "logs": len(runtime_manager["logs"]),
            "checks": len(runtime_manager["checks"]),
        },
    )


def check_runtime_manager_doctor() -> CheckResult:
    result = run_command(["python3", ".env-manager/manage.py", "doctor", "--format", "json"])
    if result.returncode != 0:
        return CheckResult(
            status="fail",
            code="runtime-manager-doctor",
            message="internal runtime manager doctor failed",
            details={
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return CheckResult(
            status="fail",
            code="runtime-manager-doctor",
            message="internal runtime manager doctor emitted invalid JSON",
            details={"error": str(exc)},
        )

    items = payload.get("checks", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return CheckResult(
            status="fail",
            code="runtime-manager-doctor",
            message="internal runtime manager doctor emitted an unexpected JSON shape",
            details={"payload_type": type(payload).__name__},
        )

    warnings = sum(1 for item in items if item.get("status") == "warn")
    details = {"warnings": warnings}
    if warnings:
        details["warning_codes"] = [item.get("code") for item in items if item.get("status") == "warn"]

    return CheckResult(
        status="pass",
        code="runtime-manager-doctor",
        message="internal runtime manager doctor completed without failures",
        details=details,
    )


def compose_summary(model: dict[str, Any]) -> dict[str, Any]:
    base_config = compose_config(include_surfaces=False)
    surfaces_config = compose_config(include_surfaces=True)
    swimmers_config = compose_config(include_surfaces=False, include_swimmers=True)

    def service_summary(service: dict[str, Any]) -> dict[str, Any]:
        return {
            "working_dir": service.get("working_dir"),
            "environment": service.get("environment") or {},
            "volumes": volume_map(service),
            "ports": service.get("ports") or [],
            "profiles": service.get("profiles") or [],
        }

    services = base_config.get("services") or {}
    surfaces_services = surfaces_config.get("services") or {}

    return {
        "project_name": base_config.get("name"),
        "workspace": service_summary(services.get("workspace") or {}),
        "workspace_swimmers": service_summary((swimmers_config.get("services") or {}).get("workspace") or {}),
        "api": service_summary(surfaces_services.get("api") or {}),
        "web": service_summary(surfaces_services.get("web") or {}),
        "expected_runtime_env": model["runtime_env"],
    }


def build_render_payload(with_compose: bool) -> dict[str, Any]:
    model = build_model()
    payload = {
        "sandbox": model["sandbox"],
        "expected_env": model["expected_env"],
        "expected_mounts": model["expected_mounts"],
        "dependencies": model["dependencies"],
        "skill_sync": model["skill_sync"],
        "runtime_manager": model["runtime_manager"],
    }
    if with_compose:
        payload["compose"] = compose_summary(model)
    return payload


def print_render_text(payload: dict[str, Any]) -> None:
    sandbox = payload["sandbox"]
    print(f"sandbox: {sandbox.get('name')}")
    print(f"purpose: {sandbox.get('purpose')}")
    runtime = sandbox.get("runtime") or {}
    print(f"runtime: {runtime.get('mode')} as {runtime.get('agent_user')}")
    print(f"entrypoints: {', '.join(sandbox.get('entrypoints') or [])}")
    print()
    print("env defaults:")
    for key, value in payload["expected_env"].items():
        print(f"  {key}={value}")
    print()
    print("expected mounts:")
    for mount in payload["expected_mounts"]:
        print(f"  {mount['source']} -> {mount['target']}")
    print()
    print("skill sync:")
    skill_sync = payload["skill_sync"]
    print(f"  script: {repo_rel(Path(skill_sync['script']))}")
    print(f"  manifest: {repo_rel(Path(skill_sync['manifest_file']))}")
    print(f"  sources: {repo_rel(Path(skill_sync['sources_file']))}")
    print(f"  output: {repo_rel(Path(skill_sync['output_dir']))}")
    print(f"  packager: {skill_sync['packager']}")
    print(f"  manifest skills: {', '.join(skill_sync['manifest_skills']) or '(none)'}")
    print(f"  present bundles: {', '.join(skill_sync['present_bundles']) or '(none)'}")
    print()
    print("runtime manager:")
    runtime_manager = payload["runtime_manager"]
    print(f"  script: {repo_rel(Path(runtime_manager['script']))}")
    print(f"  manifest: {repo_rel(Path(runtime_manager['manifest_file']))}")
    print(f"  clients: {len(runtime_manager.get('clients') or [])}")
    print(f"  repos: {len(runtime_manager['repos'])}")
    print(f"  skills: {len(runtime_manager['skills'])}")
    print(f"  services: {len(runtime_manager['services'])}")
    print(f"  logs: {len(runtime_manager['logs'])}")
    print(f"  checks: {len(runtime_manager['checks'])}")
    if "compose" in payload:
        compose = payload["compose"]
        print()
        print("compose:")
        print(f"  project: {compose.get('project_name')}")
        print(f"  workspace working_dir: {compose['workspace'].get('working_dir')}")
        print(f"  swimmers workspace ports: {compose['workspace_swimmers'].get('ports')}")
        print(f"  api ports: {compose['api'].get('ports')}")
        print(f"  web ports: {compose['web'].get('ports')}")


def detail_lines(details: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in details.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            joined = ", ".join(str(item) for item in value)
            lines.append(f"{key}: {joined}")
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


def doctor_results(skip_compose: bool, skip_skill_sync: bool) -> list[CheckResult]:
    model = build_model()
    results = [
        check_required_files(),
        check_expected_directories(),
        check_manifest_alignment(model),
        check_env_defaults(model),
        check_bundle_state(model),
        check_reference_drift(),
        check_runtime_manager_model(model),
        check_runtime_manager_doctor(),
    ]

    if skip_compose:
        results.append(
            CheckResult(
                status="warn",
                code="compose-config",
                message="compose validation skipped",
            )
        )
    else:
        results.extend(check_compose_model(model))

    if skip_skill_sync:
        results.append(
            CheckResult(
                status="warn",
                code="skill-sync-dry-run",
                message="skill-sync dry run skipped",
            )
        )
    else:
        results.append(check_skill_sync_packager(model))
        results.append(check_skill_sync_dry_run(model))

    return results


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Render and validate the skillbox sandbox model.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    render_parser = subparsers.add_parser("render", help="Print the resolved sandbox model.")
    render_parser.add_argument("--format", choices=("text", "json"), default="text")
    render_parser.add_argument(
        "--with-compose",
        action="store_true",
        help="Include resolved docker compose summary information.",
    )

    doctor_parser = subparsers.add_parser("doctor", help="Validate manifest/runtime drift.")
    doctor_parser.add_argument("--format", choices=("text", "json"), default="text")
    doctor_parser.add_argument(
        "--skip-compose",
        action="store_true",
        help="Skip docker compose validation.",
    )
    doctor_parser.add_argument(
        "--skip-skill-sync",
        action="store_true",
        help="Skip the 03-skill-sync.sh dry-run check.",
    )

    args = parser.parse_args()

    if args.command == "render":
        payload = build_render_payload(with_compose=args.with_compose)
        if args.format == "json":
            emit_json(payload)
        else:
            print_render_text(payload)
        return 0

    results = doctor_results(skip_compose=args.skip_compose, skip_skill_sync=args.skip_skill_sync)
    if args.format == "json":
        emit_json([asdict(result) for result in results])
    else:
        print_doctor_text(results)
    return 1 if any(result.status == "fail" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
