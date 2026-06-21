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
import difflib
import hashlib
import json
import math
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# Single source of truth for secret redaction (scripts/lib/redaction.py), the
# same leaf-import direction shared.py uses for lib.runtime_model. box.py shells
# out to doctl/ssh; their stdout/stderr can echo a DigitalOcean token or a
# Tailscale authkey, which must never reach operator JSON or transcripts.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from lib.redaction import redact_text as redact_diagnostic_text  # noqa: E402

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
RESUMABLE_UP_STATES = {"ssh-ready", "lockdown", "deploying", "acceptance", "onboarding"}
SWIMMERS_ENV_PREFIX = "SKILLBOX_SWIMMERS_"
DEFAULT_SWIMMERS_PORT = "3210"
PROVISIONING_ENV_VARS = (
    "SKILLBOX_DO_TOKEN",
    "SKILLBOX_DO_SSH_KEY_ID",
    "SKILLBOX_TS_AUTHKEY",
)


def inventory_path() -> Path:
    override = os.environ.get("SKILLBOX_BOX_INVENTORY", "").strip()
    if override:
        return Path(override)
    return REPO_ROOT / "workspace" / "boxes.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DRIFT = 2
BOX_COMMAND_NAMES = {
    "capabilities",
    "down",
    "import",
    "list",
    "profiles",
    "register",
    "robot-docs",
    "robot-triage",
    "ssh",
    "status",
    "unregister",
    "up",
    "upgrade",
}
BOX_JSON_COMMANDS = BOX_COMMAND_NAMES - {"robot-docs", "ssh"}
BOX_JSON_FLAG_ALIASES = {"--json", "--jason", "--jsno", "--jsson"}

STATES = [
    "creating",
    "bootstrapping",
    "ssh-ready",
    "enrolling",
    "lockdown",
    "deploying",
    "acceptance",
    "onboarding",
    "ready",
    "draining",
    # Teardown truth states: a droplet that was asked to delete but is still
    # API-listed lands in `destroy-pending` (NOT destroyed — it may still bill).
    # A droplet confirmed-absent whose volume cleanup did not finish lands in
    # `volume-cleanup-failed` (no billing lie; resumable). Both are reported by
    # box-status / box-list with the exact retry command.
    "destroy-pending",
    "volume-cleanup-failed",
    "destroyed",
]

VALID_TRANSITIONS = {
    "creating": ["bootstrapping", "destroyed"],
    "bootstrapping": ["ssh-ready", "destroyed"],
    "ssh-ready": ["enrolling", "destroyed"],
    "enrolling": ["lockdown", "destroyed"],
    "lockdown": ["deploying", "destroyed"],
    "deploying": ["acceptance", "onboarding", "destroyed"],
    "acceptance": ["ready", "destroyed"],
    "onboarding": ["ready", "destroyed"],
    "ready": ["draining", "destroyed"],
    # draining can converge to destroyed, or fall into a truthful pending state
    # when the droplet is still listed (destroy-pending) or the volume cleanup
    # did not finish after the droplet was confirmed gone (volume-cleanup-failed).
    "draining": ["destroy-pending", "volume-cleanup-failed", "destroyed"],
    # destroy-pending re-runs the read-after-delete confirmation; it stays
    # pending, advances to volume-cleanup-failed, or converges to destroyed.
    "destroy-pending": ["destroy-pending", "volume-cleanup-failed", "destroyed"],
    # volume-cleanup-failed re-runs volume cleanup only (droplet already gone).
    "volume-cleanup-failed": ["volume-cleanup-failed", "destroyed"],
}

# States from which `box down` (rerun / --resume) must idempotently converge to
# `destroyed` when the underlying infra cooperates. The droplet is already gone
# (volume-cleanup-failed) or may still be present (destroy-pending), so a rerun
# re-confirms truth rather than blindly trusting a prior delete call.
RESUMABLE_DOWN_STATES = {"destroy-pending", "volume-cleanup-failed"}

DEFAULT_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]
REMOTE_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHA256_HEX_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
IPV4_PATTERN = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_SSH_USER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$')
_HOST_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,253}[a-zA-Z0-9])?$')
_BOX_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _validate_box_id(box_id: str) -> str:
    if not box_id:
        raise argparse.ArgumentTypeError("invalid box_id: must not be empty")
    if "/" in box_id or "\\" in box_id:
        raise argparse.ArgumentTypeError("invalid box_id: must not contain path separators")
    if box_id.startswith("-"):
        raise argparse.ArgumentTypeError("invalid box_id: must not start with '-'")
    if not _BOX_ID_RE.match(box_id):
        raise argparse.ArgumentTypeError(
            "invalid box_id: must match [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}"
        )
    return box_id


def _validate_profile_name(profile_name: str) -> str:
    name = str(profile_name or "").strip()
    if not name:
        raise RuntimeError("Invalid box profile name: must not be empty")
    if "/" in name or "\\" in name:
        raise RuntimeError("Invalid box profile name: must not contain path separators")
    if name.startswith("-"):
        raise RuntimeError("Invalid box profile name: must not start with '-'")
    if not _PROFILE_NAME_RE.match(name):
        raise RuntimeError(
            "Invalid box profile name: must match [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}"
        )
    return name


def _validate_config_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{field} must be a boolean")
    return value


def _validate_ssh_user(user: str) -> str:
    if not _SSH_USER_RE.match(user):
        raise ValueError(f"Invalid ssh user: {user!r}")
    return user


def _validate_host(host: str) -> str:
    if not _HOST_RE.match(host):
        raise ValueError(f"Invalid host: {host!r}")
    return host


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


def emit_error_or_print(
    message: str,
    *,
    is_json: bool,
    error_type: str,
    next_actions: list[str] | None = None,
    recovery_hint: str | None = None,
) -> int:
    if is_json:
        emit_json(
            structured_error(
                message,
                error_type=error_type,
                recovery_hint=recovery_hint,
                next_actions=next_actions,
            )
        )
    else:
        print(message, file=sys.stderr)
    return EXIT_ERROR


def _box_agent_command(name: str) -> dict[str, Any]:
    safe_first_try = {
        "capabilities": "python3 scripts/box.py capabilities --json",
        "down": "python3 scripts/box.py down <box-id> --dry-run --format json",
        "import": "python3 scripts/box.py profiles --format json",
        "list": "python3 scripts/box.py list --format json",
        "profiles": "python3 scripts/box.py profiles --format json",
        "register": "python3 scripts/box.py profiles --format json",
        "robot-docs": "python3 scripts/box.py robot-docs guide",
        "robot-triage": "python3 scripts/box.py --robot-triage",
        "ssh": "Use MCP operator_box_exec for non-interactive commands, or make box-ssh BOX=<id> for a TTY.",
        "status": "python3 scripts/box.py status --format json",
        "unregister": "python3 scripts/box.py status <box-id> --format json",
        "up": "python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json",
        "upgrade": (
            "python3 scripts/box.py upgrade <box-id> --deploy-manifest <deploy.json> "
            "--dry-run --format json"
        ),
    }[name]
    return {
        "name": name,
        "json": name in BOX_JSON_COMMANDS,
        "mutates": name in {"down", "import", "register", "unregister", "up", "upgrade"},
        "destructive": name == "down",
        "dry_run": name in {"down", "up", "upgrade"},
        "safe_first_try": safe_first_try,
        "mutation_command": {
            "import": "python3 scripts/box.py import <box-id> --host <host> --no-probe --format json",
            "register": "python3 scripts/box.py register <box-id> --host <host> --no-probe --format json",
            "unregister": "python3 scripts/box.py unregister <box-id> --format json",
        }.get(name),
    }


def box_capabilities_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "tool": "skillbox-box",
        "contract_version": "2026-05-11",
        "root_dir": str(REPO_ROOT),
        "entrypoint": "python3 scripts/box.py",
        "commands": [_box_agent_command(name) for name in sorted(BOX_COMMAND_NAMES)],
        "agent_surfaces": {
            "capabilities": "python3 scripts/box.py capabilities --json",
            "robot_docs": "python3 scripts/box.py robot-docs guide",
            "robot_triage": "python3 scripts/box.py --robot-triage",
            "json_aliases": sorted(BOX_JSON_FLAG_ALIASES),
        },
        "stdout_stderr_contract": {
            "json_stdout": "When JSON is requested, stdout is parseable JSON only.",
            "diagnostics_stderr": "JSON typo alias notices and parser errors go to stderr.",
        },
        "safety": {
            "dry_run_first": [
                "python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json",
                "python3 scripts/box.py down <box-id> --dry-run --format json",
                (
                    "python3 scripts/box.py upgrade <box-id> --deploy-manifest <deploy.json> "
                    "--dry-run --format json"
                ),
            ],
            "confirm_with_user_before": [
                "python3 scripts/box.py down <box-id> --format json",
            ],
            "non_tty_alternative": "Use MCP operator_box_exec for remote commands instead of box.py ssh.",
        },
        "mcp_equivalents": {
            "profiles": "operator_profiles",
            "list": "operator_boxes",
            "status": "operator_box_status",
            "up": "operator_provision",
            "down": "operator_teardown",
            "ssh": "operator_box_exec",
        },
        "exit_codes": {
            "0": "success",
            "1": "runtime, environment, or user input error",
            "2": "argparse usage error or drift-style status from surrounding tools",
        },
        "next_actions": [
            "python3 scripts/box.py profiles --format json",
            "python3 scripts/box.py list --format json",
            "python3 scripts/box.py status --format json",
            "python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json",
        ],
    }


def box_robot_docs_guide() -> str:
    return """Skillbox box agent guide

Primary entrypoint:
  python3 scripts/box.py <command> [options]

Start here:
  python3 scripts/box.py capabilities --json
  python3 scripts/box.py profiles --format json
  python3 scripts/box.py list --format json
  python3 scripts/box.py status --format json
  python3 scripts/box.py --robot-triage

Structured output:
  Read-side and lifecycle preview commands accept --format json.
  Agent-friendly aliases are accepted: --json, --jason, --jsno, --jsson.
  Diagnostics and typo-alias notices are printed to stderr, not stdout.

Safe mutation pattern:
  Provision, teardown, and upgrade should be previewed first:
  python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json
  python3 scripts/box.py down <box-id> --dry-run --format json
  python3 scripts/box.py upgrade <box-id> --deploy-manifest <deploy.json> --dry-run --format json
  Confirm with the user before real teardown because it destroys infrastructure.

Remote commands:
  box.py ssh is for interactive terminals. Agents should use MCP operator_box_exec
  for non-interactive commands on a box.
"""


