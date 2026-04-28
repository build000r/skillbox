#!/usr/bin/env python3
"""Skillbox box lifecycle manager.

Orchestrates DigitalOcean droplets with Tailscale enrollment for
full create → bootstrap → deploy → first-box → drain → destroy lifecycle.

Runs from the operator's machine (not inside the container).
Uses doctl, ssh, and tailscale CLIs — no SDK dependencies.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROFILES_DIR = REPO_ROOT / "workspace" / "box-profiles"
BOOTSTRAP_SCRIPT = SCRIPT_DIR / "01-bootstrap-do.sh"
TAILSCALE_SCRIPT = SCRIPT_DIR / "02-install-tailscale.sh"
UPGRADE_SCRIPT = SCRIPT_DIR / "06-upgrade-release.sh"
INSTALL_SCRIPT = REPO_ROOT / "install.sh"
DEFAULT_BOX_CLIENT_ROOT = "${SKILLBOX_MONOSERVER_ROOT}"
DEFAULT_FIRST_BOX_BLUEPRINT = "git-repo-http-service-bootstrap-spaps-auth"
DEFAULT_ROOT_MCP_CONFIG = {
    "mcpServers": {
        "skillbox": {
            "command": "python3",
            "args": ["/workspace/.env-manager/mcp_server.py"],
        }
    }
}
RESUMABLE_UP_STATES = {"ssh-ready", "deploying", "acceptance", "onboarding"}
SWIMMERS_ENV_PREFIX = "SKILLBOX_SWIMMERS_"


def inventory_path() -> Path:
    override = os.environ.get("SKILLBOX_BOX_INVENTORY", "").strip()
    if override:
        return Path(override)
    return REPO_ROOT / "workspace" / "boxes.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DRIFT = 2

STATES = [
    "creating",
    "bootstrapping",
    "ssh-ready",
    "enrolling",
    "deploying",
    "acceptance",
    "onboarding",
    "ready",
    "draining",
    "destroyed",
]

VALID_TRANSITIONS = {
    "creating": ["bootstrapping", "destroyed"],
    "bootstrapping": ["ssh-ready", "destroyed"],
    "ssh-ready": ["enrolling", "destroyed"],
    "enrolling": ["deploying", "destroyed"],
    "deploying": ["acceptance", "onboarding", "destroyed"],
    "acceptance": ["ready", "destroyed"],
    "onboarding": ["ready", "destroyed"],
    "ready": ["draining", "destroyed"],
    "draining": ["destroyed"],
}

DEFAULT_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]
REMOTE_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHA256_HEX_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
IPV4_PATTERN = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
REGISTER_PROBE_COMMAND = (
    "TS_IP=\"$(tailscale ip -4 2>/dev/null | head -n1 || true)\"; "
    "CONTAINER=no; "
    "if [ -d \"$HOME/skillbox\" ] && "
    "CONTAINER_JSON=\"$(cd \"$HOME/skillbox\" 2>/dev/null && docker compose ps --format json 2>/dev/null | head -1 || true)\" && "
    "printf '%s' \"$CONTAINER_JSON\" | grep -q 'workspace'; then CONTAINER=yes; fi; "
    "printf 'SKILLBOX_PROBE_TAILSCALE_IPV4=%s\\n' \"$TS_IP\"; "
    "printf 'SKILLBOX_PROBE_CONTAINER_RUNNING=%s\\n' \"$CONTAINER\""
)


def shell_join(args: list[str]) -> str:
    return shlex.join([str(arg) for arg in args])


def _validated_remote_env_key(raw_key: str) -> str:
    key = str(raw_key).strip()
    if not REMOTE_ENV_KEY_PATTERN.fullmatch(key):
        raise RuntimeError(f"Invalid remote env var name: {raw_key!r}")
    return key


def build_remote_env_command(argv: list[str], env_vars: dict[str, str] | None = None) -> str:
    if not env_vars:
        return shell_join(argv)

    command = ["env"]
    for raw_key, raw_value in env_vars.items():
        key = _validated_remote_env_key(raw_key)
        command.append(f"{key}={raw_value}")
    command.extend(argv)
    return shell_join(command)


def build_deploy_command(profile: "BoxProfile") -> str:
    return " && ".join([
        "cd",
        shell_join(["git", "clone", "--branch", profile.skillbox_branch, profile.skillbox_repo, "skillbox"]),
        "cd skillbox",
        shell_join(["cp", ".env.example", ".env"]),
        shell_join(["make", "build"]),
        shell_join(["make", "up"]),
    ])


def build_release_install_args(
    client_id: str,
    release: "DeployRelease",
    *,
    remote_archive_path: str,
    repo_dir: str,
    private_path: str,
) -> list[str]:
    return [
        "--offline", remote_archive_path,
        "--sha256", release.archive_sha256,
        "--repo-dir", repo_dir,
        "--private-path", private_path,
        "--client", client_id,
        "--skip-build",
        "--skip-up",
        "--skip-first-box",
        "--no-gum",
    ]


def build_first_box_manage_argv(
    box_id: str,
    *,
    private_path: str,
    active_profiles: list[str],
    blueprint: str | None,
    set_args: list[str],
) -> list[str]:
    effective_blueprint = blueprint or DEFAULT_FIRST_BOX_BLUEPRINT
    argv = [
        "python3",
        ".env-manager/manage.py",
        "first-box",
        box_id,
        "--private-path",
        private_path,
        "--root-path",
        DEFAULT_BOX_CLIENT_ROOT,
        "--default-cwd",
        DEFAULT_BOX_CLIENT_ROOT,
        "--format",
        "json",
    ]
    argv.extend(manage_profile_args(active_profiles))
    argv.extend(["--blueprint", effective_blueprint])
    for set_arg in set_args:
        argv.extend(["--set", set_arg])
    return argv


def _set_arg_map(set_args: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in set_args:
        key, sep, value = raw.partition("=")
        if sep and key:
            values[key] = value
    return values


def blueprint_is_spaps_auth(blueprint: str | None) -> bool:
    if blueprint is None:
        return True
    return Path(blueprint).stem == DEFAULT_FIRST_BOX_BLUEPRINT


def augment_spaps_tailnet_set_args(
    set_args: list[str],
    *,
    blueprint: str | None,
    tailscale_ip: str | None,
) -> list[str]:
    """Add browser-visible SPAPS defaults for remote first-box runs."""
    if not blueprint_is_spaps_auth(blueprint):
        return list(set_args)
    ts_ip = str(tailscale_ip or "").strip()
    if not ts_ip:
        return list(set_args)

    values = _set_arg_map(set_args)
    service_port = values.get("SERVICE_PORT", "5173").strip() or "5173"
    auth_port = values.get("SPAPS_AUTH_PORT", "3301").strip() or "3301"
    defaults = {
        "SPAPS_AUTH_BASE_URL": f"http://{ts_ip}:{service_port}",
        "SPAPS_FIXTURE_BASE_URL": f"http://{ts_ip}:{service_port}",
        "SPAPS_BROWSER_API_URL": f"http://{ts_ip}:{auth_port}",
        "SPAPS_CORS_ALLOW_ORIGINS": (
            f"http://{ts_ip}:{service_port},"
            f"http://localhost:{service_port},"
            f"http://127.0.0.1:{service_port}"
        ),
    }

    augmented = list(set_args)
    for key, value in defaults.items():
        if key not in values:
            augmented.append(f"{key}={value}")
    return augmented


def build_first_box_command(
    box_id: str,
    *,
    repo_dir: str,
    private_path: str,
    active_profiles: list[str],
    blueprint: str | None,
    set_args: list[str],
) -> str:
    return " && ".join([
        shell_join(["cd", repo_dir]),
        shell_join(
            build_first_box_manage_argv(
                box_id,
                private_path=private_path,
                active_profiles=active_profiles,
                blueprint=blueprint,
                set_args=set_args,
            )
        ),
    ])

# ---------------------------------------------------------------------------
# Structured output (same protocol as manage.py)
# ---------------------------------------------------------------------------

def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def structured_error(
    message: str,
    *,
    error_type: str = "runtime_error",
    recoverable: bool = True,
    recovery_hint: str | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "type": error_type,
            "message": message,
            "recoverable": recoverable,
        },
    }
    if recovery_hint is not None:
        payload["error"]["recovery_hint"] = recovery_hint
    if next_actions is not None:
        payload["next_actions"] = next_actions
    return payload


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            f"Add it to .env or export it before running box commands."
        )
    return val


def optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_dotenv(path: Path) -> None:
    """Load a .env file into os.environ (simple key=value, no quoting)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# CLI runners
# ---------------------------------------------------------------------------

def run(args: list[str], *, check: bool = True, capture: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=capture,
        text=True,
        check=check,
        timeout=timeout,
    )