def box_robot_triage_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "tool": "skillbox-box",
        "quick_ref": box_capabilities_payload()["next_actions"],
        "recommendations": [
            {
                "id": "inspect-profiles",
                "command": "python3 scripts/box.py profiles --format json",
                "why": "Find valid sizes and SSH users before provisioning.",
            },
            {
                "id": "inspect-fleet",
                "command": "python3 scripts/box.py list --format json",
                "why": "Avoid colliding with an existing box id.",
            },
            {
                "id": "preview-provision",
                "command": "python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json",
                "why": "Checks credentials and planned lifecycle steps without creating infrastructure.",
            },
            {
                "id": "preview-teardown",
                "command": "python3 scripts/box.py down <box-id> --dry-run --format json",
                "why": "Required safe alternative before destroying infrastructure.",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            f"Add it to {operator_secret_dir() / '.env'} or export it before running box commands."
        )
    return val


def missing_env_vars(names: tuple[str, ...] | list[str]) -> list[str]:
    return [name for name in names if not os.environ.get(name, "").strip()]


def provisioning_credentials_next_actions(
    missing: list[str],
    *,
    box_id: str,
    profile_name: str,
) -> list[str]:
    if not missing:
        return [f"box up {box_id} --profile {profile_name} --deploy-manifest <path>"]
    return [
        "Ask the operator for the missing DigitalOcean/Tailscale provisioning values.",
        f"Create or update {operator_secret_dir() / '.env.box'} on the operator machine "
        "with the missing KEY=value lines.",
        f"Missing: {', '.join(missing)}",
        f"Re-run: python3 scripts/box.py up {box_id} --profile {profile_name} --dry-run --format json",
    ]


def provisioning_credentials_status() -> dict[str, Any]:
    missing = missing_env_vars(PROVISIONING_ENV_VARS)
    configured = [name for name in PROVISIONING_ENV_VARS if name not in missing]
    return {
        "ready": not missing,
        "required": list(PROVISIONING_ENV_VARS),
        "configured": configured,
        "missing": missing,
        "env_files": [
            str(operator_secret_dir() / ".env"),
            str(operator_secret_dir() / ".env.box"),
        ],
        "message": (
            "Provisioning credentials are ready."
            if not missing
            else "Provisioning is blocked until the missing operator-machine credentials are set."
        ),
    }


def emit_provisioning_credentials_error(*, box_id: str, profile_name: str, is_json: bool) -> int:
    missing = missing_env_vars(PROVISIONING_ENV_VARS)
    if not missing:
        return EXIT_OK

    message = (
        "Required provisioning credentials are unset: "
        + ", ".join(missing)
        + f". Add them to {operator_secret_dir() / '.env.box'} or export them "
        "before running box provisioning."
    )
    next_actions = provisioning_credentials_next_actions(
        missing,
        box_id=box_id,
        profile_name=profile_name,
    )
    if is_json:
        payload = structured_error(
            message,
            error_type="provisioning_credentials_missing",
            recovery_hint=(
                "These are operator-machine credentials, not values to invent inside the box. "
                f"Ask the operator to populate {operator_secret_dir() / '.env.box'} with "
                "SKILLBOX_DO_TOKEN, SKILLBOX_DO_SSH_KEY_ID, and SKILLBOX_TS_AUTHKEY, "
                "then rerun the dry-run."
            ),
            next_actions=next_actions,
        )
        payload["credential_status"] = provisioning_credentials_status()
        emit_json(payload)
    else:
        print(message, file=sys.stderr)
        print("Next actions:", file=sys.stderr)
        for action in next_actions:
            print(f"  - {action}", file=sys.stderr)
    return EXIT_ERROR


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


# Operator secret files (DigitalOcean token, Tailscale authkey, *_TOKEN/*_KEY/*_SECRET).
# These are consumed host-side only; they must live OUTSIDE the `.:/workspace` bind
# mount so in-container agents cannot read them. Canonical home is
# ${SKILLBOX_STATE_ROOT}/operator/ (the state root is mounted only at specific
# subpaths, never wholesale). Repo-root copies are deprecated and warn.
# NOTE: workspace/boxes.json is NOT a credential (droplet IDs/IPs/topology only),
# so it intentionally stays in the workspace mount.
OPERATOR_SECRET_FILENAMES = (".env", ".env.box")


def operator_secret_dir() -> Path:
    """Resolve the canonical operator-secret directory under the state root."""
    state_root = os.environ.get("SKILLBOX_STATE_ROOT", "").strip() or "./.skillbox-state"
    base = Path(state_root)
    if not base.is_absolute():
        base = REPO_ROOT / base
    return (base / "operator").resolve()


def load_operator_secret(name: str) -> None:
    """Load an operator secret file, preferring the relocated state-root copy.

    Falls back to the deprecated repo-root location (inside the workspace mount)
    with a loud stderr warning; no-op when neither file exists.
    """
    new_path = operator_secret_dir() / name
    legacy_path = REPO_ROOT / name
    if new_path.is_file():
        load_dotenv(new_path)
        return
    if legacy_path.is_file():
        sys.stderr.write(
            f"[skillbox] DEPRECATED secret location: {legacy_path} is inside the workspace "
            f"bind mount and readable by in-container agents.\n"
            f"[skillbox] Move it out of the mount with:\n"
            f"    mkdir -p {operator_secret_dir()} && mv {legacy_path} {new_path}\n"
        )
        load_dotenv(legacy_path)
        return
    # neither present: leave os.environ untouched; existing missing-credential UX handles it.


# ---------------------------------------------------------------------------
# CLI runners
# ---------------------------------------------------------------------------

def _redact_completed_process(
    result: subprocess.CompletedProcess[str],
) -> subprocess.CompletedProcess[str]:
    """Redact secrets out of a remote subprocess's captured stdout/stderr.

    This is THE single boundary where remote (doctl/ssh) output enters
    operator-visible payloads, status checks, error tails, and JSON parses.
    Redaction is value-targeted (KEY=value, bearer tokens, URL userinfo,
    tskey-/dop_v1_ tokens), so it never alters JSON structure of clean output
    that callers re-parse with ``json.loads``.
    """
    if isinstance(result.stdout, str) and result.stdout:
        result.stdout = redact_diagnostic_text(result.stdout)
    if isinstance(result.stderr, str) and result.stderr:
        result.stderr = redact_diagnostic_text(result.stderr)
    return result


def run(args: list[str], *, check: bool = True, capture: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            args,
            capture_output=capture,
            text=True,
            check=check,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        # Redact secrets out of the raised error's captured output too, so a
        # failing doctl/ssh command cannot leak a token via an exception tail.
        if isinstance(exc.stdout, str):
            exc.stdout = redact_diagnostic_text(exc.stdout)
        if isinstance(exc.stderr, str):
            exc.stderr = redact_diagnostic_text(exc.stderr)
        raise
    return _redact_completed_process(completed)


def doctl(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return run(["doctl", *args], timeout=timeout)


def ssh_cmd(user: str, host: str, command: str, *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    _validate_ssh_user(user)
    _validate_host(host)
    return run(
        ["ssh", *DEFAULT_SSH_OPTS, "--", f"{user}@{host}", command],
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
    _validate_ssh_user(user)
    _validate_host(host)
    remote_argv = ["bash", "-s"]
    if script_args:
        remote_argv.extend(["--", *script_args])
    remote_cmd = build_remote_env_command(remote_argv, env_vars)
    with script_path.open("r") as f:
        return _redact_completed_process(
            subprocess.run(
                ["ssh", *DEFAULT_SSH_OPTS, "--", f"{user}@{host}", remote_cmd],
                stdin=f,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
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


def swimmers_port() -> str:
    return os.environ.get("SKILLBOX_SWIMMERS_PORT", DEFAULT_SWIMMERS_PORT).strip() or DEFAULT_SWIMMERS_PORT


def browser_url_for(host: str | None, *, port: str | None = None) -> str | None:
    value = str(host or "").strip()
    if not value:
        return None
    return f"http://{value}:{port or swimmers_port()}/"


def _check_public_ssh(box: "Box") -> dict[str, Any]:
    target = str(box.droplet_ip or "").strip()
    payload: dict[str, Any] = {"target": target or None, "ok": False}
    if not target:
        return payload
    try:
        result = ssh_cmd(box.ssh_user, target, "echo ok", timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload["error"] = str(exc)
        return payload
    payload["ok"] = result.returncode == 0 and "ok" in result.stdout
    if not payload["ok"] and result.stderr:
        payload["error"] = result.stderr[-200:]
    return payload


def _check_tailnet_ping(box: "Box") -> dict[str, Any]:
    target = str(box.tailscale_ip or box.tailscale_hostname or "").strip()
    payload: dict[str, Any] = {"target": target or None, "ok": False}
    if not target:
        return payload
    try:
        result = run(["tailscale", "ping", "--timeout=2s", "--c=1", target], check=False, timeout=5)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        payload["error"] = str(exc)
        return payload
    payload["ok"] = result.returncode == 0
    if not payload["ok"]:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            payload["error"] = detail[-200:]
    return payload


def _check_magicdns_resolution(hostname: str | None) -> dict[str, Any]:
    host = str(hostname or "").strip()
    payload: dict[str, Any] = {"hostname": host or None, "ok": False, "resolved_ip": None}
    if not host:
        return payload
    try:
        resolved_ip = socket.gethostbyname(host)
    except OSError as exc:
        payload["error"] = str(exc)
        return payload
    payload["ok"] = True
    payload["resolved_ip"] = resolved_ip
    return payload


def _check_port_reachability(host: str | None, port: str) -> dict[str, Any]:
    target = str(host or "").strip()
    payload: dict[str, Any] = {"target": target or None, "port": port, "ok": False}
    if not target:
        return payload
    try:
        with socket.create_connection((target, int(port)), timeout=2):
            payload["ok"] = True
    except (OSError, ValueError) as exc:
        payload["error"] = str(exc)
    return payload


def box_network_health(box: "Box") -> dict[str, Any]:
    port = swimmers_port()
    magicdns = _check_magicdns_resolution(box.tailscale_hostname)
    port_target = str(box.tailscale_ip or "").strip() or str(magicdns.get("resolved_ip") or "").strip()
    return {
        "public_ssh": _check_public_ssh(box),
        "tailnet_ping": _check_tailnet_ping(box),
        "magicdns_resolution": magicdns,
        "port_reachability": _check_port_reachability(port_target, port),
    }


def scp_file(local_path: Path, user: str, host: str, remote_path: str, *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    _validate_ssh_user(user)
    _validate_host(host)
    return run(
        ["scp", *DEFAULT_SSH_OPTS, "--", str(local_path), f"{user}@{host}:{remote_path}"],
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
    posture = resolve_network_posture(box)
    suppress_public_cache = posture == POSTURE_TAILNET_ONLY and not prefer_public

    ordered = [box.droplet_ip, box.tailscale_ip, box.tailscale_hostname] if prefer_public else [
        box.tailscale_ip,
        box.tailscale_hostname,
        box.droplet_ip,
    ]
    cached = str(getattr(box, "last_ssh_target", "") or "").strip()
    public_ip = str(box.droplet_ip or "").strip()
    if cached:
        if suppress_public_cache and public_ip and cached == public_ip:
            pass
        else:
            ordered = [cached, *ordered]
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
    candidates = box_ssh_candidates(box, prefer_public=prefer_public)
    if not candidates:
        return None

    cached = str(getattr(box, "last_ssh_target", "") or "").strip()
    remaining = candidates
    if cached and cached in candidates:
        if wait_for_ssh(cached, user=box.ssh_user, max_wait=max_wait, interval=interval):
            box.last_ssh_target = cached
            return cached
        remaining = [candidate for candidate in candidates if candidate != cached]

    if not remaining:
        box.last_ssh_target = None
        return None

    max_workers = min(3, len(remaining))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(
            executor.map(
                lambda target: wait_for_ssh(target, user=box.ssh_user, max_wait=max_wait, interval=interval),
                remaining,
            )
        )
    posture = resolve_network_posture(box)
    public_ip = str(box.droplet_ip or "").strip()
    for target, reachable in zip(remaining, results):
        if reachable:
            if posture == POSTURE_TAILNET_ONLY and public_ip and target == public_ip:
                box.last_ssh_target = None
            else:
                box.last_ssh_target = target
            return target
    box.last_ssh_target = None
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
    volume_size_gb: int | None = None


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


def _deploy_manifest_text(payload: dict[str, Any], key: str, label: str, resolved_manifest: Path) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Deploy manifest is missing {label}: {resolved_manifest}")
    return value


def _deploy_manifest_sha256(payload: dict[str, Any], key: str, label: str, resolved_manifest: Path) -> str:
    value = str(payload.get(key) or "").strip().lower()
    if not SHA256_HEX_PATTERN.fullmatch(value):
        raise RuntimeError(f"Deploy manifest has invalid {label}: {resolved_manifest}")
    return value


def _deploy_manifest_archive_path(payload: dict[str, Any], resolved_manifest: Path) -> Path:
    archive_rel = _deploy_manifest_text(payload, "archive", "archive path", resolved_manifest)
    archive_path = (resolved_manifest.parent / archive_rel).resolve()
    if not archive_path.is_file():
        raise RuntimeError(f"Deploy archive not found: {archive_path}")
    return archive_path


def _deploy_manifest_active_profiles(payload: dict[str, Any], resolved_manifest: Path) -> list[str]:
    raw_active_profiles = payload.get("active_profiles")
    if raw_active_profiles is None:
        return []
    if not isinstance(raw_active_profiles, list):
        raise RuntimeError(f"Deploy manifest has invalid active_profiles: {resolved_manifest}")

    active_profiles: list[str] = []
    seen_profiles: set[str] = set()
    for raw_profile in raw_active_profiles:
        profile = str(raw_profile).strip()
        if profile and profile not in seen_profiles:
            seen_profiles.add(profile)
            active_profiles.append(profile)
    return active_profiles


def load_deploy_manifest(manifest_path: Path, *, expected_client_id: str | None = None) -> DeployRelease:
    resolved_manifest = manifest_path.expanduser().resolve()
    if not resolved_manifest.is_file():
        raise RuntimeError(f"Deploy manifest not found: {resolved_manifest}")

    try:
        payload = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Deploy manifest is not valid JSON: {resolved_manifest}") from exc

    client_id = _deploy_manifest_text(payload, "client_id", "client_id", resolved_manifest)
    if expected_client_id is not None and client_id != expected_client_id:
        raise RuntimeError(
            f"Deploy manifest {resolved_manifest} is for client {client_id!r}, not {expected_client_id!r}"
        )

    source_commit = _deploy_manifest_text(payload, "source_commit", "source_commit", resolved_manifest)
    payload_tree_sha256 = _deploy_manifest_sha256(payload, "payload_tree_sha256", "payload_tree_sha256", resolved_manifest)
    archive_path = _deploy_manifest_archive_path(payload, resolved_manifest)
    archive_sha256 = _deploy_manifest_sha256(payload, "archive_sha256", "archive_sha256", resolved_manifest)
    actual_archive_sha256 = sha256_file(archive_path)
    if actual_archive_sha256 != archive_sha256:
        raise RuntimeError(
            f"Deploy archive hash mismatch for {archive_path}: expected {archive_sha256}, got {actual_archive_sha256}"
        )

    return DeployRelease(
        manifest_path=resolved_manifest,
        client_id=client_id,
        source_commit=source_commit,
        payload_tree_sha256=payload_tree_sha256,
        archive_path=archive_path,
        archive_sha256=archive_sha256,
        active_profiles=_deploy_manifest_active_profiles(payload, resolved_manifest),
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


# ---------------------------------------------------------------------------
# Network posture policy
# ---------------------------------------------------------------------------

POSTURE_TAILNET_ONLY = "tailnet_only"
POSTURE_PUBLIC = "public"
POSTURE_UNMANAGED = "unmanaged"
VALID_POSTURES = {POSTURE_TAILNET_ONLY, POSTURE_PUBLIC, POSTURE_UNMANAGED}

EXPOSURE_WILDCARD_DIRECT = "wildcard-direct"
EXPOSURE_TAILNET_DIRECT = "tailnet-direct"
EXPOSURE_INGRESS_ROUTED = "ingress-routed"
EXPOSURE_LOOPBACK_ONLY = "loopback-only"


def resolve_network_posture(box: "Box") -> str:
    """Return the effective network posture for a box.

    Old inventory entries without the field get a safe default:
    managed boxes default to tailnet_only, external boxes to unmanaged.
    """
    explicit = str(box.network_posture or "").strip()
    if explicit in VALID_POSTURES:
        return explicit
    if box.management_mode == "external":
        return POSTURE_UNMANAGED
    return POSTURE_TAILNET_ONLY


def posture_allows_public_ssh(posture: str) -> bool:
    return posture == POSTURE_PUBLIC


def posture_requires_cloud_firewall(posture: str) -> bool:
    return posture == POSTURE_TAILNET_ONLY


def posture_requires_host_ssh_lockdown(posture: str) -> bool:
    return posture == POSTURE_TAILNET_ONLY


def posture_allows_exposure(posture: str, exposure: str) -> bool:
    """Return whether the given app exposure classification is allowed under this posture."""
    if posture == POSTURE_UNMANAGED:
        return True
    if posture == POSTURE_PUBLIC:
        return True
    # tailnet_only: wildcard-direct is a violation
    return exposure != EXPOSURE_WILDCARD_DIRECT


def evaluate_posture_violations(
    box: "Box",
    *,
    network_checks: dict[str, Any] | None = None,
    app_exposures: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compare observed network state against desired posture and return violations."""
    posture = resolve_network_posture(box)
    violations: list[dict[str, Any]] = []

    if posture == POSTURE_UNMANAGED:
        return violations

    if network_checks:
        public_ssh = network_checks.get("public_ssh", {})
        if public_ssh.get("ok") and not posture_allows_public_ssh(posture):
            violations.append({
                "type": "public_ssh_reachable",
                "severity": "error",
                "message": f"SSH is publicly reachable at {public_ssh.get('target')} but posture is {posture}",
                "posture": posture,
            })

    if posture_requires_cloud_firewall(posture) and not box.cloud_firewall_id:
        violations.append({
            "type": "cloud_firewall_missing",
            "severity": "warning",
            "message": "No cloud firewall is associated with this box",
            "posture": posture,
        })

    for app in (app_exposures or []):
        exposure = str(app.get("exposure", ""))
        if not posture_allows_exposure(posture, exposure):
            violations.append({
                "type": "app_exposure_violation",
                "severity": "error",
                "message": (
                    f"{app.get('service_id', 'unknown')} uses {exposure} "
                    f"which violates {posture} posture"
                ),
                "posture": posture,
                "service_id": app.get("service_id"),
                "exposure": exposure,
            })

    return violations


def active_profiles_for_release(release: DeployRelease | None) -> list[str]:
    profiles = release.active_profiles if release is not None else []
    return sorted(dict.fromkeys(["core", *profiles]))


def remote_box_contract_payload(context: "BoxUpContext") -> dict[str, Any]:
    state_root = str(context.box.state_root or (context.profile.storage.mount_path if context.profile.storage else "")).strip()
    ssh_user = str(getattr(context.profile, "ssh_user", "") or "skillbox").strip()
    storage_filesystem = str(
        context.box.storage_filesystem
        or (context.profile.storage.filesystem if context.profile.storage else "")
    ).strip()
    storage_min_free_gb = context.box.storage_min_free_gb
    if storage_min_free_gb is None and context.profile.storage is not None:
        storage_min_free_gb = context.profile.storage.min_free_gb

    env_updates: dict[str, str] = {}
    env_updates.update({
        "SKILLBOX_BOX_ID": context.box_id,
        "SKILLBOX_BOX_SELF": "true",
        "SKILLBOX_BOX_TAILSCALE_HOSTNAME": context.ts_hostname,
        "SKILLBOX_HOST_HOME_ROOT": f"/home/{ssh_user}",
    })
    if context.box.tailscale_ip:
        env_updates["SKILLBOX_BOX_TAILSCALE_IP"] = context.box.tailscale_ip
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
            "SKILLBOX_MONOSERVER_HOST_ROOT": f"{state_root.rstrip('/')}/repos",
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
            expose_enabled = os.environ.get("SKILLBOX_SWIMMERS_EXPOSE") == "1"
            if not expose_enabled or not token:
                raise RuntimeError(
                    "Swimmers profile requires public bind (0.0.0.0) but safety "
                    "prerequisites are not met. Set SKILLBOX_SWIMMERS_EXPOSE=1 and "
                    "provide a non-empty SKILLBOX_SWIMMERS_AUTH_TOKEN to proceed."
                )
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
    if storage.volume_size_gb is not None:
        return storage.volume_size_gb
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

    volume_size_gb: int | None = None
    if "volume_size_gb" in raw_storage:
        volume_size_raw = raw_storage.get("volume_size_gb")
        try:
            volume_size_gb = int(volume_size_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Box profile {profile_id!r} storage.volume_size_gb must be an integer") from exc
        if volume_size_gb < 1:
            raise RuntimeError(f"Box profile {profile_id!r} storage.volume_size_gb must be positive")
        if volume_size_gb < int(math.ceil(min_free_gb)):
            raise RuntimeError(
                f"Box profile {profile_id!r} storage.volume_size_gb must be >= storage.min_free_gb"
            )

    if "required" in raw_storage:
        required = _validate_config_bool(
            raw_storage["required"],
            f"Box profile {profile_id!r} storage.required",
        )
    else:
        required = True

    return BoxProfileStorage(
        provider=storage_provider,
        mount_path=mount_path,
        filesystem=filesystem,
        required=required,
        min_free_gb=min_free_gb,
        volume_size_gb=volume_size_gb,
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
    name = _validate_profile_name(name)
    profile_id = name
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
        if path.suffix == ".yaml":
            profile_id = path.stem

    if yaml_mod is None:
        raise RuntimeError("PyYAML is required to load box profiles: pip install pyyaml")

    data = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a YAML mapping in {path}")

    provider = str(data.get("provider", "digitalocean"))
    storage = parse_box_profile_storage(
        profile_id=profile_id,
        profile_provider=provider,
        raw_storage=data.get("storage"),
    )

    return BoxProfile(
        id=profile_id,
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
    last_ssh_target: str | None = None
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
    network_posture: str = ""
    cloud_firewall_id: str | None = None


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


def inventory_ssh_target_snapshot(boxes: list[Box]) -> dict[str, str | None]:
    return {box.id: box.last_ssh_target for box in boxes}


def persist_inventory_if_ssh_targets_changed(boxes: list[Box], before: dict[str, str | None]) -> None:
    if inventory_ssh_target_snapshot(boxes) == before:
        return
    if inventory_path().is_file():
        save_inventory(boxes)


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


# Bounded read-after-delete confirmation for DigitalOcean eventual consistency.
# We never spin: a single delete call followed by CONFIRM_DROPLET_ABSENT_ATTEMPTS
# bounded reads (with backoff), then a truthful pending state if still listed.
CONFIRM_DROPLET_ABSENT_ATTEMPTS = 3
CONFIRM_DROPLET_ABSENT_BACKOFF_SECONDS = 2.0


def confirm_droplet_absent(
    droplet_id: str,
    *,
    attempts: int = CONFIRM_DROPLET_ABSENT_ATTEMPTS,
    backoff_seconds: float = CONFIRM_DROPLET_ABSENT_BACKOFF_SECONDS,
    sleep: Any = time.sleep,
) -> bool:
    """Read-after-delete confirmation that a droplet is truly gone.

    Returns True only after `doctl compute droplet get` reports the droplet is
    absent (404 / empty result -> do_get_droplet returns None). If the droplet is
    still listed after a bounded number of attempts, returns False so the caller
    keeps the inventory in a truthful pending state instead of lying `destroyed`.
    A read error (doctl failure that is not a clean not-found) is treated as
    *not confirmed* — we never assume absence we did not observe.
    """
    if not str(droplet_id or "").strip():
        # Nothing to confirm; absence is vacuously true.
        return True
    last_attempt = max(1, attempts)
    for attempt in range(1, last_attempt + 1):
        try:
            droplet = do_get_droplet(droplet_id)
        except Exception:
            droplet = {"_confirm_read_error": True}
        if droplet is None:
            return True
        if attempt < last_attempt:
            sleep(backoff_seconds * attempt)
    return False


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


def do_detach_volume(volume_id: str, droplet_id: str) -> bool:
    result = run(
        ["doctl", "compute", "volume-action", "detach", volume_id, droplet_id, "--wait"],
        check=False,
        timeout=300,
    )
    return result.returncode == 0


def do_delete_volume(volume_id: str) -> bool:
    result = run(
        ["doctl", "compute", "volume", "delete", volume_id, "--force"],
        check=False,
        timeout=300,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# DigitalOcean firewall lifecycle
# ---------------------------------------------------------------------------

def do_create_firewall(
    name: str,
    droplet_ids: list[str],
    *,
    allow_ssh_cidrs: list[str] | None = None,
) -> dict[str, Any]:
    """Create a DO firewall with bootstrap or lockdown rules."""
    inbound = []
    if allow_ssh_cidrs:
        inbound.append(f"protocol:tcp,ports:22,address:{','.join(allow_ssh_cidrs)}")
    inbound.append("protocol:udp,ports:41641,address:0.0.0.0/0,address:::/0")
    result = doctl(
        "compute", "firewall", "create",
        "--name", name,
        "--droplet-ids", ",".join(droplet_ids),
        "--inbound-rules", ";".join(inbound),
        "--outbound-rules",
        "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0;"
        "protocol:udp,ports:all,address:0.0.0.0/0,address:::/0;"
        "protocol:icmp,address:0.0.0.0/0,address:::/0",
        "--output", "json",
        timeout=120,
    )
    firewalls = json.loads(result.stdout or "[]")
    if not firewalls:
        raise RuntimeError(f"doctl returned empty result when creating firewall {name}")
    return firewalls[0] if isinstance(firewalls, list) else firewalls


def do_update_firewall_lockdown(firewall_id: str, name: str, droplet_ids: list[str]) -> dict[str, Any]:
    """Update firewall to lockdown rules: no public SSH, only Tailscale UDP."""
    result = doctl(
        "compute", "firewall", "update", firewall_id,
        "--name", name,
        "--droplet-ids", ",".join(droplet_ids),
        "--inbound-rules",
        "protocol:udp,ports:41641,address:0.0.0.0/0,address:::/0",
        "--outbound-rules",
        "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0;"
        "protocol:udp,ports:all,address:0.0.0.0/0,address:::/0;"
        "protocol:icmp,address:0.0.0.0/0,address:::/0",
        "--output", "json",
        timeout=120,
    )
    firewalls = json.loads(result.stdout or "[]")
    if not firewalls:
        raise RuntimeError(f"doctl returned empty result when updating firewall {firewall_id}")
    return firewalls[0] if isinstance(firewalls, list) else firewalls


def do_delete_firewall(firewall_id: str) -> bool:
    result = run(
        ["doctl", "compute", "firewall", "delete", firewall_id, "--force"],
        check=False,
        timeout=120,
    )
    return result.returncode == 0


def do_get_firewall(firewall_id: str) -> dict[str, Any] | None:
    result = run(
        ["doctl", "compute", "firewall", "get", firewall_id, "--output", "json"],
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    firewalls = json.loads(result.stdout or "[]")
    return firewalls[0] if firewalls else None


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
    effective_blueprint: str | None = None
    set_args: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    ip: str | None = None
    ssh_target: str | None = None


@dataclass(frozen=True)
class BoxUpStage:
    name: str
    error_type: str
    action: Any
    failure_state: str | None = None
    next_actions: list[str] | None = None


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


def _run_box_up_stages(context: BoxUpContext, stages: list[BoxUpStage]) -> bool:
    for stage in stages:
        if not _run_box_up_stage(
            context,
            stage_name=stage.name,
            error_type=stage.error_type,
            action=stage.action,
            failure_state=stage.failure_state,
            next_actions=stage.next_actions,
        ):
            return False
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
    _record_box_up_step(context, "lockdown", "skip", "would verify host and cloud firewall posture")
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
        "credential_status": provisioning_credentials_status(),
        "storage": storage_payload(context.profile.storage),
        "volume": volume_payload(context.box),
        "next_actions": provisioning_credentials_next_actions(
            missing_env_vars(PROVISIONING_ENV_VARS),
            box_id=context.box_id,
            profile_name=context.profile_name,
        ),
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
    droplet_id_str = str(droplet["id"])
    posture = resolve_network_posture(context.box)
    fw_id: str | None = None
    if posture_requires_cloud_firewall(posture):
        try:
            fw = do_create_firewall(
                f"skillbox-{context.box_id}",
                [droplet_id_str],
                allow_ssh_cidrs=["0.0.0.0/0", "::/0"],
            )
            fw_id = str(fw.get("id") or "")
            context.steps.append({
                "stage": "cloud_firewall_bootstrap",
                "firewall_id": fw_id,
                "posture": posture,
            })
        except Exception as exc:
            context.steps.append({
                "stage": "cloud_firewall_bootstrap",
                "error": str(exc)[:200],
                "posture": posture,
            })
    update_box(
        context.box,
        droplet_id=droplet_id_str,
        droplet_ip=ip,
        cloud_firewall_id=fw_id,
        state="bootstrapping",
    )
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
    posture = resolve_network_posture(context.box)
    tailnet_only_ssh = "true" if posture_requires_host_ssh_lockdown(posture) else "false"
    result = ssh_script(
        "root",
        context.ip,
        TAILSCALE_SCRIPT,
        {
            "TAILSCALE_AUTHKEY": ts_authkey,
            "TAILSCALE_HOSTNAME": context.ts_hostname,
            "SSH_LOGIN_USER": context.profile.ssh_user,
            "TAILNET_ONLY_SSH": tailnet_only_ssh,
        },
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Tailscale enrollment failed (exit {result.returncode}): {result.stderr[-500:]}")
    ts_ip = extract_tailscale_ipv4(result.stdout)
    if not ts_ip:
        ts_ip_result = ssh_cmd("root", context.ip, "tailscale ip -4", timeout=15)
        ts_ip = ts_ip_result.stdout.strip().split("\n")[0] if ts_ip_result.returncode == 0 else None
    update_box(context.box, tailscale_ip=ts_ip, state="lockdown")
    save_inventory(context.boxes)
    if posture_requires_host_ssh_lockdown(posture):
        ufw_check = ssh_cmd("root", context.ip, "ufw status numbered 2>/dev/null || true", timeout=15)
        context.steps.append({
            "stage": "host_firewall_verify",
            "posture": posture,
            "tailnet_only_ssh": tailnet_only_ssh,
            "ufw_output": ufw_check.stdout[:500] if ufw_check.returncode == 0 else "ufw check failed",
        })
    if posture_requires_cloud_firewall(posture) and context.box.cloud_firewall_id:
        droplet_id_str = str(context.box.droplet_id or "")
        try:
            do_update_firewall_lockdown(
                context.box.cloud_firewall_id,
                f"skillbox-{context.box_id}",
                [droplet_id_str],
            )
            context.steps.append({
                "stage": "cloud_firewall_lockdown",
                "firewall_id": context.box.cloud_firewall_id,
                "posture": posture,
            })
        except Exception as exc:
            context.steps.append({
                "stage": "cloud_firewall_lockdown",
                "error": str(exc)[:200],
                "posture": posture,
            })
    update_box(context.box, state="deploying")
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


def _operator_swimmers_verify_target(context: BoxUpContext) -> tuple[list[str], str | None]:
    active_profiles = active_profiles_for_release(context.deploy_release)
    if "swimmers" not in active_profiles:
        return active_profiles, None

    ts_ip = str(context.box.tailscale_ip or "").strip()
    if not ts_ip:
        raise RuntimeError("Cannot verify swimmers from operator side without a Tailscale IP.")
    return active_profiles, ts_ip


def _operator_swimmers_auth_for_box(box_id: str) -> tuple[str, str]:
    token, token_source = local_swimmers_auth_token(box_id)
    if not token:
        raise RuntimeError(
            "Cannot verify swimmers from operator side without "
            f"SKILLBOX_SWIMMERS_AUTH_TOKEN or {derived_swimmers_auth_token_env(box_id)}."
        )
    return token, token_source


def _operator_swimmers_sessions_payload(url: str, token: str) -> dict[str, Any]:
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
    return payload


def _verify_operator_swimmers_surface(context: BoxUpContext) -> dict[str, Any]:
    active_profiles, ts_ip = _operator_swimmers_verify_target(context)
    if ts_ip is None:
        return {"skipped": "no swimmers profile", "active_profiles": active_profiles}

    token, token_source = _operator_swimmers_auth_for_box(context.box_id)
    port = swimmers_port()
    url = f"http://{ts_ip}:{port}/v1/sessions"
    payload = _operator_swimmers_sessions_payload(url, token)
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
    phone_url = browser_url_for(context.box.tailscale_ip)
    payload = {
        "box_id": context.box_id,
        "profile": asdict(context.profile),
        "dry_run": False,
        "droplet_id": context.box.droplet_id,
        "droplet_ip": context.box.droplet_ip,
        "tailscale_hostname": context.ts_hostname,
        "tailscale_ip": context.box.tailscale_ip,
        "phone_url": phone_url,
        "browser_url": phone_url,
        "magicdns_url": browser_url_for(context.ts_hostname),
        "ssh": f"ssh {context.profile.ssh_user}@{ssh_target}" if ssh_target else None,
        "steps": context.steps,
        "storage": storage_payload(context.profile.storage),
        "volume": volume_payload(context.box),
        "next_actions": [f"box ssh {context.box_id}", f"box status {context.box_id}"],
    }
    if context.deploy_release is not None:
        payload["deploy_release"] = deploy_release_payload(context.deploy_release)
    return payload


def _box_up_success_ssh_target(context: BoxUpContext, *, resumed: bool) -> str | None:
    if resumed:
        return context.box.tailscale_ip or context.box.tailscale_hostname or context.box.droplet_ip
    return context.ts_hostname


def _emit_box_up_success(context: BoxUpContext, *, resumed: bool = False) -> int:
    update_box(context.box, state="ready")
    save_inventory(context.boxes)
    payload = _box_up_success_payload(context)
    if resumed:
        payload["resumed"] = True
    if context.is_json:
        emit_json(payload)
    else:
        print()
        print(f"Box {context.box_id} is ready.")
        ssh_target = _box_up_success_ssh_target(context, resumed=resumed)
        print(f"  SSH: ssh {context.profile.ssh_user}@{ssh_target}")
        if not resumed:
            print(f"  IP:  {context.box.droplet_ip} (public) / {context.box.tailscale_ip or 'pending'} (tailscale)")
            if context.box.state_root:
                print(f"  State root: {context.box.state_root} ({context.box.storage_filesystem or 'unknown fs'})")
    return EXIT_OK


def _emit_resumed_box_up_dry_run(context: BoxUpContext) -> int:
    _record_box_up_step(context, "create", "skip", f"would resume droplet {context.box.droplet_id or 'unknown'}")
    _record_box_up_step(context, "storage", "skip", "would reuse attached state root")
    _record_box_up_step(context, "bootstrap", "skip", "would reuse existing host")
    _record_box_up_step(context, "ssh-ready", "skip", "would verify existing SSH")
    _record_box_up_step(context, "enroll", "skip", "would enroll only if Tailscale IP is missing")
    _record_box_up_step(context, "lockdown", "skip", "would verify host and cloud firewall posture")
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
    if context.is_json:
        emit_json(payload)
    return EXIT_OK


def _run_resumed_enroll_stage(context: BoxUpContext) -> bool:
    if context.box.tailscale_ip:
        _record_box_up_step(context, "enroll", "skip", f"already enrolled at {context.box.tailscale_ip}")
        return True
    try:
        ts_authkey = require_env("SKILLBOX_TS_AUTHKEY")
    except RuntimeError as exc:
        _record_box_up_step(context, "enroll", "fail", str(exc))
        _emit_box_up_failure(
            context,
            error_type="tailscale_auth_missing",
            message=str(exc),
            next_actions=[f"box status {context.box_id}", f"box ssh {context.box_id}"],
        )
        return False
    return _run_box_up_stage(
        context,
        stage_name="enroll",
        error_type="tailscale_failed",
        action=lambda: _enroll_box_tailscale(context, ts_authkey=ts_authkey),
        failure_state="ssh-ready",
        next_actions=[f"box ssh {context.box_id}", f"box down {context.box_id}"],
    )


def _remaining_box_up_stages(context: BoxUpContext, *, deploy_down_action: str) -> list[BoxUpStage]:
    box_id = context.box_id
    return [
        BoxUpStage(
            "deploy",
            "deploy_failed",
            lambda: _deploy_box_runtime(context),
            "ssh-ready",
            [f"box ssh {box_id}", deploy_down_action],
        ),
        BoxUpStage(
            "contract",
            "remote_contract_failed",
            lambda: _patch_remote_runtime_contract(context),
            "ssh-ready",
            [f"box ssh {box_id}", f"box status {box_id}"],
        ),
        BoxUpStage(
            "launch",
            "remote_launch_failed",
            lambda: _launch_remote_workspace(context),
            "acceptance",
            [f"box ssh {box_id}", f"box status {box_id}"],
        ),
        BoxUpStage(
            "first-box",
            "first_box_failed",
            lambda: _run_box_first_box(context, blueprint=context.effective_blueprint, set_args=context.set_args),
            "ssh-ready",
            [f"box ssh {box_id}", f"box status {box_id}"],
        ),
        BoxUpStage(
            "verify",
            "operator_verify_failed",
            lambda: _verify_operator_swimmers_surface(context),
            "ssh-ready",
            [f"box status {box_id}", f"box ssh {box_id}"],
        ),
    ]


def _run_resumed_box_up(
    context: BoxUpContext,
    *,
    blueprint: str | None,
    set_args: list[str],
) -> int:
    box_id = context.box_id
    context.effective_blueprint = blueprint
    context.set_args = set_args
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

    if not _run_resumed_enroll_stage(context):
        return EXIT_ERROR
    if not _run_box_up_stages(context, _remaining_box_up_stages(context, deploy_down_action=f"box status {box_id}")):
        return EXIT_ERROR
    return _emit_box_up_success(context, resumed=True)


def _load_box_up_profile(profile_name: str, *, is_json: bool) -> BoxProfile | None:
    try:
        return load_profile(profile_name)
    except RuntimeError as exc:
        emit_error_or_print(str(exc), is_json=is_json, error_type="profile_not_found")
        return None


def _reject_box_up_inventory_state(
    *,
    box_id: str,
    profile_name: str,
    existing: Box | None,
    resume: bool,
    is_json: bool,
) -> bool:
    if existing and existing.state not in ("destroyed",) and not resume:
        msg = (
            f"Box {box_id!r} already exists in state {existing.state!r}. "
            "Use 'box up --resume' for a partial provision, 'box down' first, or choose a different id."
        )
        emit_error_or_print(
            msg,
            is_json=is_json,
            error_type="conflict",
            next_actions=[
                f"box up {box_id} --profile {profile_name} --deploy-manifest <path> --resume",
                f"box down {box_id}",
                f"box status {box_id}",
            ],
        )
        return True
    if resume and (existing is None or existing.state == "destroyed"):
        emit_error_or_print(
            f"Box {box_id!r} has no resumable inventory entry.",
            is_json=is_json,
            error_type="not_found",
            next_actions=["box list"],
        )
        return True
    if resume and existing and existing.state not in RESUMABLE_UP_STATES:
        msg = (
            f"Box {box_id!r} cannot resume from state {existing.state!r}; "
            f"resumable states are: {', '.join(sorted(RESUMABLE_UP_STATES))}."
        )
        emit_error_or_print(
            msg,
            is_json=is_json,
            error_type="invalid_state",
            next_actions=[f"box status {box_id}", f"box down {box_id}"],
        )
        return True
    if resume and existing and existing.profile != profile_name:
        emit_error_or_print(
            f"Box {box_id!r} uses profile {existing.profile!r}, not {profile_name!r}.",
            is_json=is_json,
            error_type="profile_mismatch",
            next_actions=[f"box up {box_id} --profile {existing.profile} --deploy-manifest <path> --resume"],
        )
        return True
    return False


def _load_box_up_deploy_release(
    box_id: str,
    *,
    deploy_manifest: str | None,
    profile_name: str,
    dry_run: bool,
    is_json: bool,
) -> tuple[bool, DeployRelease | None]:
    if deploy_manifest:
        try:
            return True, load_deploy_manifest(Path(deploy_manifest), expected_client_id=box_id)
        except RuntimeError as exc:
            emit_error_or_print(str(exc), is_json=is_json, error_type="deploy_manifest_invalid")
            return False, None
    if dry_run:
        return True, None

    msg = (
        "box up requires --deploy-manifest for non-dry-run launches. "
        "Branch-based deploys are not allowed for remote provisioning."
    )
    emit_error_or_print(
        msg,
        is_json=is_json,
        error_type="deploy_manifest_required",
        next_actions=[f"box up {box_id} --profile {profile_name} --deploy-manifest <path>"],
    )
    return False, None


def _ensure_box_up_storage(profile: BoxProfile, *, box_id: str, profile_name: str, is_json: bool) -> bool:
    try:
        require_profile_storage(profile)
    except RuntimeError as exc:
        emit_error_or_print(
            str(exc),
            is_json=is_json,
            error_type="storage_layout_missing",
            next_actions=["box profiles --format json", f"box up {box_id} --profile {profile_name} --dry-run"],
        )
        return False
    return True


def _box_up_credentials(box_id: str, *, profile_name: str, is_json: bool) -> tuple[str, str, str] | None:
    if emit_provisioning_credentials_error(box_id=box_id, profile_name=profile_name, is_json=is_json) != EXIT_OK:
        return None
    return (
        require_env("SKILLBOX_DO_TOKEN"),
        require_env("SKILLBOX_DO_SSH_KEY_ID"),
        require_env("SKILLBOX_TS_AUTHKEY"),
    )


def _new_box_up_stages(context: BoxUpContext, *, ssh_key_id: str, ts_authkey: str) -> list[BoxUpStage]:
    box_id = context.box_id
    return [
        BoxUpStage("create", "droplet_create_failed", lambda: _create_box_droplet(context, ssh_key_id=ssh_key_id)),
        BoxUpStage(
            "storage",
            "storage_attach_failed",
            lambda: _ensure_box_storage(context),
            "bootstrapping",
            [f"box down {box_id}", f"box status {box_id}"],
        ),
        BoxUpStage(
            "bootstrap",
            "bootstrap_failed",
            lambda: _bootstrap_box_host(context),
            "bootstrapping",
            [f"box down {box_id}"],
        ),
        BoxUpStage(
            "ssh-ready",
            "ssh_access_failed",
            lambda: _mark_box_ssh_ready(context),
            "bootstrapping",
            [f"box down {box_id}", f"ssh {context.profile.ssh_user}@<public-ip>"],
        ),
        BoxUpStage(
            "enroll",
            "tailscale_failed",
            lambda: _enroll_box_tailscale(context, ts_authkey=ts_authkey),
            "ssh-ready",
            [f"box ssh {box_id}", f"box down {box_id}"],
        ),
        *_remaining_box_up_stages(context, deploy_down_action=f"box down {box_id}"),
    ]


def _run_new_box_up(context: BoxUpContext) -> int:
    credentials = _box_up_credentials(context.box_id, profile_name=context.profile_name, is_json=context.is_json)
    if credentials is None:
        return EXIT_ERROR
    do_token, ssh_key_id, ts_authkey = credentials
    os.environ["DIGITALOCEAN_ACCESS_TOKEN"] = do_token
    context.boxes = [candidate for candidate in context.boxes if candidate.id != context.box_id]
    if not _run_box_up_stages(context, _new_box_up_stages(context, ssh_key_id=ssh_key_id, ts_authkey=ts_authkey)):
        return EXIT_ERROR
    return _emit_box_up_success(context)


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

    profile = _load_box_up_profile(profile_name, is_json=is_json)
    if profile is None:
        return EXIT_ERROR
    profile_name = profile.id

    boxes = load_inventory()
    existing = find_box(boxes, box_id)
    if _reject_box_up_inventory_state(
        box_id=box_id,
        profile_name=profile_name,
        existing=existing,
        resume=resume,
        is_json=is_json,
    ):
        return EXIT_ERROR

    deploy_ok, deploy_release = _load_box_up_deploy_release(
        box_id,
        deploy_manifest=deploy_manifest,
        profile_name=profile_name,
        dry_run=dry_run,
        is_json=is_json,
    )
    if not deploy_ok:
        return EXIT_ERROR

    if resume and existing is not None:
        context = _build_box_resume_context(
            existing=existing,
            profile=profile,
            boxes=boxes,
            is_json=is_json,
            deploy_release=deploy_release,
        )
        context.effective_blueprint = effective_blueprint
        context.set_args = set_args
        if dry_run:
            return _emit_resumed_box_up_dry_run(context)
        return _run_resumed_box_up(context, blueprint=effective_blueprint, set_args=set_args)

    if not dry_run and not _ensure_box_up_storage(profile, box_id=box_id, profile_name=profile_name, is_json=is_json):
        return EXIT_ERROR

    context = _build_box_up_context(
        box_id=box_id,
        profile_name=profile_name,
        profile=profile,
        boxes=boxes,
        is_json=is_json,
        deploy_release=deploy_release,
    )
    context.effective_blueprint = effective_blueprint
    context.set_args = set_args

    if dry_run:
        payload = _box_up_dry_run_payload(context)
        if is_json:
            emit_json(payload)
        return EXIT_OK

    return _run_new_box_up(context)


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

def _box_down_step(steps: list[dict[str, Any]], is_json: bool, name: str, status: str, detail: Any = None) -> None:
    _record_box_step(steps, is_json, name, status, detail)


def _emit_box_down_dry_run(box: Box, box_id: str, steps: list[dict[str, Any]], *, is_json: bool) -> int:
    _box_down_step(steps, is_json, "drain", "skip", "dry-run")
    _box_down_step(steps, is_json, "remove", "skip", "dry-run")
    _box_down_step(steps, is_json, "destroy", "skip", f"would destroy droplet {box.droplet_id}")
    if box.volume_id:
        volume_label = box.volume_name or box.volume_id
        _box_down_step(steps, is_json, "volume", "skip", f"would detach/delete volume {volume_label}")
    else:
        _box_down_step(steps, is_json, "volume", "skip", "no volume id")
    payload: dict[str, Any] = {"box_id": box_id, "dry_run": True, "steps": steps, "next_actions": [f"box down {box_id}"]}
    if is_json:
        emit_json(payload)
    return EXIT_OK


def _drain_box_for_down(box: Box, steps: list[dict[str, Any]], *, is_json: bool) -> str | None:
    ssh_target = resolve_box_ssh_target(box, max_wait=5, interval=1, prefer_public=box.state == "ssh-ready")
    if ssh_target and box.state == "ready":
        try:
            if not is_json:
                print(f"[...] drain  Stopping services on {ssh_target}...")
            result = ssh_cmd(box.ssh_user, ssh_target, "cd ~/skillbox && make down", timeout=60)
            _box_down_step(steps, is_json, "drain", "ok" if result.returncode == 0 else "warn")
        except Exception:
            _box_down_step(steps, is_json, "drain", "warn", "SSH unreachable, skipping drain")
    else:
        _box_down_step(steps, is_json, "drain", "skip", f"box in state {box.state}")
    return ssh_target


def _remove_box_from_tailnet(box: Box, ssh_target: str | None, steps: list[dict[str, Any]], *, is_json: bool) -> None:
    if ssh_target:
        try:
            if not is_json:
                print("[...] remove  Removing from tailnet...")
            result = ssh_cmd("root", box.droplet_ip or ssh_target, "tailscale logout", timeout=30)
            detail = result.stderr[-500:] or result.stdout[-500:] or None
            _box_down_step(steps, is_json, "remove", "ok" if result.returncode == 0 else "warn", detail)
        except Exception:
            _box_down_step(steps, is_json, "remove", "warn", "Could not remove from tailnet")
    else:
        _box_down_step(steps, is_json, "remove", "skip", "no ssh target")


def _cleanup_box_firewall(box: Box, steps: list[dict[str, Any]], *, is_json: bool) -> bool:
    fw_id = str(box.cloud_firewall_id or "").strip()
    if not fw_id:
        _box_down_step(steps, is_json, "firewall", "skip", "no cloud firewall id")
        return True
    try:
        if not is_json:
            print(f"[...] firewall  Deleting cloud firewall {fw_id}...")
        if do_delete_firewall(fw_id):
            _box_down_step(steps, is_json, "firewall", "ok", f"firewall {fw_id} deleted")
            return True
        _box_down_step(steps, is_json, "firewall", "fail", "doctl delete returned non-zero")
    except Exception as exc:
        _box_down_step(steps, is_json, "firewall", "fail", str(exc))
    return False


DESTROY_CONFIRMED_ABSENT = "confirmed-absent"
DESTROY_PENDING = "destroy-pending"
DESTROY_FAILED = "delete-failed"


def _destroy_box_droplet(box: Box, steps: list[dict[str, Any]], *, is_json: bool) -> str:
    """Delete the droplet, then CONFIRM absence via read-after-delete.

    Returns one of:
      - DESTROY_CONFIRMED_ABSENT: doctl delete succeeded AND a follow-up
        `doctl compute droplet get` reported the droplet gone (404/empty). Only
        this result lets the caller write the `destroyed` state.
      - DESTROY_PENDING: delete succeeded but the droplet is still API-listed
        after a bounded confirm-retry; inventory must stay truthful (the droplet
        may still bill). Caller parks the box in `destroy-pending`.
      - DESTROY_FAILED: the delete call itself failed (non-zero / exception);
        inventory state is preserved for a clean retry.

    The skip-without-droplet branch returns CONFIRMED_ABSENT: there is no droplet
    to bill, so absence is vacuously confirmed.
    """
    if not box.droplet_id:
        _box_down_step(steps, is_json, "destroy", "skip", "no droplet id")
        return DESTROY_CONFIRMED_ABSENT

    try:
        if not is_json:
            print(f"[...] destroy  Deleting droplet {box.droplet_id}...")
        deleted = do_delete_droplet(box.droplet_id)
    except Exception as exc:
        _box_down_step(steps, is_json, "destroy", "fail", str(exc))
        return DESTROY_FAILED

    if not deleted:
        _box_down_step(steps, is_json, "destroy", "fail", "doctl delete returned non-zero")
        return DESTROY_FAILED

    _box_down_step(steps, is_json, "destroy", "ok", f"droplet {box.droplet_id} delete requested")

    # Read-after-delete: never trust the delete exit code alone. A delete that
    # succeeds but leaves the droplet listed would otherwise become the most
    # expensive lie — an inventory that says `destroyed` while DO still bills.
    if not is_json:
        print(f"[...] confirm  Verifying droplet {box.droplet_id} is gone...")
    if confirm_droplet_absent(box.droplet_id):
        _box_down_step(
            steps, is_json, "confirm", "ok", f"droplet {box.droplet_id} confirmed absent via API read"
        )
        return DESTROY_CONFIRMED_ABSENT

    _box_down_step(
        steps,
        is_json,
        "confirm",
        "warn",
        f"droplet {box.droplet_id} delete requested but still API-listed; not marking destroyed",
    )
    return DESTROY_PENDING


def _cleanup_box_volume(box: Box, steps: list[dict[str, Any]], *, is_json: bool) -> bool:
    volume_id = str(box.volume_id or "").strip()
    if not volume_id:
        _box_down_step(steps, is_json, "volume", "skip", "no volume id")
        return True

    volume_label = box.volume_name or volume_id
    droplet_id = str(box.droplet_id or "").strip()
    try:
        volume = do_get_volume(volume_id)
    except Exception as exc:
        _box_down_step(steps, is_json, "volume", "warn", f"could not inspect volume {volume_label}: {exc}")
        return False

    attached_ids = _volume_droplet_ids(volume)
    foreign_ids = [attached_id for attached_id in attached_ids if not droplet_id or attached_id != droplet_id]
    if foreign_ids:
        detail = (
            f"volume {volume_label} is still attached to droplet(s) {', '.join(foreign_ids)}; "
            "not deleting"
        )
        _box_down_step(steps, is_json, "volume", "warn", detail)
        return False

    if droplet_id and droplet_id in attached_ids:
        try:
            if not is_json:
                print(f"[...] volume  Detaching volume {volume_label} from droplet {droplet_id}...")
            if not do_detach_volume(volume_id, droplet_id):
                _box_down_step(steps, is_json, "volume", "warn", f"detach failed for volume {volume_label}")
                return False
            volume = do_get_volume(volume_id)
        except Exception as exc:
            _box_down_step(steps, is_json, "volume", "warn", f"detach failed for volume {volume_label}: {exc}")
            return False

    remaining_ids = _volume_droplet_ids(volume)
    if remaining_ids:
        detail = (
            f"volume {volume_label} is still attached to droplet(s) {', '.join(remaining_ids)}; "
            "not deleting"
        )
        _box_down_step(steps, is_json, "volume", "warn", detail)
        return False

    try:
        if not is_json:
            print(f"[...] volume  Deleting volume {volume_label}...")
        if do_delete_volume(volume_id):
            _box_down_step(steps, is_json, "volume", "ok", f"volume {volume_label} deleted")
            return True
        _box_down_step(steps, is_json, "volume", "warn", f"delete failed for volume {volume_label}")
    except Exception as exc:
        _box_down_step(steps, is_json, "volume", "warn", f"delete failed for volume {volume_label}: {exc}")
    return False


def _emit_box_down_destroy_failure(box: Box, box_id: str, steps: list[dict[str, Any]], *, is_json: bool) -> int:
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


def _emit_box_down_destroy_pending(boxes: list[Box], box: Box, box_id: str, steps: list[dict[str, Any]], *, is_json: bool) -> int:
    update_box(box, state="destroy-pending")
    save_inventory(boxes)
    message = (
        f"Droplet delete was requested for box {box_id!r}, but DigitalOcean still lists the "
        f"droplet (read-after-delete not yet confirmed). Inventory stays {box.state!r} so it does "
        "not falsely report destroyed while the droplet may still bill."
    )
    next_actions = [f"box status {box_id}", f"box down {box_id}", "box list"]
    payload = {
        "box_id": box_id,
        "dry_run": False,
        "steps": steps,
        "next_actions": next_actions,
    }
    payload.update(
        structured_error(
            message,
            error_type="destroy_pending",
            recovery_hint=(
                "DigitalOcean delete is eventually consistent. Re-run box down to re-confirm "
                "absence; if it persists, verify the droplet in the DO console."
            ),
            next_actions=next_actions,
        )
    )
    if is_json:
        emit_json(payload)
    else:
        print(message, file=sys.stderr)
    return EXIT_ERROR


def _emit_box_down_volume_failure(boxes: list[Box], box: Box, box_id: str, steps: list[dict[str, Any]], *, is_json: bool) -> int:
    update_box(box, state="volume-cleanup-failed")
    save_inventory(boxes)
    message = f"Droplet was destroyed for box {box_id!r}, but volume cleanup did not complete."
    payload = {
        "box_id": box_id,
        "dry_run": False,
        "steps": steps,
        "next_actions": [f"box status {box_id}", f"box down {box_id}", "box list"],
    }
    payload.update(
        structured_error(
            message,
            error_type="volume_cleanup_failed",
            recovery_hint=(
                "Inspect the volume warning, then retry box down after confirming the "
                "volume can be detached or deleted safely."
            ),
            next_actions=[f"box status {box_id}", f"box down {box_id}", "box list"],
        )
    )
    if is_json:
        emit_json(payload)
    else:
        print(message, file=sys.stderr)
    return EXIT_ERROR


def _emit_box_down_success(boxes: list[Box], box: Box, box_id: str, steps: list[dict[str, Any]], *, is_json: bool) -> int:
    update_box(box, state="destroyed")
    save_inventory(boxes)
    payload = {"box_id": box_id, "dry_run": False, "steps": steps, "next_actions": ["box list"]}
    if is_json:
        emit_json(payload)
    else:
        print(f"\nBox {box_id} destroyed.")
    return EXIT_OK


def cmd_down(box_id: str, *, dry_run: bool, fmt: str) -> int:
    is_json = fmt == "json"
    steps: list[dict[str, Any]] = []
    boxes = load_inventory()
    box = find_box(boxes, box_id)
    if box is None or box.state == "destroyed":
        return emit_error_or_print(
            f"Box {box_id!r} not found or already destroyed.",
            is_json=is_json,
            error_type="not_found",
            next_actions=["box list"],
        )

    if box.management_mode == "external":
        msg = (
            f"Box {box_id!r} was registered from an existing shared host and cannot be torn down "
            f"through box down. Use 'box unregister {box_id}' to remove the local inventory entry."
        )
        return emit_error_or_print(msg, is_json=is_json, error_type="invalid_state", next_actions=[f"box unregister {box_id}"])

    do_token = optional_env("SKILLBOX_DO_TOKEN")
    if do_token:
        os.environ["DIGITALOCEAN_ACCESS_TOKEN"] = do_token
    if dry_run:
        return _emit_box_down_dry_run(box, box_id, steps, is_json=is_json)

    if box.state == "volume-cleanup-failed":
        # Droplet already confirmed gone on a prior run; only volume cleanup is
        # outstanding. Rerun is idempotent: re-attempt volume cleanup and
        # converge to destroyed when the volume can be released.
        _box_down_step(
            steps,
            is_json,
            "destroy",
            "skip",
            "droplet was already destroyed; retrying volume cleanup",
        )
        return _finish_box_down_after_droplet_gone(boxes, box, box_id, steps, is_json=is_json)

    if box.state == "destroy-pending":
        # A prior run requested droplet delete but could not confirm absence.
        # Skip drain/tailnet/firewall (already attempted) and re-run the
        # read-after-delete confirmation only. Idempotent: converges once DO
        # finishes the delete; stays pending while the droplet is still listed.
        _box_down_step(steps, is_json, "drain", "skip", "droplet delete already requested")
        _box_down_step(steps, is_json, "remove", "skip", "droplet delete already requested")
        _box_down_step(steps, is_json, "firewall", "skip", "droplet delete already requested")
        return _resume_box_down_confirm_destroy(boxes, box, box_id, steps, is_json=is_json)

    ssh_target = _drain_box_for_down(box, steps, is_json=is_json)
    update_box(box, state="draining")
    save_inventory(boxes)
    _remove_box_from_tailnet(box, ssh_target, steps, is_json=is_json)
    _cleanup_box_firewall(box, steps, is_json=is_json)
    return _finish_box_down_destroy_phase(boxes, box, box_id, steps, is_json=is_json)


def _finish_box_down_destroy_phase(
    boxes: list[Box], box: Box, box_id: str, steps: list[dict[str, Any]], *, is_json: bool
) -> int:
    """Run droplet destroy + read-after-delete confirm, then volume cleanup.

    `destroyed` is only ever written after a confirmed-absent observation:
      - DESTROY_FAILED: preserve state, structured destroy_failed error.
      - DESTROY_PENDING: park in destroy-pending (truthful; droplet may bill).
      - DESTROY_CONFIRMED_ABSENT: safe to proceed to volume cleanup.
    """
    destroy_status = _destroy_box_droplet(box, steps, is_json=is_json)
    if destroy_status == DESTROY_FAILED:
        save_inventory(boxes)
        return _emit_box_down_destroy_failure(box, box_id, steps, is_json=is_json)
    if destroy_status == DESTROY_PENDING:
        return _emit_box_down_destroy_pending(boxes, box, box_id, steps, is_json=is_json)
    return _finish_box_down_after_droplet_gone(boxes, box, box_id, steps, is_json=is_json)


def _resume_box_down_confirm_destroy(
    boxes: list[Box], box: Box, box_id: str, steps: list[dict[str, Any]], *, is_json: bool
) -> int:
    """Resume path for destroy-pending: re-confirm absence without re-deleting.

    The droplet delete was already requested; we only need the read-after-delete
    confirmation to converge. If the droplet is gone we proceed to volume
    cleanup; otherwise the box stays in destroy-pending.
    """
    if not box.droplet_id or confirm_droplet_absent(box.droplet_id):
        detail = (
            "no droplet id" if not box.droplet_id
            else f"droplet {box.droplet_id} confirmed absent via API read"
        )
        _box_down_step(steps, is_json, "confirm", "ok", detail)
        return _finish_box_down_after_droplet_gone(boxes, box, box_id, steps, is_json=is_json)
    _box_down_step(
        steps,
        is_json,
        "confirm",
        "warn",
        f"droplet {box.droplet_id} still API-listed; staying in destroy-pending",
    )
    return _emit_box_down_destroy_pending(boxes, box, box_id, steps, is_json=is_json)


def _finish_box_down_after_droplet_gone(
    boxes: list[Box], box: Box, box_id: str, steps: list[dict[str, Any]], *, is_json: bool
) -> int:
    """Droplet is confirmed gone — run volume cleanup and finalize.

    Volume cleanup failing here is not a billing lie (the droplet is gone), so we
    park in volume-cleanup-failed (queryable + resumable) rather than destroyed.
    """
    if not _cleanup_box_volume(box, steps, is_json=is_json):
        return _emit_box_down_volume_failure(boxes, box, box_id, steps, is_json=is_json)
    return _emit_box_down_success(boxes, box, box_id, steps, is_json=is_json)


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

def _load_registration_profile(profile_name: str, *, is_json: bool) -> BoxProfile | None:
    try:
        profile_name = _validate_profile_name(profile_name)
    except RuntimeError as exc:
        emit_error_or_print(str(exc), is_json=is_json, error_type="profile_not_found")
        raise
    if profile_name == "shared":
        return None
    try:
        return load_profile(profile_name)
    except RuntimeError as exc:
        emit_error_or_print(str(exc), is_json=is_json, error_type="profile_not_found")
        raise


def _reject_registration_conflict(
    existing: Box | None,
    *,
    resolved_box_id: str,
    force: bool,
    is_json: bool,
) -> bool:
    if not existing or existing.state == "destroyed" or force:
        return False
    msg = (
        f"Box {resolved_box_id!r} already exists in state {existing.state!r}. "
        f"Use 'box unregister {resolved_box_id}' or rerun with --force."
    )
    emit_error_or_print(
        msg,
        is_json=is_json,
        error_type="conflict",
        next_actions=[f"box status {resolved_box_id}", f"box unregister {resolved_box_id}"],
    )
    return True


def _build_registered_box(
    *,
    resolved_box_id: str,
    host: str,
    profile_name: str,
    profile: BoxProfile | None,
    ssh_user: str | None,
) -> Box:
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
    return box


def _apply_registration_probe_updates(box: Box, register_probe: dict[str, Any]) -> None:
    updates: dict[str, Any] = {}
    if register_probe.get("tailscale_ip") and not box.tailscale_ip:
        updates["tailscale_ip"] = register_probe["tailscale_ip"]
    if register_probe.get("ssh_reachable"):
        updates["state"] = "ready" if register_probe.get("container_running") else "ssh-ready"
    if updates:
        update_box(box, **updates)


def _print_registration_text(resolved_box_id: str, box: Box, payload: dict[str, Any], *, host: str) -> None:
    print(f"Registered external box {resolved_box_id} from {host}.")
    print(f"  SSH user: {box.ssh_user}")
    if payload["ssh_reachable"]:
        print(f"  connect: ssh {box.ssh_user}@{payload['ssh_target']}")
    else:
        print("  ssh probe: unreachable (saved with known fields only)")


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

    try:
        profile = _load_registration_profile(profile_name, is_json=is_json)
    except RuntimeError:
        return EXIT_ERROR
    profile_name = profile.id if profile is not None else "shared"

    boxes = load_inventory()
    existing = find_box(boxes, resolved_box_id)
    if _reject_registration_conflict(existing, resolved_box_id=resolved_box_id, force=force, is_json=is_json):
        return EXIT_ERROR

    filtered_boxes = [candidate for candidate in boxes if candidate.id != resolved_box_id]
    box = _build_registered_box(
        resolved_box_id=resolved_box_id,
        host=host,
        profile_name=profile_name,
        profile=profile,
        ssh_user=ssh_user,
    )
    register_probe = probe_registered_box(box, enabled=probe)
    _apply_registration_probe_updates(box, register_probe)
    filtered_boxes.append(box)
    save_inventory(filtered_boxes)
    payload = registration_payload(box, register_probe, host=host)
    if is_json:
        emit_json(payload)
    else:
        _print_registration_text(resolved_box_id, box, payload, host=host)
    return EXIT_OK


# ---------------------------------------------------------------------------
# box status
# ---------------------------------------------------------------------------

def cmd_status(box_id: str | None, *, fmt: str) -> int:
    is_json = fmt == "json"
    boxes = load_inventory()
    ssh_target_snapshot = inventory_ssh_target_snapshot(boxes)

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
        persist_inventory_if_ssh_targets_changed(boxes, ssh_target_snapshot)
        if is_json:
            emit_json(status)
        else:
            print_box_status_text(status)
        return EXIT_OK
    else:
        active_boxes = [box for box in boxes if box.state != "destroyed"]
        if active_boxes:
            max_workers = min(5, len(active_boxes))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                statuses = list(executor.map(box_health, active_boxes))
        else:
            statuses = []
        payload: dict[str, Any] = {
            "boxes": statuses,
            "next_actions": ["box up <id> --profile <name>", "box register <id> --host <tailscale-hostname>"] if not statuses else [],
        }
        persist_inventory_if_ssh_targets_changed(boxes, ssh_target_snapshot)
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
    phone_url = browser_url_for(box.tailscale_ip)
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
        "phone_url": phone_url,
        "browser_url": phone_url,
        "magicdns_url": None,
        "network_checks": {},
    }

    if box.state in ("destroyed", "creating"):
        return status

    # Teardown-pending states are surfaced truthfully without probing SSH: the
    # droplet is being torn down (or already gone) so there is nothing to reach.
    # box-status and box-list both expose the exact retry command.
    if box.state in ("destroy-pending", "volume-cleanup-failed"):
        if box.state == "destroy-pending":
            status["teardown_pending"] = {
                "reason": "droplet delete requested but not yet confirmed absent via API read",
                "billing_risk": True,
            }
        else:
            status["teardown_pending"] = {
                "reason": "droplet confirmed gone but volume cleanup did not complete",
                "billing_risk": False,
            }
        status["next_actions"] = [f"box down {box.id}", f"box status {box.id}", "box list"]
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

    network_checks = box_network_health(box)
    status["network_checks"] = network_checks
    if network_checks["magicdns_resolution"].get("ok"):
        status["magicdns_url"] = browser_url_for(box.tailscale_hostname)

    posture = resolve_network_posture(box)
    status["network_posture"] = posture
    status["cloud_firewall_id"] = box.cloud_firewall_id
    violations = evaluate_posture_violations(box, network_checks=network_checks)
    status["posture_violations"] = violations

    next_actions: list[str] = []
    if not status["ssh_reachable"]:
        if box.management_mode == "external":
            next_actions.append(f"box unregister {box.id}")
        else:
            next_actions.append(f"box down {box.id}")
    elif not status["container_running"]:
        next_actions.append(f"box ssh {box.id}")
    for v in violations:
        if v["type"] == "public_ssh_reachable":
            next_actions.append(f"Lockdown: restrict public SSH on {box.id}")
        elif v["type"] == "cloud_firewall_missing":
            next_actions.append(f"Create cloud firewall for {box.id}")
        elif v["type"] == "app_exposure_violation":
            next_actions.append(f"Fix {v.get('service_id', 'unknown')} bind: {v.get('exposure', '')}")
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
    if status.get("phone_url"):
        print(f"Open this on phone: {status['phone_url']}")
    if status.get("magicdns_url"):
        print(f"MagicDNS: {status['magicdns_url']}")
    posture = status.get("network_posture") or "unknown"
    fw_id = status.get("cloud_firewall_id") or "none"
    print(f"  posture={posture}  cloud_firewall={fw_id}")
    network_checks = status.get("network_checks") or {}
    if network_checks:
        print("  network:")
        for label, check in (
            ("public SSH", network_checks.get("public_ssh") or {}),
            ("Tailnet ping", network_checks.get("tailnet_ping") or {}),
            ("MagicDNS", network_checks.get("magicdns_resolution") or {}),
            ("port", network_checks.get("port_reachability") or {}),
        ):
            state = "ok" if check.get("ok") else "fail"
            target = check.get("target") or check.get("hostname") or "n/a"
            print(f"    - {label}: {state} ({target})")
    violations = status.get("posture_violations") or []
    if violations:
        print("  posture violations:")
        for v in violations:
            severity = v.get("severity", "warning")
            print(f"    - [{severity}] {v.get('message', v.get('type', 'unknown'))}")


# ---------------------------------------------------------------------------
# box posture-proof
# ---------------------------------------------------------------------------

def cmd_posture_proof(box_id: str, *, fmt: str) -> int:
    is_json = fmt == "json"
    boxes = load_inventory()
    box = find_box(boxes, box_id)
    if box is None:
        msg = f"Box {box_id!r} not found."
        if is_json:
            emit_json(structured_error(msg, error_type="not_found", next_actions=["box list"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    posture = resolve_network_posture(box)
    public_ssh_probe = _check_public_ssh(box)
    tailnet_probe = _check_tailnet_ping(box)

    cloud_firewall_rules: dict[str, Any] | None = None
    if box.cloud_firewall_id:
        cloud_firewall_rules = do_get_firewall(box.cloud_firewall_id)

    network_checks = {"public_ssh": public_ssh_probe, "tailnet_ping": tailnet_probe}
    violations = evaluate_posture_violations(box, network_checks=network_checks)

    proof: dict[str, Any] = {
        "box_id": box.id,
        "posture": posture,
        "cloud_firewall_rules": cloud_firewall_rules,
        "public_ssh_probe": public_ssh_probe,
        "tailnet_probe": tailnet_probe,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "violations": violations,
    }

    if is_json:
        emit_json(proof)
    else:
        print(f"Box: {box.id}")
        print(f"Posture: {posture}")
        print(f"Cloud firewall: {'present' if cloud_firewall_rules else 'none'}")
        print(f"Public SSH: {'reachable' if public_ssh_probe.get('ok') else 'unreachable'}")
        print(f"Tailnet: {'reachable' if tailnet_probe.get('ok') else 'unreachable'}")
        if violations:
            print(f"Violations ({len(violations)}):")
            for v in violations:
                print(f"  - [{v.get('severity', 'unknown')}] {v.get('message', v.get('type', 'unknown'))}")
        else:
            print("Violations: none")
    return EXIT_OK


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

    posture = resolve_network_posture(box)
    public_ip = str(box.droplet_ip or "").strip()
    if posture == POSTURE_TAILNET_ONLY and public_ip and target == public_ip:
        print(f"Warning: connecting via public IP ({target}) — posture is {posture}; recovery mode only", file=sys.stderr)

    _validate_ssh_user(box.ssh_user)
    _validate_host(target)
    os.execvp("ssh", ["ssh", *DEFAULT_SSH_OPTS, "--", f"{box.ssh_user}@{target}"])
    return EXIT_ERROR  # unreachable


# ---------------------------------------------------------------------------
# box list
# ---------------------------------------------------------------------------

def _teardown_pending_hint(box: Box) -> dict[str, Any] | None:
    """Per-box retry hint for teardown-pending boxes, surfaced from box-list."""
    if box.state == "destroy-pending":
        return {
            "box_id": box.id,
            "state": box.state,
            "reason": "droplet delete requested but not yet confirmed absent via API read",
            "billing_risk": True,
            "next_action": f"box down {box.id}",
        }
    if box.state == "volume-cleanup-failed":
        return {
            "box_id": box.id,
            "state": box.state,
            "reason": "droplet confirmed gone but volume cleanup did not complete",
            "billing_risk": False,
            "next_action": f"box down {box.id}",
        }
    return None


def cmd_list(*, fmt: str) -> int:
    boxes = load_inventory()
    active = [b for b in boxes if b.state != "destroyed"]
    pending = [hint for hint in (_teardown_pending_hint(b) for b in active) if hint]

    if fmt == "json":
        payload: dict[str, Any] = {
            "boxes": [asdict(b) for b in active],
            "next_actions": ["box up <id> --profile <name>", "box register <id> --host <tailscale-hostname>"] if not active else [],
        }
        if pending:
            payload["teardown_pending"] = pending
        emit_json(payload)
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
            for hint in pending:
                print(f"    ! {hint['box_id']} teardown pending ({hint['state']}); retry: {hint['next_action']}")
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

def _suggest_box_command(message: str) -> str | None:
    marker = "invalid choice: '"
    if marker not in message:
        return None
    bad = message.split(marker, 1)[1].split("'", 1)[0]
    matches = difflib.get_close_matches(bad, sorted(BOX_COMMAND_NAMES), n=1)
    return matches[0] if matches else None


class BoxArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - argparse exits
        suggestion = _suggest_box_command(message)
        if suggestion:
            message = (
                f"{message}\nDid you mean: `{self.prog} {suggestion}`?\n"
                f"Discover commands: `{self.prog} capabilities --json`."
            )
        super().error(message)


def _normalize_agent_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    diagnostics: list[str] = []
    command_seen = False
    current_command: str | None = None
    pending_json = False
    for token in argv:
        if token == "--robot-help":
            normalized.extend(["robot-docs", "guide"])
            command_seen = True
            current_command = "robot-docs"
            continue
        if token == "--robot-triage":
            normalized.append("robot-triage")
            command_seen = True
            current_command = "robot-triage"
            continue
        if token in BOX_JSON_FLAG_ALIASES:
            if token != "--json":
                diagnostics.append(
                    f"Interpreting {token} as --format json. "
                    "Exact command: box.py <command> --format json"
                )
            if command_seen:
                if current_command in BOX_JSON_COMMANDS or current_command is None:
                    normalized.extend(["--format", "json"])
                else:
                    normalized.append(token)
            else:
                pending_json = True
            continue
        if not token.startswith("-") and not command_seen:
            command_seen = True
            current_command = token
            normalized.append(token)
            if pending_json and token in BOX_JSON_COMMANDS:
                normalized.extend(["--format", "json"])
                pending_json = False
            continue
        normalized.append(token)
    if pending_json and not command_seen:
        normalized.extend(["status", "--format", "json"])
    return normalized, diagnostics


def build_parser() -> argparse.ArgumentParser:
    parser = BoxArgumentParser(
        prog="box.py",
        description="Skillbox box lifecycle manager: create, bootstrap, and destroy DigitalOcean + Tailscale boxes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities_parser = subparsers.add_parser(
        "capabilities",
        help="Print the machine-readable agent contract.",
    )
    capabilities_parser.add_argument("--format", choices=("json",), default="json")

    robot_docs_parser = subparsers.add_parser(
        "robot-docs",
        help="Print agent-facing command guidance.",
    )
    robot_docs_parser.add_argument("topic", nargs="?", default="guide", choices=("guide",))
    robot_docs_parser.add_argument("--format", choices=("text", "json"), default="text")

    robot_triage_parser = subparsers.add_parser(
        "robot-triage",
        help="Print compact machine-readable first actions.",
    )
    robot_triage_parser.add_argument("--format", choices=("json",), default="json")

    up_parser = subparsers.add_parser("up", help="Create and provision a new box from a pinned deploy artifact.")
    up_parser.add_argument("box_id", type=_validate_box_id, help="Box identifier (becomes droplet name and client id).")
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
    down_parser.add_argument("box_id", type=_validate_box_id, help="Box identifier.")
    down_parser.add_argument("--dry-run", action="store_true")
    down_parser.add_argument("--format", choices=("text", "json"), default="text")

    upgrade_parser = subparsers.add_parser("upgrade", help="Upgrade an existing ready box from a pinned deploy manifest.")
    upgrade_parser.add_argument("box_id", type=_validate_box_id, help="Box identifier.")
    upgrade_parser.add_argument("--deploy-manifest", required=True, help="Pinned deploy.json from client-publish --deploy-artifact.")
    upgrade_parser.add_argument("--dry-run", action="store_true")
    upgrade_parser.add_argument("--format", choices=("text", "json"), default="text")

    status_parser = subparsers.add_parser("status", help="Check health of one or all boxes.")
    status_parser.add_argument("box_id", nargs="?", default=None, type=_validate_box_id, help="Box identifier (omit for all).")
    status_parser.add_argument("--format", choices=("text", "json"), default="text")

    posture_proof_parser = subparsers.add_parser("posture-proof", help="Generate a network posture proof artifact for a box.")
    posture_proof_parser.add_argument("box_id", type=_validate_box_id, help="Box identifier.")
    posture_proof_parser.add_argument("--format", choices=("text", "json"), default="json")

    ssh_parser = subparsers.add_parser("ssh", help="SSH into a box.")
    ssh_parser.add_argument("box_id", type=_validate_box_id, help="Box identifier.")

    register_parser = subparsers.add_parser("register", help="Register an existing shared or manually created box in local inventory.")
    register_parser.add_argument("box_id", nargs="?", default=None, type=_validate_box_id, help="Local box identifier. Defaults to a host-derived alias.")
    register_parser.add_argument("--host", required=True, type=_validate_host, help="Reachable host: Tailscale hostname, Tailscale IP, or public IP.")
    register_parser.add_argument("--profile", default="shared", help="Local profile label (default: shared).")
    register_parser.add_argument("--ssh-user", default=None, type=_validate_ssh_user, help="SSH login user. Defaults to the profile ssh_user or 'skillbox'.")
    register_parser.add_argument("--force", action="store_true", help="Replace an existing active inventory entry with the same id.")
    register_parser.add_argument("--no-probe", action="store_true", help="Skip the SSH probe and save known fields only.")
    register_parser.add_argument("--format", choices=("text", "json"), default="text")

    import_parser = subparsers.add_parser("import", help="Alias for register.")
    import_parser.add_argument("box_id", nargs="?", default=None, type=_validate_box_id, help="Local box identifier. Defaults to a host-derived alias.")
    import_parser.add_argument("--host", required=True, type=_validate_host, help="Reachable host: Tailscale hostname, Tailscale IP, or public IP.")
    import_parser.add_argument("--profile", default="shared", help="Local profile label (default: shared).")
    import_parser.add_argument("--ssh-user", default=None, type=_validate_ssh_user, help="SSH login user. Defaults to the profile ssh_user or 'skillbox'.")
    import_parser.add_argument("--force", action="store_true", help="Replace an existing active inventory entry with the same id.")
    import_parser.add_argument("--no-probe", action="store_true", help="Skip the SSH probe and save known fields only.")
    import_parser.add_argument("--format", choices=("text", "json"), default="text")

    unregister_parser = subparsers.add_parser("unregister", help="Remove a registered external box from local inventory.")
    unregister_parser.add_argument("box_id", type=_validate_box_id, help="Box identifier.")
    unregister_parser.add_argument("--format", choices=("text", "json"), default="text")

    subparsers.add_parser("list", help="List all active boxes.").add_argument(
        "--format", choices=("text", "json"), default="text",
    )

    subparsers.add_parser("profiles", help="List available box profiles.").add_argument(
        "--format", choices=("text", "json"), default="text",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_operator_secret(".env")
    load_operator_secret(".env.box")

    parser = build_parser()
    normalized_argv, diagnostics = _normalize_agent_argv(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(normalized_argv)
    for diagnostic in diagnostics:
        print(diagnostic, file=sys.stderr)

    try:
        if args.command == "capabilities":
            emit_json(box_capabilities_payload())
            return EXIT_OK
        if args.command == "robot-docs":
            guide = box_robot_docs_guide()
            if args.format == "json":
                emit_json({"ok": True, "topic": args.topic, "guide": guide})
            else:
                print(guide.rstrip())
            return EXIT_OK
        if args.command == "robot-triage":
            emit_json(box_robot_triage_payload())
            return EXIT_OK
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
        if args.command == "posture-proof":
            return cmd_posture_proof(args.box_id, fmt=args.format)
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
    except json.JSONDecodeError as exc:
        # doctl returned a 0 exit code but non-JSON stdout (warning banner,
        # empty body, rate-limit notice). Surface a structured error instead of
        # an unhandled traceback so the --format json contract still holds.
        emit_json(structured_error(
            f"Expected JSON from underlying command but parsing failed: {exc}",
            error_type="invalid_output",
            recovery_hint="Re-run the doctl/tailscale command manually to inspect its raw output.",
        ))
        return EXIT_ERROR

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