def doctl(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return run(["doctl", *args], timeout=timeout)


def ssh_cmd(user: str, host: str, command: str, *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return run(
        ["ssh", *DEFAULT_SSH_OPTS, f"{user}@{host}", command],
        check=False,
        timeout=timeout,
    )


def ssh_script(
    user: str,
    host: str,
    script_path: Path,
    env_vars: dict[str, str] | None = None,
    *,
    script_args: list[str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Run a local script on a remote host via ssh + stdin."""
    remote_argv = ["bash", "-s"]
    if script_args:
        remote_argv.extend(["--", *script_args])
    remote_cmd = build_remote_env_command(remote_argv, env_vars)
    with script_path.open("r") as f:
        return subprocess.run(
            ["ssh", *DEFAULT_SSH_OPTS, f"{user}@{host}", remote_cmd],
            stdin=f,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )


def extract_tailscale_ipv4(output: str) -> str | None:
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("TAILSCALE_IPV4="):
            continue
        value = line.split("=", 1)[1].strip()
        if value:
            return value
    return None


def is_ipv4_address(candidate: str) -> bool:
    value = str(candidate or "").strip()
    if not IPV4_PATTERN.fullmatch(value):
        return False
    parts = value.split(".")
    return all(0 <= int(part) <= 255 for part in parts)


def is_tailscale_ipv4(candidate: str) -> bool:
    if not is_ipv4_address(candidate):
        return False
    first, second, *_ = [int(part) for part in str(candidate).split(".")]
    return first == 100 and 64 <= second <= 127


def derive_box_id_from_host(host: str) -> str:
    base = str(host or "").strip().lower()
    if not base:
        return "shared-box"
    if not is_ipv4_address(base):
        base = base.split(".", 1)[0]
    base = base.removeprefix("skillbox-")
    base = re.sub(r"[^a-z0-9-]+", "-", base).strip("-")
    return base or "shared-box"


def seed_registered_box_fields(host: str) -> dict[str, str]:
    value = str(host or "").strip()
    if not value:
        return {}
    if is_tailscale_ipv4(value):
        return {"tailscale_ip": value}
    if is_ipv4_address(value):
        return {"droplet_ip": value}
    return {"tailscale_hostname": value}


def parse_register_probe(output: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tailscale_ip": None,
        "container_running": False,
    }
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("SKILLBOX_PROBE_TAILSCALE_IPV4="):
            value = line.split("=", 1)[1].strip()
            if value:
                payload["tailscale_ip"] = value
        elif line.startswith("SKILLBOX_PROBE_CONTAINER_RUNNING="):
            payload["container_running"] = line.split("=", 1)[1].strip().lower() == "yes"
    return payload


def probe_registered_box(box: "Box", *, enabled: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "probe_enabled": enabled,
        "ssh_target": None,
        "ssh_reachable": False,
        "container_running": False,
        "tailscale_ip": box.tailscale_ip,
    }
    if not enabled:
        return payload

    prefer_public = bool(box.droplet_ip and not box.tailscale_ip and not box.tailscale_hostname)
    ssh_target = resolve_box_ssh_target(box, max_wait=5, interval=1, prefer_public=prefer_public)
    if not ssh_target:
        return payload

    payload["ssh_target"] = ssh_target
    payload["ssh_reachable"] = True
    result = ssh_cmd(box.ssh_user, ssh_target, REGISTER_PROBE_COMMAND, timeout=20)
    if result.returncode != 0:
        return payload

    parsed = parse_register_probe(result.stdout)
    payload["container_running"] = bool(parsed["container_running"])
    if parsed["tailscale_ip"]:
        payload["tailscale_ip"] = parsed["tailscale_ip"]
    return payload


def scp_file(local_path: Path, user: str, host: str, remote_path: str, *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return run(
        ["scp", *DEFAULT_SSH_OPTS, str(local_path), f"{user}@{host}:{remote_path}"],
        check=False,
        timeout=timeout,
    )


def wait_for_ssh(host: str, user: str = "root", *, max_wait: int = 120, interval: int = 5) -> bool:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            result = ssh_cmd(user, host, "echo ok", timeout=10)
        except subprocess.TimeoutExpired:
            time.sleep(interval)
            continue
        if result.returncode == 0 and "ok" in result.stdout:
            return True
        time.sleep(interval)
    return False


def box_ssh_candidates(box: "Box", *, prefer_public: bool = False) -> list[str]:
    ordered = [box.droplet_ip, box.tailscale_ip, box.tailscale_hostname] if prefer_public else [
        box.tailscale_ip,
        box.tailscale_hostname,
        box.droplet_ip,
    ]
    candidates: list[str] = []
    for candidate in ordered:
        value = str(candidate or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def resolve_box_ssh_target(
    box: "Box",
    *,
    max_wait: int = 10,
    interval: int = 2,
    prefer_public: bool = False,
) -> str | None:
    for target in box_ssh_candidates(box, prefer_public=prefer_public):
        if wait_for_ssh(target, user=box.ssh_user, max_wait=max_wait, interval=interval):
            return target
    return None


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------

@dataclass
class BoxProfile:
    id: str
    provider: str = "digitalocean"
    region: str = "nyc3"
    size: str = "s-2vcpu-4gb"
    image: str = "ubuntu-24-04-x64"
    ssh_user: str = "skillbox"
    tailscale_hostname_prefix: str = "skillbox"
    skillbox_repo: str = "https://github.com/build000r/skillbox.git"
    skillbox_branch: str = "main"
    storage: "BoxProfileStorage | None" = None


@dataclass
class BoxProfileStorage:
    provider: str
    mount_path: str
    filesystem: str
    required: bool = True
    min_free_gb: float = 0.0


@dataclass
class DeployRelease:
    manifest_path: Path
    client_id: str
    source_commit: str
    payload_tree_sha256: str
    archive_path: Path
    archive_sha256: str
    active_profiles: list[str] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def load_deploy_manifest(manifest_path: Path, *, expected_client_id: str | None = None) -> DeployRelease:
    resolved_manifest = manifest_path.expanduser().resolve()
    if not resolved_manifest.is_file():
        raise RuntimeError(f"Deploy manifest not found: {resolved_manifest}")

    try:
        payload = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Deploy manifest is not valid JSON: {resolved_manifest}") from exc

    client_id = str(payload.get("client_id") or "").strip()
    if not client_id:
        raise RuntimeError(f"Deploy manifest is missing client_id: {resolved_manifest}")
    if expected_client_id is not None and client_id != expected_client_id:
        raise RuntimeError(
            f"Deploy manifest {resolved_manifest} is for client {client_id!r}, not {expected_client_id!r}"
        )

    source_commit = str(payload.get("source_commit") or "").strip()
    if not source_commit:
        raise RuntimeError(f"Deploy manifest is missing source_commit: {resolved_manifest}")

    payload_tree_sha256 = str(payload.get("payload_tree_sha256") or "").strip().lower()
    if not SHA256_HEX_PATTERN.fullmatch(payload_tree_sha256):
        raise RuntimeError(f"Deploy manifest has invalid payload_tree_sha256: {resolved_manifest}")

    archive_rel = str(payload.get("archive") or "").strip()
    if not archive_rel:
        raise RuntimeError(f"Deploy manifest is missing archive path: {resolved_manifest}")
    archive_path = (resolved_manifest.parent / archive_rel).resolve()
    if not archive_path.is_file():
        raise RuntimeError(f"Deploy archive not found: {archive_path}")

    archive_sha256 = str(payload.get("archive_sha256") or "").strip().lower()
    if not SHA256_HEX_PATTERN.fullmatch(archive_sha256):
        raise RuntimeError(f"Deploy manifest has invalid archive_sha256: {resolved_manifest}")
    actual_archive_sha256 = sha256_file(archive_path)
    if actual_archive_sha256 != archive_sha256:
        raise RuntimeError(
            f"Deploy archive hash mismatch for {archive_path}: expected {archive_sha256}, got {actual_archive_sha256}"
        )

    raw_active_profiles = payload.get("active_profiles")
    active_profiles: list[str] = []
    seen_profiles: set[str] = set()
    if raw_active_profiles is not None:
        if not isinstance(raw_active_profiles, list):
            raise RuntimeError(f"Deploy manifest has invalid active_profiles: {resolved_manifest}")
        for raw_profile in raw_active_profiles:
            profile = str(raw_profile).strip()
            if not profile or profile in seen_profiles:
                continue
            seen_profiles.add(profile)
            active_profiles.append(profile)

    return DeployRelease(
        manifest_path=resolved_manifest,
        client_id=client_id,
        source_commit=source_commit,
        payload_tree_sha256=payload_tree_sha256,
        archive_path=archive_path,
        archive_sha256=archive_sha256,
        active_profiles=active_profiles,
    )


def deploy_release_payload(release: DeployRelease) -> dict[str, Any]:
    return {
        "manifest_path": str(release.manifest_path),
        "source_commit": release.source_commit,
        "payload_tree_sha256": release.payload_tree_sha256,
        "archive_path": str(release.archive_path),
        "archive_sha256": release.archive_sha256,
        "active_profiles": release.active_profiles,
    }


def manage_profile_args(active_profiles: list[str]) -> list[str]:
    args: list[str] = []
    for profile in active_profiles:
        if profile == "core":
            continue
        args.extend(["--profile", profile])
    return args


def normalized_env_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip()).strip("_")
    return slug.upper()


def derived_swimmers_auth_token_env(box_id: str) -> str:
    slug = normalized_env_slug(box_id)
    return f"SWIMMERS_{slug}_AUTH_TOKEN" if slug else "SWIMMERS_AUTH_TOKEN"


def local_swimmers_auth_token(box_id: str) -> tuple[str | None, str | None]:
    for env_name in ("SKILLBOX_SWIMMERS_AUTH_TOKEN", derived_swimmers_auth_token_env(box_id)):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value, env_name
    return None, None


def normalize_remote_env_updates(raw_updates: dict[str, Any]) -> dict[str, str]:
    updates: dict[str, str] = {}
    for raw_key, raw_value in (raw_updates or {}).items():
        key = str(raw_key).strip()
        value = str(raw_value)
        if not key:
            continue
        if not REMOTE_ENV_KEY_PATTERN.fullmatch(key):
            raise RuntimeError(f"Invalid env key in remote contract: {key!r}")
        if "\n" in value or "\r" in value:
            raise RuntimeError(f"Invalid multiline env value in remote contract for {key}")
        updates[key] = value
    return updates


def is_loopback_publish_host(value: str | None) -> bool:
    host = str(value or "").strip().lower()
    return host in {"", "localhost", "::1", "0:0:0:0:0:0:0:1"} or host.startswith("127.")


def active_profiles_for_release(release: DeployRelease | None) -> list[str]:
    profiles = release.active_profiles if release is not None else []
    return sorted(dict.fromkeys(["core", *profiles]))


def remote_box_contract_payload(context: "BoxUpContext") -> dict[str, Any]:
    state_root = str(context.box.state_root or (context.profile.storage.mount_path if context.profile.storage else "")).strip()
    storage_filesystem = str(
        context.box.storage_filesystem
        or (context.profile.storage.filesystem if context.profile.storage else "")
    ).strip()
    storage_min_free_gb = context.box.storage_min_free_gb
    if storage_min_free_gb is None and context.profile.storage is not None:
        storage_min_free_gb = context.profile.storage.min_free_gb

    env_updates: dict[str, str] = {}
    if context.profile.storage is not None:
        env_updates.update({
            "SKILLBOX_STORAGE_PROVIDER": context.box.storage_provider or context.profile.storage.provider,
            "SKILLBOX_STORAGE_FILESYSTEM": storage_filesystem,
            "SKILLBOX_STORAGE_REQUIRED": "true",
            "SKILLBOX_STORAGE_MIN_FREE_GB": str(storage_min_free_gb or 0),
        })
    if state_root:
        env_updates.update({
            "SKILLBOX_STATE_ROOT": state_root,
            "SKILLBOX_CLIENTS_HOST_ROOT": f"{state_root.rstrip('/')}/clients",
            "SKILLBOX_MONOSERVER_HOST_ROOT": f"{state_root.rstrip('/')}/monoserver",
        })

    active_profiles = active_profiles_for_release(context.deploy_release)
    has_swimmers_profile = "swimmers" in active_profiles
    token, token_source = local_swimmers_auth_token(context.box_id)
    for key, value in os.environ.items():
        if key.startswith(SWIMMERS_ENV_PREFIX) and value.strip():
            env_updates[key] = value.strip()
    if has_swimmers_profile:
        publish_host = env_updates.get("SKILLBOX_SWIMMERS_PUBLISH_HOST")
        if is_loopback_publish_host(publish_host):
            env_updates["SKILLBOX_SWIMMERS_PUBLISH_HOST"] = "0.0.0.0"
    if token:
        env_updates["SKILLBOX_SWIMMERS_AUTH_TOKEN"] = token
        env_updates.setdefault("SKILLBOX_SWIMMERS_AUTH_MODE", "token")

    return {
        "env_updates": env_updates,
        "mcp_config": DEFAULT_ROOT_MCP_CONFIG,
        "active_profiles": active_profiles,
        "swimmers_auth_token_env": token_source,
    }


def build_remote_contract_command(payload: dict[str, Any], *, repo_dir: str) -> str:
    payload = dict(payload)
    payload["env_updates"] = normalize_remote_env_updates(payload.get("env_updates") or {})
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode("utf-8")).decode("ascii")
    script = f"""python3 - <<'PY'
import base64
import json
import re
from pathlib import Path

payload = json.loads(base64.b64decode({encoded!r}).decode("utf-8"))
repo = Path({repo_dir!r}).expanduser()
env_path = repo / ".env"
example_path = repo / ".env.example"
if not env_path.exists():
    if example_path.exists():
        env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        env_path.write_text("", encoding="utf-8")

key_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
updates = {{}}
for raw_key, raw_value in (payload.get("env_updates") or {{}}).items():
    key = str(raw_key).strip()
    value = str(raw_value)
    if not key:
        continue
    if not key_pattern.fullmatch(key):
        raise SystemExit(f"Invalid env key in remote contract: {{key!r}}")
    if "\\n" in value or "\\r" in value:
        raise SystemExit(f"Invalid multiline env value in remote contract for {{key}}")
    updates[key] = value
lines = env_path.read_text(encoding="utf-8").splitlines()
rendered = []
seen = set()
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        rendered.append(line)
        continue
    key, _, _value = line.partition("=")
    key = key.strip()
    if key in updates:
        rendered.append(f"{{key}}={{updates[key]}}")
        seen.add(key)
    else:
        rendered.append(line)
for key in sorted(updates):
    if key not in seen:
        rendered.append(f"{{key}}={{updates[key]}}")
env_path.write_text("\\n".join(rendered).rstrip() + "\\n", encoding="utf-8")

mcp_path = repo / ".mcp.json"
mcp_status = "kept"
if not mcp_path.exists():
    mcp_path.write_text(json.dumps(payload["mcp_config"], indent=2, sort_keys=True) + "\\n", encoding="utf-8")
    mcp_status = "created"

print(json.dumps({{
    "env_updates": sorted(updates),
    "mcp_config": mcp_status,
}}))
PY"""
    return script


def remote_workspace_launch_targets(active_profiles: list[str]) -> list[str]:
    targets = ["build", "up"]
    if "swimmers" in set(active_profiles):
        targets.append("swimmers-start")
    return targets


def build_remote_workspace_launch_command(active_profiles: list[str], *, repo_dir: str) -> str:
    return " && ".join(
        [
            shell_join(["cd", repo_dir]),
            *[
                shell_join(["make", target])
                for target in remote_workspace_launch_targets(active_profiles)
            ],
        ]
    )


def storage_payload(storage: BoxProfileStorage | None) -> dict[str, Any] | None:
    if storage is None:
        return None
    return asdict(storage)


def volume_name_for_box(box_id: str) -> str:
    return f"skillbox-state-{box_id}"


def volume_filesystem_label(name: str, filesystem: str) -> str:
    # Keep the DO volume name descriptive, but shorten the filesystem label to
    # fit mkfs/ext4/xfs limits so volume creation does not fail server-side.
    max_len = 12 if filesystem == "xfs" else 16
    candidate = str(name).strip()
    if candidate.startswith("skillbox-state-"):
        candidate = "skillbox-" + candidate.removeprefix("skillbox-state-")
    candidate = re.sub(r"[^A-Za-z0-9_-]+", "-", candidate).strip("-_")
    if not candidate:
        candidate = "skillbox"
    if len(candidate) <= max_len:
        return candidate

    suffix = ""
    parts = [part for part in candidate.split("-") if part]
    if parts:
        suffix = parts[-1]
    if suffix:
        suffix = suffix[-(max_len - 2):]
        prefix_len = max_len - len(suffix) - 1
        if prefix_len > 0:
            shortened = f"{candidate[:prefix_len]}-{suffix}"
            shortened = shortened[:max_len].strip("-_")
            if shortened:
                return shortened

    shortened = candidate[:max_len].strip("-_")
    return shortened or "skillbox"[:max_len]


def storage_volume_size_gb(storage: BoxProfileStorage) -> int:
    return max(20, int(math.ceil(storage.min_free_gb or 0.0)))


def parse_box_profile_storage(
    *,
    profile_id: str,
    profile_provider: str,
    raw_storage: Any,
) -> BoxProfileStorage | None:
    if raw_storage is None:
        return None
    if not isinstance(raw_storage, dict):
        raise RuntimeError(f"Expected a YAML mapping at storage in box profile {profile_id!r}")

    storage_provider = str(raw_storage.get("provider") or profile_provider).strip() or profile_provider
    if storage_provider != profile_provider:
        raise RuntimeError(
            f"Box profile {profile_id!r} storage.provider {storage_provider!r} does not match provider {profile_provider!r}"
        )

    mount_path = str(raw_storage.get("mount_path") or "").strip()
    if not mount_path:
        raise RuntimeError(f"Box profile {profile_id!r} storage.mount_path is required")

    filesystem = str(raw_storage.get("filesystem") or "").strip()
    if not filesystem:
        raise RuntimeError(f"Box profile {profile_id!r} storage.filesystem is required")

    min_free_raw = raw_storage.get("min_free_gb", 0)
    try:
        min_free_gb = float(min_free_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Box profile {profile_id!r} storage.min_free_gb must be numeric") from exc
    if min_free_gb < 0:
        raise RuntimeError(f"Box profile {profile_id!r} storage.min_free_gb cannot be negative")

    return BoxProfileStorage(
        provider=storage_provider,
        mount_path=mount_path,
        filesystem=filesystem,
        required=bool(raw_storage.get("required", True)),
        min_free_gb=min_free_gb,
    )


def build_release_upgrade_args(
    client_id: str,
    release: "DeployRelease",
    *,
    remote_archive_path: str,
    repo_dir: str,
) -> list[str]:
    args = [
        "--archive", remote_archive_path,
        "--sha256", release.archive_sha256,
        "--repo-dir", repo_dir,
        "--client", client_id,
    ]
    args.extend(manage_profile_args(release.active_profiles))
    return args


def load_profile(name: str) -> BoxProfile:
    try:
        import yaml as yaml_mod
    except ModuleNotFoundError:
        yaml_mod = None

    path = PROFILES_DIR / f"{name}.yaml"
    if not path.is_file():
        # Try without extension
        path = PROFILES_DIR / name
        if not path.is_file():
            available = [p.stem for p in PROFILES_DIR.glob("*.yaml")] if PROFILES_DIR.is_dir() else []
            raise RuntimeError(
                f"Box profile {name!r} not found. Available: {', '.join(available) or '(none)'}"
            )

    if yaml_mod is None:
        raise RuntimeError("PyYAML is required to load box profiles: pip install pyyaml")

    data = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a YAML mapping in {path}")

    provider = str(data.get("provider", "digitalocean"))
    storage = parse_box_profile_storage(
        profile_id=name,
        profile_provider=provider,
        raw_storage=data.get("storage"),
    )

    return BoxProfile(
        id=name,
        provider=provider,
        region=data.get("region", "nyc3"),
        size=data.get("size", "s-2vcpu-4gb"),
        image=data.get("image", "ubuntu-24-04-x64"),
        ssh_user=data.get("ssh_user", "skillbox"),
        tailscale_hostname_prefix=data.get("tailscale_hostname_prefix", "skillbox"),
        skillbox_repo=data.get("skillbox_repo", "https://github.com/build000r/skillbox.git"),
        skillbox_branch=data.get("skillbox_branch", "main"),
        storage=storage,
    )


def list_profiles() -> list[BoxProfile]:
    if not PROFILES_DIR.is_dir():
        return []
    profiles = []
    for path in sorted(PROFILES_DIR.glob("*.yaml")):
        try:
            profiles.append(load_profile(path.stem))
        except RuntimeError:
            pass
    return profiles


# ---------------------------------------------------------------------------
# Inventory (boxes.json)
# ---------------------------------------------------------------------------

@dataclass
class Box:
    id: str
    profile: str
    state: str = "creating"
    management_mode: str = "managed"
    droplet_id: str | None = None
    droplet_ip: str | None = None
    tailscale_hostname: str | None = None
    tailscale_ip: str | None = None
    ssh_user: str = "skillbox"
    created_at: str = ""
    updated_at: str = ""
    region: str = ""
    size: str = ""
    storage_provider: str | None = None
    state_root: str | None = None
    storage_filesystem: str | None = None
    storage_required: bool = False
    storage_min_free_gb: float | None = None
    volume_id: str | None = None
    volume_name: str | None = None
    volume_size_gb: int | None = None


def load_inventory() -> list[Box]:
    path = inventory_path()
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    boxes = []
    for item in data.get("boxes", []):
        boxes.append(Box(**{k: v for k, v in item.items() if k in Box.__dataclass_fields__}))
    return boxes


def save_inventory(boxes: list[Box]) -> None:
    path = inventory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"boxes": [asdict(b) for b in boxes]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def find_box(boxes: list[Box], box_id: str) -> Box | None:
    for b in boxes:
        if b.id == box_id:
            return b
    return None


def update_box(box: Box, **kwargs: Any) -> None:
    for k, v in kwargs.items():
        if hasattr(box, k):
            setattr(box, k, v)
    box.updated_at = datetime.now(timezone.utc).isoformat()


def volume_payload(box: Box) -> dict[str, Any] | None:
    if not box.volume_name and not box.volume_id:
        return None
    return {
        "id": box.volume_id,
        "name": box.volume_name,
        "size_gb": box.volume_size_gb,
    }


def registration_payload(box: Box, probe: dict[str, Any], *, host: str) -> dict[str, Any]:
    next_actions = [f"box status {box.id}"]
    if probe.get("ssh_reachable"):
        next_actions.append(f"box ssh {box.id}")
    elif box.management_mode == "external":
        next_actions.append(f"box unregister {box.id}")

    return {
        "box_id": box.id,
        "host": host,
        "registered": True,
        "management_mode": box.management_mode,
        "state": box.state,
        "profile": box.profile,
        "droplet_id": box.droplet_id,
        "droplet_ip": box.droplet_ip,
        "tailscale_hostname": box.tailscale_hostname,
        "tailscale_ip": box.tailscale_ip,
        "ssh_user": box.ssh_user,
        "region": box.region,
        "size": box.size,
        "state_root": box.state_root,
        "storage_filesystem": box.storage_filesystem,
        "volume_name": box.volume_name,
        "volume_size_gb": box.volume_size_gb,
        "ssh_target": probe.get("ssh_target"),
        "ssh_reachable": bool(probe.get("ssh_reachable")),
        "container_running": bool(probe.get("container_running")),
        "probe_enabled": bool(probe.get("probe_enabled")),
        "next_actions": next_actions,
    }


# ---------------------------------------------------------------------------
# DigitalOcean operations
# ---------------------------------------------------------------------------

def do_create_droplet(
    name: str,
    *,
    region: str,
    size: str,
    image: str,
    ssh_key_id: str,
) -> dict[str, Any]:
    result = doctl(
        "compute", "droplet", "create", name,
        "--region", region,
        "--size", size,
        "--image", image,
        "--ssh-keys", ssh_key_id,
        "--wait",
        "--output", "json",
        timeout=300,
    )
    droplets = json.loads(result.stdout)
    if not droplets:
        raise RuntimeError(f"doctl returned empty result when creating droplet {name}")
    return droplets[0]


def do_get_droplet(droplet_id: str) -> dict[str, Any] | None:
    result = run(
        ["doctl", "compute", "droplet", "get", droplet_id, "--output", "json"],
        check=False,
    )
    if result.returncode != 0:
        return None
    droplets = json.loads(result.stdout)
    return droplets[0] if droplets else None


def do_delete_droplet(droplet_id: str) -> bool:
    result = run(
        ["doctl", "compute", "droplet", "delete", droplet_id, "--force"],
        check=False,
    )
    return result.returncode == 0


def do_droplet_public_ip(droplet: dict[str, Any]) -> str | None:
    for net in droplet.get("networks", {}).get("v4", []):
        if net.get("type") == "public":
            return net.get("ip_address")
    return None


def _volume_droplet_ids(volume: dict[str, Any]) -> list[str]:
    raw_ids = volume.get("droplet_ids")
    if raw_ids is None:
        raw_ids = volume.get("dropletIds")
    if not isinstance(raw_ids, list):
        return []
    return [str(item) for item in raw_ids if str(item).strip()]


def _volume_size_gb(volume: dict[str, Any], fallback: int) -> int:
    raw = volume.get("size_gigabytes")
    if raw is None:
        raw = volume.get("sizeGigaBytes")
    if raw is None:
        raw = volume.get("size")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def do_list_volumes(*, region: str | None = None) -> list[dict[str, Any]]:
    args = ["compute", "volume", "list"]
    if region:
        args.extend(["--region", region])
    args.extend(["--output", "json"])
    result = doctl(*args, timeout=120)
    payload = json.loads(result.stdout)
    if not isinstance(payload, list):
        raise RuntimeError("doctl returned an unexpected volume list payload")
    return payload


def do_get_volume(volume_id: str) -> dict[str, Any]:
    result = doctl("compute", "volume", "get", volume_id, "--output", "json", timeout=120)
    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"doctl returned an empty result for volume {volume_id}")
    return payload[0]


def do_find_volume_by_name(name: str, *, region: str) -> dict[str, Any] | None:
    matches = [volume for volume in do_list_volumes(region=region) if str(volume.get("name") or "") == name]
    if not matches:
        return None
    if len(matches) > 1:
        raise RuntimeError(f"Multiple DigitalOcean volumes named {name!r} exist in region {region!r}")
    return matches[0]


def do_create_volume(
    name: str,
    *,
    region: str,
    size_gb: int,
    filesystem: str,
    description: str = "",
) -> dict[str, Any]:
    args = [
        "compute", "volume", "create", name,
        "--region", region,
        "--size", f"{size_gb}GiB",
        "--fs-type", filesystem,
        "--fs-label", volume_filesystem_label(name, filesystem),
        "--output", "json",
    ]
    if description:
        args.extend(["--desc", description])
    result = doctl(*args, timeout=300)
    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"doctl returned an empty result when creating volume {name}")
    return payload[0]


def do_attach_volume(volume_id: str, droplet_id: str) -> None:
    doctl(
        "compute", "volume-action", "attach", volume_id, droplet_id,
        "--wait",
        "--output", "json",
        timeout=300,
    )


# ---------------------------------------------------------------------------
# Tailscale operations
# ---------------------------------------------------------------------------

def ts_remove_node(hostname: str) -> bool:
    """Remove a node from the tailnet by hostname via doctl-style CLI."""
    del hostname
    # Try tailscale CLI first (admin removal requires API, but we try)
    result = run(
        ["tailscale", "logout"],
        check=False,
    )
    # For proper removal, we SSH into the box and run tailscale logout there
    return result.returncode == 0


# ---------------------------------------------------------------------------
# box up
# ---------------------------------------------------------------------------

@dataclass
class BoxUpContext:
    box_id: str
    profile_name: str
    profile: BoxProfile
    box: Box
    boxes: list[Box]
    ts_hostname: str
    is_json: bool
    deploy_release: DeployRelease | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    ip: str | None = None
    ssh_target: str | None = None


def require_profile_storage(profile: BoxProfile) -> BoxProfileStorage:
    if profile.provider != "digitalocean":
        raise RuntimeError(
            f"Unsupported box provider {profile.provider!r}. box.py currently provisions DigitalOcean droplets only."
        )

    storage = profile.storage
    if storage is None:
        raise RuntimeError(
            f"DigitalOcean profile {profile.id!r} is missing a storage stanza. "
            "Declare storage.mount_path, storage.filesystem, storage.required, and storage.min_free_gb."
        )
    if not storage.required:
        raise RuntimeError(
            f"DigitalOcean profile {profile.id!r} must declare storage.required=true for durable state."
        )
    return storage


def _record_box_up_step(context: BoxUpContext, name: str, status: str, detail: Any = None) -> None:
    entry: dict[str, Any] = {"step": name, "status": status}
    if detail is not None:
        entry["detail"] = detail
    context.steps.append(entry)
    if not context.is_json:
        marker = "ok" if status == "ok" else ("skip" if status == "skip" else "FAIL")
        suffix = f"  {detail}" if detail and isinstance(detail, str) else ""
        print(f"[{marker}] {name}{suffix}")


def _emit_box_up_failure(
    context: BoxUpContext,
    *,
    error_type: str,
    message: str,
    next_actions: list[str] | None = None,
) -> int:
    payload: dict[str, Any] = {
        "box_id": context.box_id,
        "dry_run": False,
        "steps": context.steps,
    }
    payload.update(structured_error(message, error_type=error_type, next_actions=next_actions))
    if context.is_json:
        emit_json(payload)
    return EXIT_ERROR


def _run_box_up_stage(
    context: BoxUpContext,
    *,
    stage_name: str,
    error_type: str,
    action: Any,
    failure_state: str | None = None,
    next_actions: list[str] | None = None,
) -> bool:
    try:
        detail = action()
    except Exception as exc:
        _record_box_up_step(context, stage_name, "fail", str(exc))
        if failure_state is not None:
            update_box(context.box, state=failure_state)
            save_inventory(context.boxes)
        _emit_box_up_failure(
            context,
            error_type=error_type,
            message=str(exc),
            next_actions=next_actions,
        )
        return False

    _record_box_up_step(context, stage_name, "ok", detail)
    return True


def _build_box_up_context(
    *,
    box_id: str,
    profile_name: str,
    profile: BoxProfile,
    boxes: list[Box],
    is_json: bool,
    deploy_release: DeployRelease | None = None,
) -> BoxUpContext:
    now = datetime.now(timezone.utc).isoformat()
    ts_hostname = f"{profile.tailscale_hostname_prefix}-{box_id}"
    storage = profile.storage
    box = Box(
        id=box_id,
        profile=profile_name,
        state="creating",
        ssh_user=profile.ssh_user,
        tailscale_hostname=ts_hostname,
        created_at=now,
        updated_at=now,
        region=profile.region,
        size=profile.size,
        storage_provider=storage.provider if storage is not None else None,
        state_root=storage.mount_path if storage is not None else None,
        storage_filesystem=storage.filesystem if storage is not None else None,
        storage_required=storage.required if storage is not None else False,
        storage_min_free_gb=storage.min_free_gb if storage is not None else None,
        volume_name=volume_name_for_box(box_id) if storage is not None else None,
        volume_size_gb=storage_volume_size_gb(storage) if storage is not None else None,
    )
    return BoxUpContext(
        box_id=box_id,
        profile_name=profile_name,
        profile=profile,
        box=box,
        boxes=boxes,
        ts_hostname=ts_hostname,
        is_json=is_json,
        deploy_release=deploy_release,
    )


def _build_box_resume_context(
    *,
    existing: Box,
    profile: BoxProfile,
    boxes: list[Box],
    is_json: bool,
    deploy_release: DeployRelease | None,
) -> BoxUpContext:
    context = BoxUpContext(
        box_id=existing.id,
        profile_name=existing.profile,
        profile=profile,
        box=existing,
        boxes=boxes,
        ts_hostname=existing.tailscale_hostname or f"{profile.tailscale_hostname_prefix}-{existing.id}",
        is_json=is_json,
        deploy_release=deploy_release,
    )
    context.ip = existing.droplet_ip
    return context


def _box_up_dry_run_payload(context: BoxUpContext) -> dict[str, Any]:
    _record_box_up_step(context, "create", "skip", f"would create {context.profile.size} in {context.profile.region}")
    if context.profile.storage is not None:
        _record_box_up_step(
            context,
            "storage",
            "skip",
            f"would attach {context.box.volume_name} at {context.profile.storage.mount_path}",
        )
    _record_box_up_step(context, "bootstrap", "skip", "dry-run")
    _record_box_up_step(context, "ssh-ready", "skip", f"would verify ssh {context.profile.ssh_user}@<public-ip>")
    _record_box_up_step(context, "enroll", "skip", f"would enroll as {context.ts_hostname}")
    _record_box_up_step(context, "deploy", "skip", "dry-run")
    _record_box_up_step(context, "contract", "skip", "dry-run")
    _record_box_up_step(context, "launch", "skip", "dry-run")
    _record_box_up_step(context, "first-box", "skip", "dry-run")
    _record_box_up_step(context, "verify", "skip", "dry-run")
    payload = {
        "box_id": context.box_id,
        "profile": asdict(context.profile),
        "dry_run": True,
        "steps": context.steps,
        "storage": storage_payload(context.profile.storage),
        "volume": volume_payload(context.box),
        "next_actions": [f"box up {context.box_id} --profile {context.profile_name} --deploy-manifest <path>"],
    }
    if context.deploy_release is not None:
        payload["deploy_release"] = deploy_release_payload(context.deploy_release)
    return payload


def _create_box_droplet(context: BoxUpContext, *, ssh_key_id: str) -> str:
    droplet_name = f"skillbox-{context.box_id}"
    if not context.is_json:
        print(f"[...] create  Creating {context.profile.size} droplet in {context.profile.region}...")
    droplet = do_create_droplet(
        droplet_name,
        region=context.profile.region,
        size=context.profile.size,
        image=context.profile.image,
        ssh_key_id=ssh_key_id,
    )
    ip = do_droplet_public_ip(droplet)
    if not ip:
        raise RuntimeError("Droplet created but no public IP assigned")
    context.ip = ip
    update_box(context.box, droplet_id=str(droplet["id"]), droplet_ip=ip, state="bootstrapping")
    context.boxes.append(context.box)
    save_inventory(context.boxes)
    return f"droplet {droplet['id']} at {ip}"


def _ensure_box_storage(context: BoxUpContext) -> str:
    storage = require_profile_storage(context.profile)
    droplet_id = str(context.box.droplet_id or "").strip()
    if not droplet_id:
        raise RuntimeError("Droplet ID unavailable during storage provisioning")

    volume_name = context.box.volume_name or volume_name_for_box(context.box_id)
    size_gb = context.box.volume_size_gb or storage_volume_size_gb(storage)
    volume = do_find_volume_by_name(volume_name, region=context.profile.region)
    created = False
    if volume is None:
        volume = do_create_volume(
            volume_name,
            region=context.profile.region,
            size_gb=size_gb,
            filesystem=storage.filesystem,
            description=f"Skillbox durable state for {context.box_id}",
        )
        created = True

    volume_id = str(volume.get("id") or "").strip()
    if not volume_id:
        raise RuntimeError(f"DigitalOcean volume {volume_name!r} has no ID")

    attached_ids = _volume_droplet_ids(volume)
    if attached_ids and droplet_id not in attached_ids:
        raise RuntimeError(
            f"Volume {volume_name!r} is already attached to droplet(s) {', '.join(attached_ids)}. "
            f"Detach it before reusing box {context.box_id!r}."
        )
    if droplet_id not in attached_ids:
        do_attach_volume(volume_id, droplet_id)
        volume = do_get_volume(volume_id)

    update_box(
        context.box,
        volume_id=volume_id,
        volume_name=volume_name,
        volume_size_gb=_volume_size_gb(volume, size_gb),
        state="bootstrapping",
    )
    save_inventory(context.boxes)
    action = "created+attached" if created else "attached"
    return f"{action} volume {volume_name} ({context.box.volume_size_gb}GiB) at {storage.mount_path}"


def _bootstrap_box_host(context: BoxUpContext) -> str:
    if context.ip is None:
        raise RuntimeError("Droplet IP unavailable during bootstrap")
    if not context.is_json:
        print(f"[...] bootstrap  Waiting for SSH on {context.ip}...")
    if not wait_for_ssh(context.ip, user="root"):
        raise RuntimeError(f"SSH not reachable at root@{context.ip} after 120s")
    if not context.is_json:
        print("[...] bootstrap  Running 01-bootstrap-do.sh...")
    storage = require_profile_storage(context.profile)
    env_vars = {
        "APP_USER": context.profile.ssh_user,
        "SKILLBOX_STATE_ROOT": storage.mount_path,
        "SKILLBOX_STORAGE_FILESYSTEM": storage.filesystem,
        "SKILLBOX_STORAGE_MIN_FREE_GB": str(storage.min_free_gb),
        "SKILLBOX_VOLUME_NAME": context.box.volume_name or volume_name_for_box(context.box_id),
    }
    result = ssh_script("root", context.ip, BOOTSTRAP_SCRIPT, env_vars, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Bootstrap failed (exit {result.returncode}): {result.stderr[-500:]}")
    return f"OS packages + Docker + state root {storage.mount_path} mounted"


def _mark_box_ssh_ready(context: BoxUpContext) -> str:
    public_ip = str(context.box.droplet_ip or "").strip()
    if not public_ip:
        raise RuntimeError("Droplet public IP unavailable while checking skillbox SSH access")
    if not wait_for_ssh(public_ip, user=context.profile.ssh_user, max_wait=30, interval=3):
        raise RuntimeError(f"SSH not reachable at {context.profile.ssh_user}@{public_ip} after bootstrap")
    context.ssh_target = public_ip
    update_box(context.box, state="ssh-ready")
    save_inventory(context.boxes)
    return f"ssh {context.profile.ssh_user}@{public_ip}"


def _enroll_box_tailscale(context: BoxUpContext, *, ts_authkey: str) -> str:
    if context.ip is None:
        raise RuntimeError("Droplet IP unavailable during tailscale enrollment")
    if not context.is_json:
        print(f"[...] enroll  Joining tailnet as {context.ts_hostname}...")
    update_box(context.box, state="enrolling")
    save_inventory(context.boxes)
    result = ssh_script(
        "root",
        context.ip,
        TAILSCALE_SCRIPT,
        {
            "TAILSCALE_AUTHKEY": ts_authkey,
            "TAILSCALE_HOSTNAME": context.ts_hostname,
            "SSH_LOGIN_USER": context.profile.ssh_user,
        },
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Tailscale enrollment failed (exit {result.returncode}): {result.stderr[-500:]}")
    ts_ip = extract_tailscale_ipv4(result.stdout)
    if not ts_ip:
        ts_ip_result = ssh_cmd("root", context.ip, "tailscale ip -4", timeout=15)
        ts_ip = ts_ip_result.stdout.strip().split("\n")[0] if ts_ip_result.returncode == 0 else None
    update_box(context.box, tailscale_ip=ts_ip, state="deploying")
    save_inventory(context.boxes)
    return f"tailscale {context.ts_hostname} at {ts_ip or 'unknown'}"


def _resolve_deploy_target(context: BoxUpContext) -> str:
    if not box_ssh_candidates(context.box, prefer_public=context.box.state == "ssh-ready"):
        raise RuntimeError("No SSH target is known for deploy")
    for ssh_target in box_ssh_candidates(context.box, prefer_public=context.box.state == "ssh-ready"):
        max_wait = 30 if ssh_target == context.ip else 60
        if wait_for_ssh(ssh_target, user=context.profile.ssh_user, max_wait=max_wait, interval=5):
            context.ssh_target = ssh_target
            return ssh_target

    raise RuntimeError(
        f"Cannot reach {context.profile.ssh_user}@{context.ts_hostname or '<no-tailscale-host>'}, "
        f"{context.box.tailscale_ip or '<no-tailscale-ip>'}, or {context.ip} via SSH"
    )


def _deploy_box_runtime(context: BoxUpContext) -> str:
    ssh_target = _resolve_deploy_target(context)
    if context.deploy_release is None:
        if not context.is_json:
            print(f"[...] deploy  Cloning skillbox and starting container via {context.ts_hostname}...")
        deploy_cmds = build_deploy_command(context.profile)
        result = ssh_cmd(context.profile.ssh_user, ssh_target, deploy_cmds, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"Deploy failed (exit {result.returncode}): {result.stderr[-500:]}")
    else:
        release = context.deploy_release
        if not context.is_json:
            print(
                f"[...] deploy  Installing pinned release {release.source_commit[:12]} "
                f"via {context.ts_hostname}..."
            )
        remote_home = f"/home/{context.profile.ssh_user}"
        remote_archive_path = f"{remote_home}/{release.archive_path.name}"
        upload_result = scp_file(release.archive_path, context.profile.ssh_user, ssh_target, remote_archive_path, timeout=600)
        if upload_result.returncode != 0:
            raise RuntimeError(f"Deploy archive upload failed (exit {upload_result.returncode}): {upload_result.stderr[-500:]}")

        install_args = build_release_install_args(
            context.box_id,
            release,
            remote_archive_path=remote_archive_path,
            repo_dir=f"{remote_home}/skillbox",
            private_path=f"{remote_home}/skillbox-config",
        )
        result = ssh_script(
            context.profile.ssh_user,
            ssh_target,
            INSTALL_SCRIPT,
            script_args=install_args,
            timeout=1800,
        )
        if result.returncode != 0:
            tail = result.stderr[-500:] or result.stdout[-500:]
            raise RuntimeError(f"Deploy failed (exit {result.returncode}): {tail}")
    update_box(context.box, state="acceptance")
    save_inventory(context.boxes)
    if context.deploy_release is None:
        return "container running"
    return f"installed release {context.deploy_release.source_commit[:12]}"


def _patch_remote_runtime_contract(context: BoxUpContext) -> dict[str, Any]:
    if context.ssh_target is None:
        raise RuntimeError("SSH target unavailable while writing remote runtime contract")
    remote_home = f"/home/{context.profile.ssh_user}"
    remote_repo_dir = f"{remote_home}/skillbox"
    payload = remote_box_contract_payload(context)
    result = ssh_cmd(
        context.profile.ssh_user,
        context.ssh_target,
        build_remote_contract_command(payload, repo_dir=remote_repo_dir),
        timeout=60,
    )
    if result.returncode != 0:
        tail = result.stderr[-500:] or result.stdout[-500:]
        raise RuntimeError(f"remote contract patch failed (exit {result.returncode}): {tail}")
    try:
        detail = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        detail = {"stdout_tail": result.stdout[-500:]}
    return {
        "active_profiles": payload.get("active_profiles") or ["core"],
        "swimmers_auth_token_env": payload.get("swimmers_auth_token_env"),
        **detail,
    }


def _launch_remote_workspace(context: BoxUpContext) -> dict[str, Any]:
    if context.deploy_release is None:
        return {
            "skipped": "legacy deploy already launched workspace",
            "active_profiles": active_profiles_for_release(context.deploy_release),
        }
    if context.ssh_target is None:
        raise RuntimeError("SSH target unavailable while launching remote workspace")

    active_profiles = active_profiles_for_release(context.deploy_release)
    remote_home = f"/home/{context.profile.ssh_user}"
    remote_repo_dir = f"{remote_home}/skillbox"
    targets = remote_workspace_launch_targets(active_profiles)
    result = ssh_cmd(
        context.profile.ssh_user,
        context.ssh_target,
        build_remote_workspace_launch_command(active_profiles, repo_dir=remote_repo_dir),
        timeout=1800,
    )
    if result.returncode != 0:
        tail = result.stderr[-500:] or result.stdout[-500:]
        raise RuntimeError(f"remote workspace launch failed (exit {result.returncode}): {tail}")
    return {
        "targets": targets,
        "active_profiles": active_profiles,
    }


def _verify_operator_swimmers_surface(context: BoxUpContext) -> dict[str, Any]:
    active_profiles = active_profiles_for_release(context.deploy_release)
    if "swimmers" not in active_profiles:
        return {"skipped": "no swimmers profile", "active_profiles": active_profiles}

    ts_ip = str(context.box.tailscale_ip or "").strip()
    if not ts_ip:
        raise RuntimeError("Cannot verify swimmers from operator side without a Tailscale IP.")
    token, token_source = local_swimmers_auth_token(context.box_id)
    if not token:
        raise RuntimeError(
            "Cannot verify swimmers from operator side without "
            f"SKILLBOX_SWIMMERS_AUTH_TOKEN or {derived_swimmers_auth_token_env(context.box_id)}."
        )

    port = os.environ.get("SKILLBOX_SWIMMERS_PORT", "3210").strip() or "3210"
    url = f"http://{ts_ip}:{port}/v1/sessions"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read(1024)
            status = response.status
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Operator-side swimmers check failed for {url}: {exc}") from exc
    if status != 200:
        raise RuntimeError(f"Operator-side swimmers check returned HTTP {status} for {url}.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Operator-side swimmers check returned non-JSON from {url}.") from exc
    if "sessions" not in payload:
        raise RuntimeError(f"Operator-side swimmers check returned JSON without sessions from {url}.")
    return {
        "url": url,
        "auth_token_env": token_source,
        "sessions": len(payload.get("sessions") or []),
    }


def _run_box_first_box(context: BoxUpContext, *, blueprint: str | None, set_args: list[str]) -> dict[str, Any]:
    if context.ssh_target is None:
        raise RuntimeError("SSH target unavailable during first-box")
    remote_home = f"/home/{context.profile.ssh_user}"
    remote_repo_dir = f"{remote_home}/skillbox"
    remote_private_path = f"{remote_home}/skillbox-config"
    if not context.is_json:
        print(f"[...] first-box  Running canonical first-box for client {context.box_id}...")
    effective_set_args = augment_spaps_tailnet_set_args(
        set_args,
        blueprint=blueprint,
        tailscale_ip=context.box.tailscale_ip,
    )
    exec_cmd = build_first_box_command(
        context.box_id,
        repo_dir=remote_repo_dir,
        private_path=remote_private_path,
        active_profiles=context.deploy_release.active_profiles if context.deploy_release is not None else [],
        blueprint=blueprint,
        set_args=effective_set_args,
    )
    result = ssh_cmd(context.profile.ssh_user, context.ssh_target, exec_cmd, timeout=600)
    if result.returncode != 0:
        tail = result.stderr[-500:] or result.stdout[-500:]
        raise RuntimeError(f"first-box failed (exit {result.returncode}): {tail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "client_id": context.box_id,
            "active_profiles": active_profiles_for_release(context.deploy_release),
            "status": "ok",
        }
    return {
        "client_id": payload.get("client_id") or context.box_id,
        "active_profiles": payload.get("active_profiles") or active_profiles_for_release(context.deploy_release),
        "created_client": payload.get("created_client"),
        "output_dir": payload.get("output_dir"),
    }


def _box_up_success_payload(context: BoxUpContext) -> dict[str, Any]:
    ssh_target = context.ssh_target or context.box.tailscale_ip or context.ts_hostname or context.box.droplet_ip
    payload = {
        "box_id": context.box_id,
        "profile": asdict(context.profile),
        "dry_run": False,
        "droplet_id": context.box.droplet_id,
        "droplet_ip": context.box.droplet_ip,
        "tailscale_hostname": context.ts_hostname,
        "tailscale_ip": context.box.tailscale_ip,
        "ssh": f"ssh {context.profile.ssh_user}@{ssh_target}" if ssh_target else None,
        "steps": context.steps,
        "storage": storage_payload(context.profile.storage),
        "volume": volume_payload(context.box),
        "next_actions": [f"box ssh {context.box_id}", f"box status {context.box_id}"],
    }
    if context.deploy_release is not None:
        payload["deploy_release"] = deploy_release_payload(context.deploy_release)
    return payload


def _run_resumed_box_up(
    context: BoxUpContext,
    *,
    blueprint: str | None,
    set_args: list[str],
) -> int:
    box_id = context.box_id
    _record_box_up_step(context, "create", "skip", f"resuming droplet {context.box.droplet_id or 'unknown'}")
    _record_box_up_step(context, "storage", "skip", f"resuming state root {context.box.state_root or 'unknown'}")
    _record_box_up_step(context, "bootstrap", "skip", "resuming existing host")

    if not _run_box_up_stage(
        context,
        stage_name="ssh-ready",
        error_type="ssh_access_failed",
        action=lambda: f"ssh {context.profile.ssh_user}@{_resolve_deploy_target(context)}",
        failure_state="ssh-ready",
        next_actions=[f"box status {box_id}", f"box ssh {box_id}"],
    ):
        return EXIT_ERROR

    if context.box.tailscale_ip:
        _record_box_up_step(context, "enroll", "skip", f"already enrolled at {context.box.tailscale_ip}")
    else:
        try:
            ts_authkey = require_env("SKILLBOX_TS_AUTHKEY")
        except RuntimeError as exc:
            _record_box_up_step(context, "enroll", "fail", str(exc))
            _emit_box_up_failure(
                context,
                error_type="tailscale_auth_missing",
                message=str(exc),
                next_actions=[f"box status {box_id}", f"box ssh {box_id}"],
            )
            return EXIT_ERROR
        if not _run_box_up_stage(
            context,
            stage_name="enroll",
            error_type="tailscale_failed",
            action=lambda: _enroll_box_tailscale(context, ts_authkey=ts_authkey),
            failure_state="ssh-ready",
            next_actions=[f"box ssh {box_id}", f"box down {box_id}"],
        ):
            return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="deploy",
        error_type="deploy_failed",
        action=lambda: _deploy_box_runtime(context),
        failure_state="ssh-ready",
        next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="contract",
        error_type="remote_contract_failed",
        action=lambda: _patch_remote_runtime_contract(context),
        failure_state="ssh-ready",
        next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="launch",
        error_type="remote_launch_failed",
        action=lambda: _launch_remote_workspace(context),
        failure_state="acceptance",
        next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="first-box",
        error_type="first_box_failed",
        action=lambda: _run_box_first_box(context, blueprint=blueprint, set_args=set_args),
        failure_state="ssh-ready",
        next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="verify",
        error_type="operator_verify_failed",
        action=lambda: _verify_operator_swimmers_surface(context),
        failure_state="ssh-ready",
        next_actions=[f"box status {box_id}", f"box ssh {box_id}"],
    ):
        return EXIT_ERROR

    update_box(context.box, state="ready")
    save_inventory(context.boxes)
    payload = _box_up_success_payload(context)
    payload["resumed"] = True
    if context.is_json:
        emit_json(payload)
    else:
        print()
        print(f"Box {box_id} is ready.")
        print(f"  SSH: ssh {context.profile.ssh_user}@{context.box.tailscale_ip or context.box.tailscale_hostname or context.box.droplet_ip}")
    return EXIT_OK


def cmd_up(
    box_id: str,
    *,
    profile_name: str,
    blueprint: str | None,
    set_args: list[str],
    deploy_manifest: str | None,
    resume: bool,
    dry_run: bool,
    fmt: str,
) -> int:
    is_json = fmt == "json"
    effective_blueprint = blueprint or DEFAULT_FIRST_BOX_BLUEPRINT

    try:
        profile = load_profile(profile_name)
    except RuntimeError as exc:
        if is_json:
            emit_json(structured_error(str(exc), error_type="profile_not_found"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    boxes = load_inventory()
    existing = find_box(boxes, box_id)
    if existing and existing.state not in ("destroyed",) and not resume:
        msg = (
            f"Box {box_id!r} already exists in state {existing.state!r}. "
            "Use 'box up --resume' for a partial provision, 'box down' first, or choose a different id."
        )
        if is_json:
            emit_json(
                structured_error(
                    msg,
                    error_type="conflict",
                    next_actions=[
                        f"box up {box_id} --profile {profile_name} --deploy-manifest <path> --resume",
                        f"box down {box_id}",
                        f"box status {box_id}",
                    ],
                )
            )
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR
    if resume and (existing is None or existing.state == "destroyed"):
        msg = f"Box {box_id!r} has no resumable inventory entry."
        if is_json:
            emit_json(structured_error(msg, error_type="not_found", next_actions=["box list"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR
    if resume and existing and existing.state not in RESUMABLE_UP_STATES:
        msg = (
            f"Box {box_id!r} cannot resume from state {existing.state!r}; "
            f"resumable states are: {', '.join(sorted(RESUMABLE_UP_STATES))}."
        )
        if is_json:
            emit_json(structured_error(msg, error_type="invalid_state", next_actions=[f"box status {box_id}", f"box down {box_id}"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR
    if resume and existing and existing.profile != profile_name:
        msg = f"Box {box_id!r} uses profile {existing.profile!r}, not {profile_name!r}."
        if is_json:
            emit_json(structured_error(msg, error_type="profile_mismatch", next_actions=[f"box up {box_id} --profile {existing.profile} --deploy-manifest <path> --resume"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    deploy_release: DeployRelease | None = None
    if deploy_manifest:
        try:
            deploy_release = load_deploy_manifest(Path(deploy_manifest), expected_client_id=box_id)
        except RuntimeError as exc:
            if is_json:
                emit_json(structured_error(str(exc), error_type="deploy_manifest_invalid"))
            else:
                print(str(exc), file=sys.stderr)
            return EXIT_ERROR
    elif not dry_run:
        msg = (
            "box up requires --deploy-manifest for non-dry-run launches. "
            "Branch-based deploys are not allowed for remote provisioning."
        )
        if is_json:
            emit_json(
                structured_error(
                    msg,
                    error_type="deploy_manifest_required",
                    next_actions=[f"box up {box_id} --profile {profile_name} --deploy-manifest <path>"],
                )
            )
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    if resume and existing is not None:
        context = _build_box_resume_context(
            existing=existing,
            profile=profile,
            boxes=boxes,
            is_json=is_json,
            deploy_release=deploy_release,
        )
        if dry_run:
            _record_box_up_step(context, "create", "skip", f"would resume droplet {existing.droplet_id or 'unknown'}")
            _record_box_up_step(context, "storage", "skip", "would reuse attached state root")
            _record_box_up_step(context, "bootstrap", "skip", "would reuse existing host")
            _record_box_up_step(context, "ssh-ready", "skip", "would verify existing SSH")
            _record_box_up_step(context, "enroll", "skip", "would enroll only if Tailscale IP is missing")
            _record_box_up_step(context, "deploy", "skip", "would reinstall pinned release")
            _record_box_up_step(context, "contract", "skip", "would write remote .env and .mcp.json contract")
            _record_box_up_step(context, "launch", "skip", "would build and start remote workspace")
            _record_box_up_step(context, "first-box", "skip", "would rerun first-box")
            _record_box_up_step(context, "verify", "skip", "would run operator-side checks")
            payload = {
                "box_id": context.box_id,
                "profile": asdict(context.profile),
                "dry_run": True,
                "resumed": True,
                "steps": context.steps,
                "storage": storage_payload(context.profile.storage),
                "volume": volume_payload(context.box),
                "next_actions": [f"box up {context.box_id} --profile {context.profile_name} --deploy-manifest <path> --resume"],
            }
            if context.deploy_release is not None:
                payload["deploy_release"] = deploy_release_payload(context.deploy_release)
            if is_json:
                emit_json(payload)
            return EXIT_OK
        return _run_resumed_box_up(context, blueprint=effective_blueprint, set_args=set_args)

    if not dry_run:
        try:
            require_profile_storage(profile)
        except RuntimeError as exc:
            if is_json:
                emit_json(
                    structured_error(
                        str(exc),
                        error_type="storage_layout_missing",
                        next_actions=["box profiles --format json", f"box up {box_id} --profile {profile_name} --dry-run"],
                    )
                )
            else:
                print(str(exc), file=sys.stderr)
            return EXIT_ERROR

    context = _build_box_up_context(
        box_id=box_id,
        profile_name=profile_name,
        profile=profile,
        boxes=boxes,
        is_json=is_json,
        deploy_release=deploy_release,
    )

    if dry_run:
        payload = _box_up_dry_run_payload(context)
        if is_json:
            emit_json(payload)
        return EXIT_OK

    do_token = require_env("SKILLBOX_DO_TOKEN")
    ssh_key_id = require_env("SKILLBOX_DO_SSH_KEY_ID")
    ts_authkey = require_env("SKILLBOX_TS_AUTHKEY")
    os.environ["DIGITALOCEAN_ACCESS_TOKEN"] = do_token

    context.boxes = [candidate for candidate in boxes if candidate.id != box_id]

    if not _run_box_up_stage(
        context,
        stage_name="create",
        error_type="droplet_create_failed",
        action=lambda: _create_box_droplet(context, ssh_key_id=ssh_key_id),
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="storage",
        error_type="storage_attach_failed",
        action=lambda: _ensure_box_storage(context),
        failure_state="bootstrapping",
        next_actions=[f"box down {box_id}", f"box status {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="bootstrap",
        error_type="bootstrap_failed",
        action=lambda: _bootstrap_box_host(context),
        failure_state="bootstrapping",
        next_actions=[f"box down {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="ssh-ready",
        error_type="ssh_access_failed",
        action=lambda: _mark_box_ssh_ready(context),
        failure_state="bootstrapping",
        next_actions=[f"box down {box_id}", f"ssh {context.profile.ssh_user}@<public-ip>"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="enroll",
        error_type="tailscale_failed",
        action=lambda: _enroll_box_tailscale(context, ts_authkey=ts_authkey),
        failure_state="ssh-ready",
        next_actions=[f"box ssh {box_id}", f"box down {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="deploy",
        error_type="deploy_failed",
        action=lambda: _deploy_box_runtime(context),
        failure_state="ssh-ready",
        next_actions=[f"box ssh {box_id}", f"box down {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="contract",
        error_type="remote_contract_failed",
        action=lambda: _patch_remote_runtime_contract(context),
        failure_state="ssh-ready",
        next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="launch",
        error_type="remote_launch_failed",
        action=lambda: _launch_remote_workspace(context),
        failure_state="acceptance",
        next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="first-box",
        error_type="first_box_failed",
        action=lambda: _run_box_first_box(context, blueprint=effective_blueprint, set_args=set_args),
        failure_state="ssh-ready",
        next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="verify",
        error_type="operator_verify_failed",
        action=lambda: _verify_operator_swimmers_surface(context),
        failure_state="ssh-ready",
        next_actions=[f"box status {box_id}", f"box ssh {box_id}"],
    ):
        return EXIT_ERROR

    update_box(context.box, state="ready")
    save_inventory(context.boxes)
    payload = _box_up_success_payload(context)
    if is_json:
        emit_json(payload)
    else:
        print()
        print(f"Box {box_id} is ready.")
        print(f"  SSH: ssh {context.profile.ssh_user}@{context.ts_hostname}")
        print(f"  IP:  {context.box.droplet_ip} (public) / {context.box.tailscale_ip or 'pending'} (tailscale)")
        if context.box.state_root:
            print(f"  State root: {context.box.state_root} ({context.box.storage_filesystem or 'unknown fs'})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# box upgrade
# ---------------------------------------------------------------------------

def _record_box_step(steps: list[dict[str, Any]], is_json: bool, name: str, status: str, detail: Any = None) -> None:
    entry: dict[str, Any] = {"step": name, "status": status}
    if detail is not None:
        entry["detail"] = detail
    steps.append(entry)
    if not is_json:
        marker = "ok" if status == "ok" else ("skip" if status == "skip" else "FAIL")
        suffix = f"  {detail}" if isinstance(detail, str) and detail else ""
        print(f"[{marker}] {name}{suffix}")


def _resolve_existing_box_target(box: Box) -> str:
    prefer_public = box.state == "ssh-ready"
    target = resolve_box_ssh_target(box, max_wait=10 if prefer_public else 15, interval=2, prefer_public=prefer_public)
    if target:
        return target
    raise RuntimeError(
        f"Cannot reach {box.ssh_user}@{box.tailscale_hostname or '<no-tailscale-host>'}, "
        f"{box.tailscale_ip or '<no-tailscale-ip>'}, or {box.droplet_ip or '<no-public-ip>'} via SSH"
    )


def _emit_box_upgrade_failure(
    *,
    box_id: str,
    steps: list[dict[str, Any]],
    is_json: bool,
    error_type: str,
    message: str,
    deploy_release: DeployRelease,
    next_actions: list[str] | None = None,
) -> int:
    payload: dict[str, Any] = {
        "box_id": box_id,
        "dry_run": False,
        "steps": steps,
        "deploy_release": deploy_release_payload(deploy_release),
    }
    payload.update(structured_error(message, error_type=error_type, next_actions=next_actions))
    if is_json:
        emit_json(payload)
    else:
        print(message, file=sys.stderr)
    return EXIT_ERROR


def _box_upgrade_dry_run_payload(box: Box, release: DeployRelease, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "box_id": box.id,
        "profile": box.profile,
        "dry_run": True,
        "steps": steps,
        "volume": volume_payload(box),
        "deploy_release": deploy_release_payload(release),
        "next_actions": [f"box upgrade {box.id} --deploy-manifest {release.manifest_path}"],
    }


def _box_upgrade_success_payload(box: Box, release: DeployRelease, steps: list[dict[str, Any]]) -> dict[str, Any]:
    ssh_target = box.tailscale_ip or box.tailscale_hostname or box.droplet_ip
    return {
        "box_id": box.id,
        "profile": box.profile,
        "dry_run": False,
        "ssh": f"ssh {box.ssh_user}@{ssh_target}" if ssh_target else None,
        "steps": steps,
        "volume": volume_payload(box),
        "deploy_release": deploy_release_payload(release),
        "next_actions": [f"box status {box.id}", f"box ssh {box.id}"],
    }


def cmd_upgrade(
    box_id: str,
    *,
    deploy_manifest: str,
    dry_run: bool,
    fmt: str,
) -> int:
    is_json = fmt == "json"
    steps: list[dict[str, Any]] = []
    boxes = load_inventory()
    box = find_box(boxes, box_id)
    if box is None or box.state == "destroyed":
        msg = f"Box {box_id!r} not found or already destroyed."
        if is_json:
            emit_json(structured_error(msg, error_type="not_found", next_actions=["box list"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR
    if box.state != "ready":
        msg = f"Box {box_id!r} must be in 'ready' state for upgrade; found {box.state!r}."
        if is_json:
            emit_json(structured_error(msg, error_type="invalid_state", next_actions=[f"box status {box_id}"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    try:
        release = load_deploy_manifest(Path(deploy_manifest), expected_client_id=box_id)
    except RuntimeError as exc:
        if is_json:
            emit_json(structured_error(str(exc), error_type="deploy_manifest_invalid"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        profile = load_profile(box.profile)
    except RuntimeError as exc:
        if is_json:
            emit_json(structured_error(str(exc), error_type="profile_not_found", next_actions=["box profiles --format json"]))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if dry_run:
        _record_box_step(steps, is_json, "upload", "skip", "dry-run")
        _record_box_step(steps, is_json, "contract", "skip", "would refresh remote .env and .mcp.json contract")
        _record_box_step(steps, is_json, "upgrade", "skip", f"would install {release.source_commit[:12]}")
        _record_box_step(steps, is_json, "verify", "skip", "dry-run")
        if is_json:
            emit_json(_box_upgrade_dry_run_payload(box, release, steps))
        return EXIT_OK

    try:
        ssh_target = _resolve_existing_box_target(box)
    except RuntimeError as exc:
        _record_box_step(steps, is_json, "upload", "fail", str(exc))
        return _emit_box_upgrade_failure(
            box_id=box_id,
            steps=steps,
            is_json=is_json,
            error_type="ssh_unreachable",
            message=str(exc),
            deploy_release=release,
            next_actions=[f"box status {box_id}", f"box ssh {box_id}"],
        )

    remote_home = f"/home/{box.ssh_user}"
    remote_repo_dir = f"{remote_home}/skillbox"
    remote_archive_path = f"{remote_home}/{release.archive_path.name}"
    upload_result = scp_file(release.archive_path, box.ssh_user, ssh_target, remote_archive_path, timeout=600)
    if upload_result.returncode != 0:
        detail = upload_result.stderr[-500:] or upload_result.stdout[-500:] or "scp failed"
        _record_box_step(steps, is_json, "upload", "fail", detail)
        return _emit_box_upgrade_failure(
            box_id=box_id,
            steps=steps,
            is_json=is_json,
            error_type="upload_failed",
            message=f"Upgrade archive upload failed: {detail}",
            deploy_release=release,
            next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
        )
    _record_box_step(steps, is_json, "upload", "ok", remote_archive_path)

    contract_context = _build_box_resume_context(
        existing=box,
        profile=profile,
        boxes=boxes,
        is_json=is_json,
        deploy_release=release,
    )
    contract_context.ssh_target = ssh_target
    try:
        contract_detail = _patch_remote_runtime_contract(contract_context)
    except RuntimeError as exc:
        _record_box_step(steps, is_json, "contract", "fail", str(exc))
        return _emit_box_upgrade_failure(
            box_id=box_id,
            steps=steps,
            is_json=is_json,
            error_type="remote_contract_failed",
            message=str(exc),
            deploy_release=release,
            next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
        )
    _record_box_step(steps, is_json, "contract", "ok", contract_detail)

    upgrade_args = build_release_upgrade_args(
        box_id,
        release,
        remote_archive_path=remote_archive_path,
        repo_dir=remote_repo_dir,
    )
    upgrade_result = ssh_script(
        box.ssh_user,
        ssh_target,
        UPGRADE_SCRIPT,
        script_args=upgrade_args,
        timeout=1800,
    )
    if upgrade_result.returncode != 0:
        detail = upgrade_result.stderr[-500:] or upgrade_result.stdout[-500:] or "remote upgrade failed"
        _record_box_step(steps, is_json, "upgrade", "fail", detail)
        return _emit_box_upgrade_failure(
            box_id=box_id,
            steps=steps,
            is_json=is_json,
            error_type="upgrade_failed",
            message=f"Remote upgrade failed: {detail}",
            deploy_release=release,
            next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
        )
    _record_box_step(steps, is_json, "upgrade", "ok", f"installed {release.source_commit[:12]}")

    verify_result = ssh_cmd(
        box.ssh_user,
        ssh_target,
        "cd ~/skillbox && docker compose ps --format json 2>/dev/null | head -1",
        timeout=30,
    )
    verify_ok = verify_result.returncode == 0 and "workspace" in verify_result.stdout
    verify_detail = {
        "ssh_target": ssh_target,
        "container_running": verify_ok,
    }
    _record_box_step(steps, is_json, "verify", "ok" if verify_ok else "fail", verify_detail)
    if not verify_ok:
        return _emit_box_upgrade_failure(
            box_id=box_id,
            steps=steps,
            is_json=is_json,
            error_type="verify_failed",
            message=f"Box {box_id!r} did not report a healthy workspace container after upgrade.",
            deploy_release=release,
            next_actions=[f"box status {box_id}", f"box ssh {box_id}"],
        )

    update_box(box, state="ready")
    save_inventory(boxes)
    payload = _box_upgrade_success_payload(box, release, steps)
    if is_json:
        emit_json(payload)
    else:
        print()
        print(f"Box {box_id} upgraded to {release.source_commit[:12]}.")
        print(f"  SSH: ssh {box.ssh_user}@{box.tailscale_ip or box.tailscale_hostname or box.droplet_ip}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# box down
# ---------------------------------------------------------------------------

def cmd_down(box_id: str, *, dry_run: bool, fmt: str) -> int:
    is_json = fmt == "json"
    steps: list[dict[str, Any]] = []

    def step(name: str, status: str, detail: Any = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"step": name, "status": status}
        if detail is not None:
            entry["detail"] = detail
        steps.append(entry)
        if not is_json:
            marker = "ok" if status == "ok" else ("skip" if status == "skip" else "FAIL")
            print(f"[{marker}] {name}")
        return entry

    boxes = load_inventory()
    box = find_box(boxes, box_id)
    if box is None or box.state == "destroyed":
        msg = f"Box {box_id!r} not found or already destroyed."
        if is_json:
            emit_json(structured_error(msg, error_type="not_found", next_actions=["box list"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    if box.management_mode == "external":
        msg = (
            f"Box {box_id!r} was registered from an existing shared host and cannot be torn down "
            f"through box down. Use 'box unregister {box_id}' to remove the local inventory entry."
        )
        if is_json:
            emit_json(structured_error(msg, error_type="invalid_state", next_actions=[f"box unregister {box_id}"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    do_token = optional_env("SKILLBOX_DO_TOKEN")
    if do_token:
        os.environ["DIGITALOCEAN_ACCESS_TOKEN"] = do_token

    if dry_run:
        step("drain", "skip", "dry-run")
        step("remove", "skip", "dry-run")
        step("destroy", "skip", f"would destroy droplet {box.droplet_id}")
        payload: dict[str, Any] = {"box_id": box_id, "dry_run": True, "steps": steps, "next_actions": [f"box down {box_id}"]}
        if is_json:
            emit_json(payload)
        return EXIT_OK

    # -- 1. Drain ---------------------------------------------------------------
    ssh_target = resolve_box_ssh_target(box, max_wait=5, interval=1, prefer_public=box.state == "ssh-ready")
    if ssh_target and box.state == "ready":
        try:
            if not is_json:
                print(f"[...] drain  Stopping services on {ssh_target}...")
            result = ssh_cmd(box.ssh_user, ssh_target, "cd ~/skillbox && make down", timeout=60)
            step("drain", "ok" if result.returncode == 0 else "warn")
        except Exception:
            step("drain", "warn", "SSH unreachable, skipping drain")
    else:
        step("drain", "skip", f"box in state {box.state}")

    update_box(box, state="draining")
    save_inventory(boxes)

    # -- 2. Remove from Tailnet -------------------------------------------------
    if ssh_target:
        try:
            if not is_json:
                print("[...] remove  Removing from tailnet...")
            # Run tailscale logout on the box itself
            result = ssh_cmd("root", box.droplet_ip or ssh_target, "tailscale logout", timeout=30)
            detail = result.stderr[-500:] or result.stdout[-500:] or None
            step("remove", "ok" if result.returncode == 0 else "warn", detail)
        except Exception:
            step("remove", "warn", "Could not remove from tailnet")
    else:
        step("remove", "skip", "no ssh target")

    # -- 3. Destroy droplet -----------------------------------------------------
    destroy_ok = False
    if box.droplet_id:
        try:
            if not is_json:
                print(f"[...] destroy  Deleting droplet {box.droplet_id}...")
            if do_delete_droplet(box.droplet_id):
                step("destroy", "ok", f"droplet {box.droplet_id} deleted")
                destroy_ok = True
            else:
                step("destroy", "fail", "doctl delete returned non-zero")
        except Exception as exc:
            step("destroy", "fail", str(exc))
    else:
        step("destroy", "skip", "no droplet id")
        destroy_ok = True

    if not destroy_ok:
        save_inventory(boxes)
        message = f"Droplet deletion failed for box {box_id!r}; inventory state remains {box.state!r}."
        payload = {
            "box_id": box_id,
            "dry_run": False,
            "steps": steps,
            "next_actions": [f"box status {box_id}", f"box down {box_id}"],
        }
        payload.update(
            structured_error(
                message,
                error_type="destroy_failed",
                next_actions=[f"box status {box_id}", f"box down {box_id}"],
            )
        )
        if is_json:
            emit_json(payload)
        else:
            print(message, file=sys.stderr)
        return EXIT_ERROR

    update_box(box, state="destroyed")
    save_inventory(boxes)

    payload = {"box_id": box_id, "dry_run": False, "steps": steps, "next_actions": ["box list"]}
    if is_json:
        emit_json(payload)
    else:
        print(f"\nBox {box_id} destroyed.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# box unregister
# ---------------------------------------------------------------------------

def cmd_unregister(box_id: str, *, fmt: str) -> int:
    is_json = fmt == "json"
    boxes = load_inventory()
    box = find_box(boxes, box_id)
    if box is None or box.state == "destroyed":
        msg = f"Box {box_id!r} not found or already destroyed."
        if is_json:
            emit_json(structured_error(msg, error_type="not_found", next_actions=["box list"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    if box.management_mode != "external":
        msg = f"Box {box_id!r} is managed by this inventory. Use 'box down {box_id}' for teardown."
        if is_json:
            emit_json(structured_error(msg, error_type="invalid_state", next_actions=[f"box down {box_id} --dry-run"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    update_box(box, state="destroyed")
    save_inventory(boxes)
    payload = {
        "box_id": box_id,
        "management_mode": box.management_mode,
        "unregistered": True,
        "next_actions": ["box list"],
    }
    if is_json:
        emit_json(payload)
    else:
        print(f"Unregistered external box {box_id}.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# box register
# ---------------------------------------------------------------------------

def cmd_register(
    box_id: str | None,
    *,
    host: str,
    profile_name: str,
    ssh_user: str | None,
    force: bool,
    probe: bool,
    fmt: str,
) -> int:
    is_json = fmt == "json"
    resolved_box_id = box_id or derive_box_id_from_host(host)

    profile: BoxProfile | None = None
    if profile_name != "shared":
        try:
            profile = load_profile(profile_name)
        except RuntimeError as exc:
            if is_json:
                emit_json(structured_error(str(exc), error_type="profile_not_found"))
            else:
                print(str(exc), file=sys.stderr)
            return EXIT_ERROR

    boxes = load_inventory()
    existing = find_box(boxes, resolved_box_id)
    if existing and existing.state != "destroyed" and not force:
        msg = (
            f"Box {resolved_box_id!r} already exists in state {existing.state!r}. "
            f"Use 'box unregister {resolved_box_id}' or rerun with --force."
        )
        if is_json:
            emit_json(
                structured_error(
                    msg,
                    error_type="conflict",
                    next_actions=[f"box status {resolved_box_id}", f"box unregister {resolved_box_id}"],
                )
            )
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    filtered_boxes = [candidate for candidate in boxes if candidate.id != resolved_box_id]
    now = datetime.now(timezone.utc).isoformat()
    storage = profile.storage if profile is not None else None
    box = Box(
        id=resolved_box_id,
        profile=profile_name,
        state="ready",
        management_mode="external",
        ssh_user=(ssh_user or (profile.ssh_user if profile is not None else "skillbox")),
        created_at=now,
        updated_at=now,
        region=profile.region if profile is not None else "",
        size=profile.size if profile is not None else "",
        storage_provider=storage.provider if storage is not None else None,
        state_root=storage.mount_path if storage is not None else None,
        storage_filesystem=storage.filesystem if storage is not None else None,
        storage_required=storage.required if storage is not None else False,
        storage_min_free_gb=storage.min_free_gb if storage is not None else None,
        volume_name=volume_name_for_box(resolved_box_id) if storage is not None else None,
        volume_size_gb=storage_volume_size_gb(storage) if storage is not None else None,
    )
    update_box(box, **seed_registered_box_fields(host))

    register_probe = probe_registered_box(box, enabled=probe)
    updates: dict[str, Any] = {}
    if register_probe.get("tailscale_ip") and not box.tailscale_ip:
        updates["tailscale_ip"] = register_probe["tailscale_ip"]
    if register_probe.get("ssh_reachable"):
        updates["state"] = "ready" if register_probe.get("container_running") else "ssh-ready"
    if updates:
        update_box(box, **updates)

    filtered_boxes.append(box)
    save_inventory(filtered_boxes)
    payload = registration_payload(box, register_probe, host=host)
    if is_json:
        emit_json(payload)
    else:
        print(f"Registered external box {resolved_box_id} from {host}.")
        print(f"  SSH user: {box.ssh_user}")
        if payload["ssh_reachable"]:
            print(f"  connect: ssh {box.ssh_user}@{payload['ssh_target']}")
        else:
            print("  ssh probe: unreachable (saved with known fields only)")
    return EXIT_OK


# ---------------------------------------------------------------------------
# box status
# ---------------------------------------------------------------------------

def cmd_status(box_id: str | None, *, fmt: str) -> int:
    is_json = fmt == "json"
    boxes = load_inventory()

    if box_id:
        box = find_box(boxes, box_id)
        if box is None:
            msg = f"Box {box_id!r} not found."
            if is_json:
                emit_json(structured_error(msg, error_type="not_found", next_actions=["box list"]))
            else:
                print(msg, file=sys.stderr)
            return EXIT_ERROR

        status = box_health(box)
        if is_json:
            emit_json(status)
        else:
            print_box_status_text(status)
        return EXIT_OK
    else:
        statuses = [box_health(b) for b in boxes if b.state != "destroyed"]
        payload: dict[str, Any] = {
            "boxes": statuses,
            "next_actions": ["box up <id> --profile <name>", "box register <id> --host <tailscale-hostname>"] if not statuses else [],
        }
        if is_json:
            emit_json(payload)
        else:
            if not statuses:
                print("No active boxes.")
            else:
                for s in statuses:
                    print_box_status_text(s)
                    print()
        return EXIT_OK


def box_health(box: Box) -> dict[str, Any]:
    status: dict[str, Any] = {
        "id": box.id,
        "state": box.state,
        "profile": box.profile,
        "management_mode": box.management_mode,
        "droplet_id": box.droplet_id,
        "droplet_ip": box.droplet_ip,
        "tailscale_hostname": box.tailscale_hostname,
        "tailscale_ip": box.tailscale_ip,
        "ssh_user": box.ssh_user,
        "region": box.region,
        "size": box.size,
        "state_root": box.state_root,
        "storage_filesystem": box.storage_filesystem,
        "volume_id": box.volume_id,
        "volume_name": box.volume_name,
        "volume_size_gb": box.volume_size_gb,
        "created_at": box.created_at,
        "ssh_target": None,
        "ssh_reachable": False,
        "container_running": False,
    }

    if box.state in ("destroyed", "creating"):
        return status

    ssh_target = resolve_box_ssh_target(box, max_wait=5, interval=1, prefer_public=box.state == "ssh-ready")
    if ssh_target:
        status["ssh_target"] = ssh_target
        status["ssh_reachable"] = True

        if status["ssh_reachable"]:
            container_probe = ssh_cmd(
                box.ssh_user, ssh_target,
                "cd ~/skillbox && docker compose ps --format json 2>/dev/null | head -1",
                timeout=15,
            )
            status["container_running"] = container_probe.returncode == 0 and "workspace" in container_probe.stdout

    next_actions: list[str] = []
    if not status["ssh_reachable"]:
        if box.management_mode == "external":
            next_actions.append(f"box unregister {box.id}")
        else:
            next_actions.append(f"box down {box.id}")
    elif not status["container_running"]:
        next_actions.append(f"box ssh {box.id}")
    status["next_actions"] = next_actions or [f"box ssh {box.id}"]
    return status


def print_box_status_text(status: dict[str, Any]) -> None:
    reachable = "yes" if status["ssh_reachable"] else "no"
    container = "yes" if status["container_running"] else "no"
    ts = status["tailscale_hostname"] or "n/a"
    print(f"{status['id']}  state={status['state']}  profile={status['profile']}")
    if status.get("management_mode") == "external":
        print("  mode=external")
    print(f"  droplet={status['droplet_id']}  ip={status['droplet_ip']}  ts={ts}")
    if status.get("state_root"):
        print(f"  state_root={status['state_root']}  fs={status.get('storage_filesystem') or 'n/a'}")
    if status.get("volume_name"):
        print(f"  volume={status['volume_name']}  size_gb={status.get('volume_size_gb') or 'n/a'}")
    print(f"  ssh={reachable}  container={container}")
    if status.get("ssh_reachable"):
        connect_target = status.get("ssh_target") or status.get("tailscale_ip") or ts
        print(f"  connect: ssh {status['ssh_user']}@{connect_target}")


# ---------------------------------------------------------------------------
# box ssh
# ---------------------------------------------------------------------------

def cmd_ssh(box_id: str) -> int:
    boxes = load_inventory()
    box = find_box(boxes, box_id)
    if box is None or box.state == "destroyed":
        print(f"Box {box_id!r} not found or destroyed.", file=sys.stderr)
        return EXIT_ERROR

    target = resolve_box_ssh_target(box, max_wait=5, interval=1, prefer_public=box.state == "ssh-ready")
    if not target:
        print(f"Box {box_id!r} has no reachable address.", file=sys.stderr)
        return EXIT_ERROR

    os.execvp("ssh", ["ssh", *DEFAULT_SSH_OPTS, f"{box.ssh_user}@{target}"])
    return EXIT_ERROR  # unreachable


# ---------------------------------------------------------------------------
# box list
# ---------------------------------------------------------------------------

def cmd_list(*, fmt: str) -> int:
    boxes = load_inventory()
    active = [b for b in boxes if b.state != "destroyed"]

    if fmt == "json":
        emit_json({
            "boxes": [asdict(b) for b in active],
            "next_actions": ["box up <id> --profile <name>", "box register <id> --host <tailscale-hostname>"] if not active else [],
        })
    else:
        if not active:
            print("No active boxes.")
        else:
            for b in active:
                ts = b.tailscale_hostname or "n/a"
                root = b.state_root or "n/a"
                volume = b.volume_name or "n/a"
                print(
                    f"  {b.id}  state={b.state}  ts={ts}  ip={b.droplet_ip}  "
                    f"profile={b.profile}  mode={b.management_mode}  state_root={root}  volume={volume}"
                )
    return EXIT_OK


# ---------------------------------------------------------------------------
# box profiles
# ---------------------------------------------------------------------------

def cmd_profiles(*, fmt: str) -> int:
    profiles = list_profiles()
    if fmt == "json":
        emit_json({"profiles": [asdict(p) for p in profiles]})
    else:
        if not profiles:
            print(f"No profiles found in {PROFILES_DIR}")
        else:
            for p in profiles:
                storage = p.storage.mount_path if p.storage is not None else "n/a"
                print(f"  {p.id}  {p.size} in {p.region} ({p.image})  state_root={storage}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(REPO_ROOT / ".env.box")

    parser = argparse.ArgumentParser(
        description="Skillbox box lifecycle manager: create, bootstrap, and destroy DigitalOcean + Tailscale boxes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    up_parser = subparsers.add_parser("up", help="Create and provision a new box from a pinned deploy artifact.")
    up_parser.add_argument("box_id", help="Box identifier (becomes droplet name and client id).")
    up_parser.add_argument("--profile", default="dev-small", help="Box profile from workspace/box-profiles/.")
    up_parser.add_argument(
        "--blueprint",
        default=DEFAULT_FIRST_BOX_BLUEPRINT,
        help=(
            "Client blueprint for the remote first-box step "
            f"(defaults to {DEFAULT_FIRST_BOX_BLUEPRINT}; pass another blueprint to override)."
        ),
    )
    up_parser.add_argument("--set", action="append", default=[], help="Blueprint variable KEY=VALUE.")
    up_parser.add_argument("--deploy-manifest", default=None, help="Pinned deploy.json from client-publish --deploy-artifact. Required unless --dry-run.")
    up_parser.add_argument("--resume", action="store_true", help="Resume a partial box from ssh-ready/deploying/acceptance/onboarding instead of recreating it.")
    up_parser.add_argument("--dry-run", action="store_true")
    up_parser.add_argument("--format", choices=("text", "json"), default="text")

    down_parser = subparsers.add_parser("down", help="Drain and destroy a box.")
    down_parser.add_argument("box_id", help="Box identifier.")
    down_parser.add_argument("--dry-run", action="store_true")
    down_parser.add_argument("--format", choices=("text", "json"), default="text")

    upgrade_parser = subparsers.add_parser("upgrade", help="Upgrade an existing ready box from a pinned deploy manifest.")
    upgrade_parser.add_argument("box_id", help="Box identifier.")
    upgrade_parser.add_argument("--deploy-manifest", required=True, help="Pinned deploy.json from client-publish --deploy-artifact.")
    upgrade_parser.add_argument("--dry-run", action="store_true")
    upgrade_parser.add_argument("--format", choices=("text", "json"), default="text")

    status_parser = subparsers.add_parser("status", help="Check health of one or all boxes.")
    status_parser.add_argument("box_id", nargs="?", default=None, help="Box identifier (omit for all).")
    status_parser.add_argument("--format", choices=("text", "json"), default="text")

    ssh_parser = subparsers.add_parser("ssh", help="SSH into a box.")
    ssh_parser.add_argument("box_id", help="Box identifier.")

    register_parser = subparsers.add_parser("register", help="Register an existing shared or manually created box in local inventory.")
    register_parser.add_argument("box_id", nargs="?", default=None, help="Local box identifier. Defaults to a host-derived alias.")
    register_parser.add_argument("--host", required=True, help="Reachable host: Tailscale hostname, Tailscale IP, or public IP.")
    register_parser.add_argument("--profile", default="shared", help="Local profile label (default: shared).")
    register_parser.add_argument("--ssh-user", default=None, help="SSH login user. Defaults to the profile ssh_user or 'skillbox'.")
    register_parser.add_argument("--force", action="store_true", help="Replace an existing active inventory entry with the same id.")
    register_parser.add_argument("--no-probe", action="store_true", help="Skip the SSH probe and save known fields only.")
    register_parser.add_argument("--format", choices=("text", "json"), default="text")

    import_parser = subparsers.add_parser("import", help="Alias for register.")
    import_parser.add_argument("box_id", nargs="?", default=None, help="Local box identifier. Defaults to a host-derived alias.")
    import_parser.add_argument("--host", required=True, help="Reachable host: Tailscale hostname, Tailscale IP, or public IP.")
    import_parser.add_argument("--profile", default="shared", help="Local profile label (default: shared).")
    import_parser.add_argument("--ssh-user", default=None, help="SSH login user. Defaults to the profile ssh_user or 'skillbox'.")
    import_parser.add_argument("--force", action="store_true", help="Replace an existing active inventory entry with the same id.")
    import_parser.add_argument("--no-probe", action="store_true", help="Skip the SSH probe and save known fields only.")
    import_parser.add_argument("--format", choices=("text", "json"), default="text")

    unregister_parser = subparsers.add_parser("unregister", help="Remove a registered external box from local inventory.")
    unregister_parser.add_argument("box_id", help="Box identifier.")
    unregister_parser.add_argument("--format", choices=("text", "json"), default="text")

    subparsers.add_parser("list", help="List all active boxes.").add_argument(
        "--format", choices=("text", "json"), default="text",
    )

    subparsers.add_parser("profiles", help="List available box profiles.").add_argument(
        "--format", choices=("text", "json"), default="text",
    )

    args = parser.parse_args()

    try:
        if args.command == "up":
            return cmd_up(
                args.box_id,
                profile_name=args.profile,
                blueprint=args.blueprint,
                set_args=args.set,
                deploy_manifest=args.deploy_manifest,
                resume=args.resume,
                dry_run=args.dry_run,
                fmt=args.format,
            )
        if args.command == "down":
            return cmd_down(args.box_id, dry_run=args.dry_run, fmt=args.format)
        if args.command == "upgrade":
            return cmd_upgrade(
                args.box_id,
                deploy_manifest=args.deploy_manifest,
                dry_run=args.dry_run,
                fmt=args.format,
            )
        if args.command == "status":
            return cmd_status(args.box_id, fmt=args.format)
        if args.command == "ssh":
            return cmd_ssh(args.box_id)
        if args.command in ("register", "import"):
            return cmd_register(
                args.box_id,
                host=args.host,
                profile_name=args.profile,
                ssh_user=args.ssh_user,
                force=args.force,
                probe=not args.no_probe,
                fmt=args.format,
            )
        if args.command == "unregister":
            return cmd_unregister(args.box_id, fmt=args.format)
        if args.command == "list":
            return cmd_list(fmt=args.format)
        if args.command == "profiles":
            return cmd_profiles(fmt=args.format)
    except RuntimeError as exc:
        emit_json(structured_error(str(exc)))
        return EXIT_ERROR
    except subprocess.TimeoutExpired as exc:
        emit_json(structured_error(f"Command timed out: {exc.cmd}", error_type="timeout"))
        return EXIT_ERROR

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
