#!/usr/bin/env python3
"""
skillbox operator MCP server — fleet and container lifecycle as native agent tools.

Runs on the operator's machine (outside the container).
Wraps box.py (DO+Tailscale fleet), docker compose (container lifecycle),
and 04-reconcile.py (outer validation) as MCP tools.

Protocol: JSON-RPC 2.0 over stdio (MCP 2024-11-05).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BOX_PY = SCRIPT_DIR / "box.py"
RECONCILE_PY = SCRIPT_DIR / "04-reconcile.py"
# DEPRECATED repo-root secret locations (inside the workspace bind mount).
# Retained for reference/back-compat; main() loads via load_operator_secret(),
# which prefers operator_secret_dir() and only falls back here with a warning.
ENV_FILE = REPO_ROOT / ".env"
ENV_BOX_FILE = REPO_ROOT / ".env.box"

SERVER_NAME = "skillbox-operator"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_FIRST_BOX_BLUEPRINT = "git-repo-http-service-bootstrap-spaps-auth"
PROVISION_TIMEOUT_SECONDS = 3600

# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

_BOX_ID_PROP: dict = {
    "type": "string",
    "description": (
        "Box identifier (becomes droplet name and client ID). "
        "Pattern: lowercase alphanumeric with hyphens. "
        "Discover IDs with operator_boxes."
    ),
}
_DRY_RUN_PROP: dict = {
    "type": "boolean",
    "description": "Preview changes without applying them. ALWAYS use first for destructive operations.",
    "default": False,
}

# ---------------------------------------------------------------------------
# Identifier validation (path traversal / flag injection guard)
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_SSH_USER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$")
_HOST_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,253}[a-zA-Z0-9])?$")

# Single source of truth for secret redaction (scripts/lib/redaction.py), same
# leaf-import direction as lib.runtime_model. ``redact_diagnostic_text`` and
# ``_redact_diagnostic_value`` are preserved as thin aliases because call sites
# (including the box_exec audit path) and tests reference these exact names.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from lib.redaction import (  # noqa: E402
    REDACTION_MARKER,
    redact_text as redact_diagnostic_text,
    redact_value as _redact_diagnostic_value,
)

DRYRUN_MARKER_TTL_SECONDS = 600  # 10 minutes
_DRYRUN_MARKER_STATUS_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# operator_box_exec command policy (server-side gate)
#
# operator_box_exec runs ARBITRARY shell over Tailscale SSH on any inventory
# box. Unlike teardown/compose_down (single fixed effect), the command itself
# is the payload, so the gate lives here on the server (works for every MCP
# client, like the provision dry-run gate) rather than only in the hook.
#
# Policy, in two tiers:
#   1. READ-ONLY ALLOWLIST — a SHORT, BORING set of inspection commands that
#      cannot mutate state. These pass unconditionally (no dry-run friction).
#      We match on the LEADING command token(s) and refuse the command if it
#      contains shell metacharacters that could chain a second command
#      (`;`, `|`, `&`, `>`, backticks, `$(`, `&&`, `||`, newlines used as
#      separators, etc.) — an allowlisted prefix must NOT be a smuggling
#      vector for an arbitrary tail.
#   2. EVERYTHING ELSE — mutating verbs, unknown commands, or anything with
#      chaining metacharacters — requires a fresh dry_run=true preview that
#      stamps a marker keyed by box_id + a hash of the NORMALIZED command, so
#      a marker minted for command A cannot authorize command B.
# ---------------------------------------------------------------------------

# Shell metacharacters that can chain/redirect a second command. Their presence
# disqualifies the read-only fast path: even `cat foo` becomes mutating-capable
# as `cat foo > /etc/passwd` or `cat foo; rm -rf /`. A command with any of these
# must go through the dry-run marker path regardless of its leading token.
_SHELL_CHAIN_RE = re.compile(r"[;&|><`\n\r]|\$\(|\$\{|\\\n")

# Read-only allowlist. Keyed by the leading token; the value is either:
#   - None: any args allowed (e.g. `df`, `uptime`).
#   - a set of allowed SECOND tokens (e.g. `docker` -> {"ps", "logs", ...},
#     `git` -> {"status", "log", ...}, `systemctl` -> {"status", ...}).
# Conservative on purpose: subcommands like `docker exec`, `git push`,
# `systemctl restart` are NOT here and fall through to the dry-run gate.
_READONLY_ALLOWLIST: dict[str, set[str] | None] = {
    # Plain inspection commands (any args).
    "cat": None,
    "df": None,
    "du": None,
    "free": None,
    "head": None,
    "hostname": None,
    "id": None,
    "journalctl": None,
    "ls": None,
    "nproc": None,
    "ps": None,
    "pwd": None,
    "stat": None,
    "tail": None,
    "uname": None,
    "uptime": None,
    "wc": None,
    "whoami": None,
    # Subcommand-scoped: only the read-only verbs below are allowlisted.
    "docker": {"ps", "logs", "images", "inspect", "stats", "version", "top"},
    "git": {"status", "log", "diff", "show", "branch", "remote", "rev-parse"},
    "systemctl": {"status", "is-active", "is-enabled", "list-units", "show"},
}

# Paths whose `cat`/`head`/`tail` would leak secrets. If the read-only command
# touches one of these, it does NOT get the fast path — it must dry-run first so
# the preview (and audit) records exactly what would be read.
_SECRET_PATH_RE = re.compile(
    r"(?:^|[\s=])"
    r"(?:[^\s]*/)?"
    r"(?:\.env(?:\.[\w.-]+)?|\.netrc|id_rsa|id_ed25519|"
    r"[^\s]*secret[^\s]*|[^\s]*credential[^\s]*|authkey|\.ssh/[^\s]*)",
    re.IGNORECASE,
)


def normalize_command(command: str) -> str:
    """Collapse insignificant whitespace so trivially-different spellings of
    the SAME command hash to the same marker key.

    Collapses runs of any whitespace (spaces, tabs, newlines) to a single
    space and strips leading/trailing whitespace. This makes
    ``"ls   -la"`` == ``"ls -la"`` and tolerates a trailing newline, but does
    NOT alter token order, quoting, or operators, so two semantically distinct
    commands never collide.
    """
    return re.sub(r"\s+", " ", command).strip()


def command_hash(command: str) -> str:
    """Stable short hash of the normalized command, used in the marker key.

    Binds a dry-run marker to the EXACT command previewed: a marker for
    command A cannot authorize command B because their hashes differ.
    """
    return hashlib.sha256(normalize_command(command).encode("utf-8")).hexdigest()[:16]


def _leading_tokens(command: str) -> list[str]:
    """Best-effort split of the normalized command into its leading tokens.

    We only need the first two tokens to consult the allowlist. ``shlex`` would
    raise on unbalanced quotes; for classification a simple whitespace split of
    the normalized command is sufficient and never raises.
    """
    return normalize_command(command).split(" ")


def classify_box_exec_command(command: str) -> dict[str, Any]:
    """Classify *command* as 'read-only' (allowlisted) or 'mutating'.

    Returns a dict: {"verdict": "read-only"|"mutating", "reason": str}.
    'read-only' means it passes unconditionally; 'mutating' means a matching
    dry-run marker is required. The classifier is conservative: anything it is
    not SURE is read-only is treated as mutating.
    """
    normalized = normalize_command(command)
    if not normalized:
        return {"verdict": "mutating", "reason": "empty command"}

    if _SHELL_CHAIN_RE.search(command):
        return {
            "verdict": "mutating",
            "reason": "contains shell chaining/redirection metacharacters",
        }

    tokens = _leading_tokens(command)
    head = tokens[0]

    # Reject an env-var prefix (FOO=bar cmd ...) or absolute/relative path
    # invocation on the fast path — we only allowlist bare, known tokens.
    if "=" in head or "/" in head:
        return {"verdict": "mutating", "reason": f"non-allowlisted invocation: {head!r}"}

    if head not in _READONLY_ALLOWLIST:
        return {"verdict": "mutating", "reason": f"command {head!r} not in read-only allowlist"}

    allowed_sub = _READONLY_ALLOWLIST[head]
    if allowed_sub is not None:
        sub = tokens[1] if len(tokens) > 1 else ""
        if sub not in allowed_sub:
            return {
                "verdict": "mutating",
                "reason": f"{head} subcommand {sub or '<none>'!r} not in read-only allowlist",
            }

    # `cat`/`head`/`tail`/`stat`/`ls` of a secret-looking path is NOT free:
    # it could exfiltrate secrets, so route it through the dry-run preview.
    if head in {"cat", "head", "tail", "stat", "ls", "wc"} and _SECRET_PATH_RE.search(command):
        return {
            "verdict": "mutating",
            "reason": "reads a secret-looking path; preview required",
        }

    return {"verdict": "read-only", "reason": f"allowlisted: {head}"}


def _dcg_verdict(command: str) -> dict[str, Any] | None:
    """Optionally pipe *command* through `dcg check` and surface its verdict.

    Best-effort and never a hard dependency: returns None if dcg is not
    installed or errors. Output is advisory only — the server-side classifier
    is the real gate.
    """
    dcg_bin = shutil.which("dcg")
    if not dcg_bin:
        return None
    try:
        proc = subprocess.run(
            [dcg_bin, "check", "--stdin"],
            input=command,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    verdict: dict[str, Any] = {"available": True, "exit_code": proc.returncode}
    out = proc.stdout.strip()
    if out:
        try:
            verdict["report"] = json.loads(out)
        except json.JSONDecodeError:
            verdict["report"] = redact_diagnostic_text(out)
    verdict["blocked"] = proc.returncode != 0
    return verdict


def _validate_identifier(value: str, kind: str) -> str:
    """Validate that *value* is a safe slug identifier.

    Rejects path separators, leading dashes, and anything not matching
    the slug pattern ``^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$``.

    Returns the validated value on success; raises ValueError otherwise.
    """
    if not value:
        raise ValueError(f"Invalid {kind}: must not be empty")
    if "/" in value or "\\" in value:
        raise ValueError(f"Invalid {kind}: must not contain path separators")
    if value.startswith("-"):
        raise ValueError(f"Invalid {kind}: must not start with '-'")
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(
            f"Invalid {kind}: must be a slug matching [a-zA-Z0-9][a-zA-Z0-9._-]{{0,63}}"
        )
    return value


def _validate_string_identifier(value: Any, kind: str, *, trim: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {kind}: must be a string")
    candidate = value.strip() if trim else value
    return _validate_identifier(candidate, kind)


def _validate_string(value: Any, kind: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {kind}: must be a string")
    return value


def _validate_bool(value: Any, kind: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Invalid {kind}: must be a boolean")
    return value


def _validate_optional_bool(params: dict, key: str, *, default: bool = False) -> bool:
    if key not in params:
        return default
    return _validate_bool(params[key], key)


def _validate_int(value: Any, kind: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Invalid {kind}: must be an integer")
    return value


def _validate_ssh_user(value: str, kind: str = "ssh_user") -> str:
    if not isinstance(value, str) or not _SSH_USER_RE.match(value):
        raise ValueError(f"Invalid {kind}: {value!r}")
    return value


def _validate_host(value: str, kind: str = "host") -> str:
    if not isinstance(value, str) or not _HOST_RE.match(value):
        raise ValueError(f"Invalid {kind}: {value!r}")
    return value


def _tool_metadata(
    *,
    read_only: bool,
    destructive: bool = False,
    dry_run_required: bool = False,
    requires_user_confirmation: bool = False,
    side_effects: str = "none",
    safe_first_call: str,
    exact_cli: str,
    next_tools: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
        },
        "x_skillbox_contract": {
            "dry_run_required": dry_run_required,
            "requires_user_confirmation": requires_user_confirmation,
            "side_effects": side_effects,
            "safe_first_call": safe_first_call,
            "exact_cli": exact_cli,
            "next_tools": next_tools or [],
        },
    }

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    # --- Fleet inspection ---
    {
        "name": "operator_profiles",
        "description": (
            "List available box profiles from workspace/box-profiles/. "
            "Each profile declares region, size, image, and SSH user for a DigitalOcean droplet. "
            "Use to choose a profile before provisioning."
        ),
        **_tool_metadata(
            read_only=True,
            safe_first_call="operator_profiles",
            exact_cli="python3 scripts/box.py profiles --format json",
            next_tools=["operator_boxes", "operator_provision"],
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "operator_boxes",
        "description": (
            "List all active boxes from inventory (workspace/boxes.json). "
            "Shows box ID, state, profile, droplet IP, and Tailscale hostname. "
            "RUN THIS FIRST to understand the current fleet before any operation."
        ),
        **_tool_metadata(
            read_only=True,
            safe_first_call="operator_boxes",
            exact_cli="python3 scripts/box.py list --format json",
            next_tools=["operator_box_status", "operator_profiles"],
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "operator_box_status",
        "description": (
            "Deep health probe for a specific box: SSH reachability, container state, "
            "droplet IP, Tailscale hostname, profile details. "
            "Omit box_id to check all boxes. "
            "Run before provisioning to check for conflicts, or after to verify health."
        ),
        **_tool_metadata(
            read_only=True,
            safe_first_call="operator_box_status",
            exact_cli="python3 scripts/box.py status --format json",
            next_tools=["operator_boxes", "operator_box_exec"],
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "box_id": {
                    "type": "string",
                    "description": "Box identifier. Omit to check all active boxes.",
                },
            },
        },
    },
    # --- Fleet lifecycle ---
    {
        "name": "operator_provision",
        "description": (
            "Full zero-to-running provision flow: create DO droplet → bootstrap OS → "
            "enroll in Tailscale → clone skillbox → build + start container → onboard project → verify. "
            "This is the primary macro — one call replaces 7 manual steps. "
            "ALWAYS use dry_run=true first. "
            "Dry-run returns credential_status; if missing is non-empty, stop and ask the operator "
            "to populate the operator secret file (${SKILLBOX_STATE_ROOT}/operator/.env.box, "
            "default ./.skillbox-state/operator/.env.box) with SKILLBOX_DO_TOKEN, "
            "SKILLBOX_DO_SSH_KEY_ID, and SKILLBOX_TS_AUTHKEY before running real provisioning."
        ),
        **_tool_metadata(
            read_only=False,
            dry_run_required=True,
            side_effects="creates DigitalOcean droplet, enrolls Tailscale, clones/builds skillbox",
            safe_first_call="operator_provision(box_id='<id>', dry_run=true)",
            exact_cli="python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json",
            next_tools=["operator_profiles", "operator_boxes", "operator_box_status"],
        ),
        "inputSchema": {
            "type": "object",
            "required": ["box_id"],
            "properties": {
                "box_id": _BOX_ID_PROP,
                "profile": {
                    "type": "string",
                    "description": "Box profile name (default: 'dev-small'). Use operator_profiles to list options.",
                    "default": "dev-small",
                },
                "deploy_manifest": {
                    "type": "string",
                    "description": (
                        "Pinned deploy.json path for non-dry-run launches. "
                        "Generate it with client-publish --deploy-artifact."
                    ),
                },
                "blueprint": {
                    "type": "string",
                    "description": (
                        "Client blueprint for the onboard step. Defaults to "
                        f"'{DEFAULT_FIRST_BOX_BLUEPRINT}' for SPAPS local auth/RBAC fixtures; "
                        "use 'git-repo-http-service-bootstrap' for a plain app service."
                    ),
                    "default": DEFAULT_FIRST_BOX_BLUEPRINT,
                },
                "set_vars": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Blueprint variables as KEY=VALUE strings. "
                        "Example: ['PRIMARY_REPO_URL=https://github.com/acme/app.git']."
                    ),
                },
                "resume": {
                    "type": "boolean",
                    "description": (
                        "Resume a partial ssh-ready/deploying/acceptance/onboarding box "
                        "instead of creating a new droplet."
                    ),
                    "default": False,
                },
                "dry_run": _DRY_RUN_PROP,
            },
        },
    },
    {
        "name": "operator_teardown",
        "description": (
            "Full teardown flow: drain services → remove from Tailnet → destroy DO droplet. "
            "CONFIRM WITH USER before running — this destroys infrastructure. "
            "ALWAYS use dry_run=true first."
        ),
        **_tool_metadata(
            read_only=False,
            destructive=True,
            dry_run_required=True,
            requires_user_confirmation=True,
            side_effects="drains services, removes Tailnet enrollment, destroys droplet",
            safe_first_call="operator_teardown(box_id='<id>', dry_run=true)",
            exact_cli="python3 scripts/box.py down <box-id> --dry-run --format json",
            next_tools=["operator_boxes", "operator_box_status"],
        ),
        "inputSchema": {
            "type": "object",
            "required": ["box_id"],
            "properties": {
                "box_id": _BOX_ID_PROP,
                "dry_run": _DRY_RUN_PROP,
            },
        },
    },
    {
        "name": "operator_box_exec",
        "description": (
            "Run a command on a box over Tailscale SSH. "
            "Use for ad-hoc operations: checking logs, running manage.py commands, inspecting state. "
            "The command runs as the box's SSH user (typically 'skillbox'). "
            "For interactive SSH, use 'make box-ssh BOX=<id>' instead. "
            "GATED: read-only inspection commands (status/logs/df/cat/ls/etc.) run "
            "immediately. Any MUTATING or unrecognized command must first be "
            "previewed with dry_run=true (which returns exactly what would run and "
            "stamps a marker bound to box_id + the command hash); only then will the "
            "identical command execute for real."
        ),
        **_tool_metadata(
            read_only=False,
            side_effects="runs caller-supplied command over SSH",
            safe_first_call=(
                "operator_box_exec(box_id='<id>', command='cd ~/skillbox && "
                "python3 .env-manager/manage.py status --format json')"
            ),
            exact_cli="make box-ssh BOX=<id>",
            next_tools=["operator_boxes", "operator_box_status"],
        ),
        "inputSchema": {
            "type": "object",
            "required": ["box_id", "command"],
            "properties": {
                "box_id": _BOX_ID_PROP,
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to execute on the box. "
                        "Example: 'cd ~/skillbox && docker compose exec -T workspace python3 .env-manager/manage.py status --format json'."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Command timeout in seconds (default: 120).",
                    "default": 120,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "Preview a mutating/unknown command without running it. Returns the exact "
                        "command that would execute and stamps a marker bound to box_id + command hash, "
                        "authorizing one real run of THIS command. Read-only commands do not need this."
                    ),
                    "default": False,
                },
            },
        },
    },
    # --- Local container lifecycle ---
    {
        "name": "operator_compose_up",
        "description": (
            "Build the workspace image and start the local container (docker compose build + up -d). "
            "Use on the operator machine to bring up the local skillbox workspace. "
            "Pass build=false to skip the image build and only start. "
            "Check response steps[] for optional surface start failures even when the headline up succeeds."
        ),
        **_tool_metadata(
            read_only=False,
            side_effects="builds and starts local Docker containers",
            safe_first_call="operator_doctor",
            exact_cli="make doctor",
            next_tools=["operator_doctor", "operator_render"],
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "build": {
                    "type": "boolean",
                    "description": "Build the workspace image before starting (default: true).",
                    "default": True,
                },
                "surfaces": {
                    "type": "boolean",
                    "description": "Also start optional api+web surfaces (default: false).",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "operator_compose_down",
        "description": (
            "Stop all local containers (docker compose down). "
            "This stops the workspace, api, and web containers. "
            "ALWAYS use dry_run=true first to preview what will be stopped."
        ),
        **_tool_metadata(
            read_only=False,
            destructive=True,
            dry_run_required=True,
            requires_user_confirmation=True,
            side_effects="stops local Docker containers",
            safe_first_call="operator_compose_down(dry_run=true)",
            exact_cli="docker compose ps --format json",
            next_tools=["operator_doctor"],
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": _DRY_RUN_PROP,
            },
        },
    },
    # --- Outer validation ---
    {
        "name": "operator_doctor",
        "description": (
            "Run outer validation: manifest drift, Compose wiring, file presence, "
            "skill sync state. Uses scripts/04-reconcile.py doctor. "
            "Run after cloning, after config changes, or to verify the repo is healthy."
        ),
        **_tool_metadata(
            read_only=True,
            safe_first_call="operator_doctor",
            exact_cli="python3 scripts/04-reconcile.py doctor --format json",
            next_tools=["operator_render"],
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "operator_render",
        "description": (
            "Print the resolved sandbox model: box shape, runtime paths, ports, dependencies. "
            "Uses scripts/04-reconcile.py render. Read-only, no side effects. "
            "Use to understand what the current configuration will produce."
        ),
        **_tool_metadata(
            read_only=True,
            safe_first_call="operator_render",
            exact_cli="python3 scripts/04-reconcile.py render --format json",
            next_tools=["operator_doctor"],
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "with_compose": {
                    "type": "boolean",
                    "description": "Include Docker Compose config in the render output.",
                    "default": False,
                },
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Load a .env file into os.environ (simple key=value, no quoting)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
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
# subpaths, never wholesale). The legacy repo-root ENV_FILE/ENV_BOX_FILE locations
# are deprecated and warn.
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


def run_script(
    script: Path,
    args: list[str],
    *,
    timeout: int = 300,
) -> tuple[bool, int, Any]:
    """Run a Python script as subprocess and parse JSON output."""
    if not script.exists():
        return False, -1, {
            "error": {
                "type": "script_not_found",
                "message": f"{script.name} not found at {script}.",
                "recoverable": False,
                "recovery_hint": "Are you running from the skillbox repo root?",
            }
        }

    cmd = [sys.executable, str(script)] + args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        return False, -1, {
            "error": {
                "type": "timeout",
                "message": f"{script.name} timed out after {timeout}s.",
                "recoverable": True,
            }
        }

    if proc.stderr.strip():
        stderr_text = redact_diagnostic_text(proc.stderr.strip())
        print(f"[operator-mcp] {script.name} stderr: {stderr_text}", file=sys.stderr, flush=True)

    stdout = proc.stdout.strip()
    if stdout:
        try:
            return proc.returncode == 0, proc.returncode, _redact_diagnostic_value(json.loads(stdout))
        except json.JSONDecodeError:
            return proc.returncode == 0, proc.returncode, {"text": redact_diagnostic_text(stdout)}

    return proc.returncode == 0, proc.returncode, {"exit_code": proc.returncode}


def _compose_monoserver_layer() -> list[str]:
    """Return the -f flags for the monoserver layer (client override or fat default)."""
    focus_path = REPO_ROOT / "workspace" / ".focus.json"
    if focus_path.is_file():
        try:
            focus = json.loads(focus_path.read_text(encoding="utf-8"))
            client_id = focus.get("client_id", "")
            override = REPO_ROOT / "workspace" / ".compose-overrides" / f"docker-compose.client-{client_id}.yml"
            if client_id and override.is_file():
                return ["-f", str(override.relative_to(REPO_ROOT))]
        except (json.JSONDecodeError, OSError):
            pass
    return ["-f", "docker-compose.monoserver.yml"]


def run_compose(args: list[str], *, timeout: int = 300) -> tuple[bool, int, Any]:
    """Run docker compose and return structured output."""
    file_flags = ["-f", "docker-compose.yml"] + _compose_monoserver_layer()
    cmd = ["docker", "compose"] + file_flags + args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT),
        )
    except FileNotFoundError:
        return False, -1, {
            "error": {
                "type": "docker_not_found",
                "message": "docker not found. Install Docker to manage containers.",
                "recoverable": False,
            }
        }
    except subprocess.TimeoutExpired:
        return False, -1, {
            "error": {
                "type": "timeout",
                "message": f"docker compose timed out after {timeout}s.",
                "recoverable": True,
            }
        }

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    ok = proc.returncode == 0

    # Try JSON parse (docker compose ps --format json)
    if stdout:
        try:
            return ok, proc.returncode, _redact_diagnostic_value(json.loads(stdout))
        except json.JSONDecodeError:
            pass

    return ok, proc.returncode, {
        "exit_code": proc.returncode,
        "stdout": redact_diagnostic_text(stdout),
        "stderr": redact_diagnostic_text(stderr),
    }


def run_ssh(
    user: str,
    host: str,
    command: str,
    *,
    timeout: int = 120,
) -> tuple[bool, int, Any]:
    """Run a command on a remote box over SSH."""
    ssh_opts = [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
    ]
    cmd = ["ssh", *ssh_opts, "--", f"{user}@{host}", command]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, -1, {
            "error": {
                "type": "ssh_not_found",
                "message": "ssh not found.",
                "recoverable": False,
            }
        }
    except subprocess.TimeoutExpired:
        return False, -1, {
            "error": {
                "type": "timeout",
                "message": f"SSH command timed out after {timeout}s.",
                "recoverable": True,
                "recovery_hint": "The box may be unreachable. Check operator_box_status.",
            }
        }

    stdout = proc.stdout.strip()
    ok = proc.returncode == 0

    # Try JSON parse
    if stdout:
        try:
            return ok, proc.returncode, _redact_diagnostic_value(json.loads(stdout))
        except json.JSONDecodeError:
            pass

    return ok, proc.returncode, {
        "exit_code": proc.returncode,
        "stdout": redact_diagnostic_text(stdout),
        "stderr": redact_diagnostic_text(proc.stderr.strip()),
    }


# ---------------------------------------------------------------------------
# Inventory helpers (read-only, for box_exec routing)
# ---------------------------------------------------------------------------

def load_inventory() -> list[dict]:
    inv_path = REPO_ROOT / "workspace" / "boxes.json"
    override = os.environ.get("SKILLBOX_BOX_INVENTORY", "").strip()
    if override:
        inv_path = Path(override)
    if not inv_path.is_file():
        return []
    data = json.loads(inv_path.read_text(encoding="utf-8"))
    return data.get("boxes", [])


def find_box(box_id: str) -> dict | None:
    for b in load_inventory():
        if b.get("id") == box_id:
            return b
    return None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_operator_profiles(_params: dict) -> dict:
    ok, _code, data = run_script(BOX_PY, ["profiles", "--format", "json"])
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_boxes(_params: dict) -> dict:
    ok, _code, data = run_script(BOX_PY, ["list", "--format", "json"])
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_box_status(params: dict) -> dict:
    args = ["status", "--format", "json"]
    if "box_id" in params and params["box_id"] is not None:
        try:
            box_id_param = _validate_string_identifier(params["box_id"], "box_id")
        except ValueError as exc:
            return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})
        args.insert(1, box_id_param)
    ok, _code, data = run_script(BOX_PY, args)
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_provision(params: dict) -> dict:
    if "box_id" not in params or params["box_id"] is None:
        return _missing_required_error(
            "operator_provision",
            "'box_id' is required for operator_provision.",
            [
                "operator_boxes",
                "operator_profiles",
                "operator_provision(box_id='<id>', dry_run=true)",
            ],
        )
    box_id = params["box_id"]

    try:
        box_id_param = _validate_string_identifier(box_id, "box_id")
    except ValueError as exc:
        return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})

    profile_param = ""
    if "profile" in params and params["profile"] is not None:
        try:
            profile_param = _validate_string_identifier(params["profile"], "profile", trim=True)
        except ValueError as exc:
            return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})
    blueprint_param = None
    if "blueprint" in params and params["blueprint"] is not None:
        try:
            blueprint_param = _validate_string_identifier(params["blueprint"], "blueprint")
        except ValueError as exc:
            return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})
    deploy_manifest_param = None
    if "deploy_manifest" in params and params["deploy_manifest"] is not None:
        try:
            deploy_manifest_param = _validate_string(params["deploy_manifest"], "deploy_manifest")
        except ValueError as exc:
            return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})
    set_vars_param = []
    if "set_vars" in params and params["set_vars"] is not None:
        if not isinstance(params["set_vars"], list):
            return _error_content({
                "error": {
                    "type": "invalid_parameter",
                    "message": "Invalid set_vars: must be an array",
                    "recoverable": True,
                }
            })
        for sv in params["set_vars"]:
            try:
                set_vars_param.append(_validate_string(sv, "set_vars item"))
            except ValueError as exc:
                return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})
    try:
        resume_param = _validate_optional_bool(params, "resume")
        dry_run_param = _validate_optional_bool(params, "dry_run")
    except ValueError as exc:
        return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})

    args = ["up", box_id_param, "--format", "json"]
    if profile_param:
        args += ["--profile", profile_param]
    if deploy_manifest_param:
        args += ["--deploy-manifest", deploy_manifest_param]
    if blueprint_param:
        args += ["--blueprint", blueprint_param]
    for sv in set_vars_param:
        args += ["--set", sv]
    if resume_param:
        args.append("--resume")
    if dry_run_param:
        args.append("--dry-run")
    elif not _has_dryrun_marker("operator_provision", box_id_param):
        return _dry_run_required_error(
            "operator_provision",
            box_id_param,
            "operator_provision(box_id='<id>', dry_run=true)",
            "python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json",
            marker_status=_dryrun_marker_rejection_status("operator_provision", box_id_param),
        )

    ok, _code, data = run_script(BOX_PY, args, timeout=PROVISION_TIMEOUT_SECONDS)
    emit_event(
        "operator.provision",
        box_id_param,
        {"ok": ok, "dry_run": dry_run_param, "resume": resume_param},
    )
    if ok and dry_run_param:
        _stamp_dryrun_marker("operator_provision", box_id_param)
    elif ok and not dry_run_param:
        _clear_dryrun_marker("operator_provision", box_id_param)
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_teardown(params: dict) -> dict:
    if "box_id" not in params or params["box_id"] is None:
        return _missing_required_error(
            "operator_teardown",
            "'box_id' is required for operator_teardown.",
            [
                "operator_boxes",
                "operator_box_status",
                "operator_teardown(box_id='<id>', dry_run=true)",
            ],
        )

    try:
        box_id_param = _validate_string_identifier(params["box_id"], "box_id")
    except ValueError as exc:
        return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})
    try:
        dry_run_param = _validate_optional_bool(params, "dry_run")
    except ValueError as exc:
        return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})

    args = ["down", box_id_param, "--format", "json"]
    if dry_run_param:
        args.append("--dry-run")
    elif not _has_dryrun_marker("operator_teardown", box_id_param):
        return _dry_run_required_error(
            "operator_teardown",
            box_id_param,
            "operator_teardown(box_id='<id>', dry_run=true)",
            "python3 scripts/box.py down <box-id> --dry-run --format json",
            marker_status=_dryrun_marker_rejection_status("operator_teardown", box_id_param),
        )

    ok, _code, data = run_script(BOX_PY, args, timeout=300)
    emit_event("operator.teardown", box_id_param, {"ok": ok, "dry_run": dry_run_param})

    # Stamp dry-run marker so the PreToolUse hook allows the real run next.
    if ok and dry_run_param:
        _stamp_dryrun_marker("operator_teardown", box_id_param)
    elif ok and not dry_run_param:
        _clear_dryrun_marker("operator_teardown", box_id_param)

    return _ok_content(data) if ok else _error_content(data)


def handle_operator_box_exec(params: dict) -> dict:
    if (
        "box_id" not in params
        or params["box_id"] is None
        or "command" not in params
        or params["command"] is None
    ):
        return _missing_required_error(
            "operator_box_exec",
            "'box_id' and 'command' are required for operator_box_exec.",
            [
                "operator_boxes",
                "operator_box_status",
                "operator_box_exec(box_id='<id>', command='cd ~/skillbox && python3 .env-manager/manage.py status --format json')",
            ],
        )

    try:
        box_id_param = _validate_string_identifier(params["box_id"], "box_id")
        command_param = _validate_string(params["command"], "command")
    except ValueError as exc:
        return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})
    if not command_param:
        return _missing_required_error(
            "operator_box_exec",
            "'box_id' and 'command' are required for operator_box_exec.",
            [
                "operator_boxes",
                "operator_box_status",
                "operator_box_exec(box_id='<id>', command='cd ~/skillbox && python3 .env-manager/manage.py status --format json')",
            ],
        )

    box = find_box(box_id_param)
    if box is None or box.get("state") == "destroyed":
        return _error_content({
            "error": {
                "type": "box_not_found",
                "message": f"Box '{box_id_param}' not found or destroyed.",
                "recoverable": True,
                "recovery_hint": (
                    "Run operator_boxes to list active boxes, or register an existing shared box "
                    "with `python3 scripts/box.py register <id> --host <tailscale-hostname>`."
                ),
            }
        })

    host = box.get("tailscale_ip") or box.get("tailscale_hostname") or box.get("droplet_ip")
    user = box.get("ssh_user", "skillbox")
    if not host:
        return _error_content({
            "error": {
                "type": "no_ssh_target",
                "message": f"Box '{box_id_param}' has no reachable address.",
                "recoverable": False,
            }
        })

    try:
        validated_user = _validate_ssh_user(str(user), "ssh_user")
        validated_host = _validate_host(str(host), "host")
    except ValueError as exc:
        return _error_content({
            "error": {
                "type": "invalid_box_config",
                "message": str(exc),
                "recoverable": False,
                "recovery_hint": (
                    "Inventory entry for this box has an unsafe ssh_user or host. "
                    "Fix workspace/boxes.json (or re-register the box) before retrying."
                ),
            }
        })

    try:
        timeout = _validate_int(params["timeout"], "timeout") if "timeout" in params and params["timeout"] is not None else 120
    except ValueError:
        return _error_content({
            "error": {
                "type": "invalid_parameter",
                "message": "timeout must be an integer number of seconds.",
                "recoverable": True,
            }
        })

    try:
        dry_run_param = _validate_optional_bool(params, "dry_run")
    except ValueError as exc:
        return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})

    # --- Command policy gate (server-side, every-client) -------------------
    classification = classify_box_exec_command(command_param)
    marker_key = _box_exec_marker_key(box_id_param, command_param)
    cmd_hash = command_hash(command_param)

    # Read-only allowlisted commands run unconditionally — no dry-run friction.
    if classification["verdict"] == "read-only" and not dry_run_param:
        emit_box_exec_audit(
            box_id_param,
            command_param,
            verdict="allow-readonly",
            reason=classification["reason"],
        )
        ok, _code, data = run_ssh(validated_user, validated_host, command_param, timeout=timeout)
        return _ok_content(data) if ok else _error_content(data)

    # Mutating/unknown (or an explicit dry_run). In dry_run mode we preview the
    # EXACT command and stamp a marker bound to box_id + command hash.
    if dry_run_param:
        _stamp_dryrun_marker("operator_box_exec", marker_key)
        emit_box_exec_audit(
            box_id_param,
            command_param,
            verdict="preview",
            reason=classification["reason"],
            dry_run=True,
        )
        payload: dict[str, Any] = {
            "dry_run": True,
            "box_id": box_id_param,
            "classification": classification["verdict"],
            "reason": classification["reason"],
            "would_run": {
                "ssh_user": validated_user,
                "host": validated_host,
                "command": command_param,
                "command_hash": cmd_hash,
                "timeout": timeout,
            },
            "next_actions": [
                "Confirm the command above with the user, then re-issue the IDENTICAL "
                "operator_box_exec call WITHOUT dry_run to execute it.",
            ],
        }
        dcg = _dcg_verdict(command_param)
        if dcg is not None:
            payload["dcg"] = dcg
        return _ok_content(payload)

    # Mutating, non-dry-run: require a fresh marker bound to THIS command.
    if not _has_dryrun_marker("operator_box_exec", marker_key):
        emit_box_exec_audit(
            box_id_param,
            command_param,
            verdict="reject",
            reason=f"no dry-run marker for command hash {cmd_hash}: {classification['reason']}",
        )
        marker_status = _dryrun_marker_rejection_status("operator_box_exec", marker_key)
        ttl_seconds = marker_status.get("ttl_seconds")
        age_seconds = marker_status.get("age_seconds")
        if age_seconds is None:
            marker_note = f"no marker for this command; configured marker ttl is {ttl_seconds}s"
        else:
            marker_note = f"observed marker age is {age_seconds}s; configured marker ttl is {ttl_seconds}s"
        return _error_content({
            "error": {
                "type": "dry_run_required",
                "message": (
                    f"operator_box_exec classified this command as '{classification['verdict']}' "
                    f"({classification['reason']}). A mutating/unknown command requires a successful "
                    f"dry_run=true preview of the IDENTICAL command first ({marker_note})."
                ),
                "recoverable": True,
                "subject": box_id_param,
                "classification": classification["verdict"],
                "command_hash": cmd_hash,
                "marker": {
                    "exists": bool(marker_status.get("exists")),
                    "expired": bool(marker_status.get("expired")),
                    "age_seconds": age_seconds,
                    "ttl_seconds": ttl_seconds,
                },
                "next_actions": [
                    {
                        "tool": "operator_box_exec",
                        "arguments": {
                            "box_id": box_id_param,
                            "command": command_param,
                            "dry_run": True,
                        },
                    },
                ],
            }
        })

    # Marker present and valid — authorize a single real run, then consume it.
    emit_box_exec_audit(
        box_id_param,
        command_param,
        verdict="allow-marker",
        reason=f"matching dry-run marker for command hash {cmd_hash}",
    )
    ok, _code, data = run_ssh(validated_user, validated_host, command_param, timeout=timeout)
    if ok:
        _clear_dryrun_marker("operator_box_exec", marker_key)
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_compose_up(params: dict) -> dict:
    results: list[dict[str, Any]] = []

    try:
        build_param = _validate_optional_bool(params, "build", default=True)
        surfaces_param = _validate_optional_bool(params, "surfaces")
    except ValueError as exc:
        return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})

    if build_param:
        ok, code, data = run_compose(["build"], timeout=600)
        results.append({"step": "build", "ok": ok, "exit_code": code, "detail": data})
        if not ok:
            return _error_content({
                "steps": results,
                "error": {"type": "build_failed", "message": "docker compose build failed.", "recoverable": True},
            })

    ok, code, data = run_compose(["up", "-d"], timeout=120)
    results.append({"step": "up", "ok": ok, "exit_code": code, "detail": data})
    headline_ok = ok
    if not headline_ok:
        emit_event("operator.compose_up", "local", {"ok": False, "headline_ok": False})
        return _error_content({
            "steps": results,
            "headline_step": "up",
            "headline_ok": False,
            "error": {"type": "up_failed", "message": "docker compose up failed.", "recoverable": True},
        })

    if surfaces_param and ok:
        ok_s, code_s, data_s = run_compose(["--profile", "surfaces", "up", "-d"], timeout=60)
        results.append({"step": "up-surfaces", "ok": ok_s, "exit_code": code_s, "detail": data_s})

    partial_failures = [step for step in results if not step["ok"]]
    all_ok = not partial_failures
    emit_event("operator.compose_up", "local", {"ok": headline_ok, "headline_ok": headline_ok, "all_steps_ok": all_ok})
    payload = {
        "steps": results,
        "headline_step": "up",
        "headline_ok": headline_ok,
        "partial_failures": partial_failures,
        "next_actions": ["operator_doctor"] if all_ok else ["Inspect steps[] for optional surface failures."],
    }
    return _ok_content(payload)


def handle_operator_compose_down(params: dict) -> dict:
    try:
        is_dry_run = _validate_optional_bool(params, "dry_run")
    except ValueError as exc:
        return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})
    if is_dry_run:
        # Compose doesn't have native dry-run; simulate it.
        ok, code, data = run_compose(["ps", "--format", "json"], timeout=30)
        if not ok:
            return _error_content({
                "dry_run": True,
                "action": "compose down",
                "exit_code": code,
                "detail": data,
                "error": {
                    "type": "compose_preview_failed",
                    "message": "docker compose ps failed during compose-down preview.",
                    "recoverable": True,
                },
            })
        payload = {
            "dry_run": True,
            "action": "compose down",
            "would_stop": data,
            "next_actions": ["Run operator_compose_down without dry_run to proceed."],
        }
        _stamp_dryrun_marker("operator_compose_down", "local")
        return _ok_content(payload)

    if not _has_dryrun_marker("operator_compose_down", "local"):
        return _dry_run_required_error(
            "operator_compose_down",
            "local",
            "operator_compose_down(dry_run=true)",
            "docker compose ps --format json",
            marker_status=_dryrun_marker_rejection_status("operator_compose_down", "local"),
        )

    ok, code, data = run_compose(["down"], timeout=120)
    emit_event("operator.compose_down", "local", {"ok": ok})
    if ok:
        _clear_dryrun_marker("operator_compose_down", "local")
    payload = {"ok": ok, "exit_code": code, "detail": data}
    return _ok_content(payload) if ok else _error_content(payload)


def handle_operator_doctor(_params: dict) -> dict:
    ok, _code, data = run_script(RECONCILE_PY, ["doctor", "--format", "json"])
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_render(params: dict) -> dict:
    args = ["render", "--format", "json"]
    try:
        with_compose_param = _validate_optional_bool(params, "with_compose")
    except ValueError as exc:
        return _error_content({"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}})
    if with_compose_param:
        args.append("--with-compose")
    ok, _code, data = run_script(RECONCILE_PY, args)
    return _ok_content(data) if ok else _error_content(data)


# ---------------------------------------------------------------------------
# Event journal (operator-side, same JSONL format as manage.py)
# ---------------------------------------------------------------------------

def emit_event(event_type: str, subject: str, detail: dict | None = None) -> None:
    """Append an event to the operator-level journal."""
    import time as _time
    journal_path = REPO_ROOT / "logs" / "runtime" / "journal.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _time.time(),
        "type": event_type,
        "subject": subject,
        "detail": detail or {},
    }
    try:
        with journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
    except OSError:
        pass


def emit_box_exec_audit(
    box_id: str,
    command: str,
    *,
    verdict: str,
    reason: str,
    dry_run: bool = False,
) -> None:
    """Record an audit event for EVERY operator_box_exec invocation.

    Logs box_id, the command HASH (never raw secrets), a REDACTED command
    preview, the gate verdict (allow-readonly / allow-marker / reject /
    preview), and a human reason. The raw command is redacted (KEY=value and
    bearer-token shaped substrings) before it ever touches the journal so a
    command carrying a secret cannot leak it into the audit trail.
    """
    emit_event(
        "operator.box_exec",
        box_id,
        {
            "verdict": verdict,
            "reason": reason,
            "dry_run": dry_run,
            "command_hash": command_hash(command),
            "command_redacted": redact_diagnostic_text(normalize_command(command)),
        },
    )


# ---------------------------------------------------------------------------
# Dry-run marker (coordinates with PreToolUse hook)
# ---------------------------------------------------------------------------

def _dryrun_marker_path(tool_name: str, box_id: str) -> Path:
    """Return the marker path after validating identifiers."""
    _validate_identifier(tool_name, "tool_name")
    _validate_identifier(box_id, "box_id")
    return REPO_ROOT / ".skillbox-state" / "dryrun-markers" / f".skillbox-dryrun-{tool_name}-{box_id}"


def _box_exec_marker_key(box_id: str, command: str) -> str:
    """Marker subject for operator_box_exec, binding box_id + command hash.

    The marker store keys on a single slug; we combine the (already validated)
    box_id with the normalized-command hash so a marker minted for command A on
    box X cannot authorize command B (different hash) or command A on box Y
    (different box_id). To stay within the 64-char identifier limit for any
    box_id length, the box_id is folded into a short hash and joined with the
    command hash: ``{box_hash}.{command_hash}`` (only ``[a-z0-9.]``). Distinct
    box_ids and distinct (normalized) commands therefore land on distinct
    markers; identical ones collide intentionally.
    """
    box_hash = hashlib.sha256(box_id.encode("utf-8")).hexdigest()[:16]
    return f"{box_hash}.{command_hash(command)}"


def _dryrun_marker_ttl_seconds() -> int:
    raw_ttl = str(os.environ.get("SKILLBOX_DRYRUN_MARKER_TTL_SECONDS") or "").strip()
    if raw_ttl:
        try:
            ttl = int(raw_ttl)
        except ValueError:
            ttl = DRYRUN_MARKER_TTL_SECONDS
        else:
            if ttl > 0:
                return ttl
    return DRYRUN_MARKER_TTL_SECONDS


def _stamp_dryrun_marker(tool_name: str, box_id: str) -> None:
    """Create a temp marker so the PreToolUse hook knows a dry-run was done."""
    marker = _dryrun_marker_path(tool_name, box_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"dry-run completed for {tool_name} box={box_id}\n")


def _dryrun_marker_status(tool_name: str, box_id: str) -> dict[str, Any]:
    marker = _dryrun_marker_path(tool_name, box_id)
    ttl_seconds = _dryrun_marker_ttl_seconds()
    status: dict[str, Any] = {
        "path": str(marker),
        "exists": False,
        "valid": False,
        "expired": False,
        "age_seconds": None,
        "ttl_seconds": ttl_seconds,
    }
    if not marker.is_file():
        return status
    status["exists"] = True
    try:
        age_seconds = max(0, int(time.time() - marker.stat().st_mtime))
    except OSError:
        return status
    status["age_seconds"] = age_seconds
    status["expired"] = age_seconds > ttl_seconds
    status["valid"] = not status["expired"]
    return status


def _has_dryrun_marker(tool_name: str, box_id: str) -> bool:
    """Check if a valid, non-expired dry-run marker exists."""
    status = _dryrun_marker_status(tool_name, box_id)
    cache_key = (tool_name, box_id)
    if status["valid"]:
        _DRYRUN_MARKER_STATUS_CACHE.pop(cache_key, None)
    else:
        _DRYRUN_MARKER_STATUS_CACHE[cache_key] = status
    if status["expired"]:
        # Expired — clean up and report absent.
        try:
            Path(str(status["path"])).unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return bool(status["valid"])


def _dryrun_marker_rejection_status(tool_name: str, box_id: str) -> dict[str, Any]:
    return _DRYRUN_MARKER_STATUS_CACHE.pop((tool_name, box_id), None) or _dryrun_marker_status(tool_name, box_id)


def _clear_dryrun_marker(tool_name: str, box_id: str) -> None:
    """Remove the dry-run marker after a successful real operation."""
    try:
        marker = _dryrun_marker_path(tool_name, box_id)
        marker.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------

def _ok_content(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, sort_keys=True, default=str)}]}


def _error_content(data: Any) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(data, indent=2, sort_keys=True, default=str)}],
        "isError": True,
    }


def _missing_required_error(tool_name: str, message: str, next_actions: list[str]) -> dict:
    return _error_content({
        "error": {
            "type": "missing_required_parameter",
            "message": message,
            "recoverable": True,
            "next_actions": next_actions,
        }
    })


def _dry_run_required_error(
    tool_name: str,
    subject: str,
    safe_first_call: str,
    exact_cli: str,
    *,
    marker_status: dict[str, Any] | None = None,
) -> dict:
    marker = marker_status or {"ttl_seconds": _dryrun_marker_ttl_seconds(), "age_seconds": None}
    ttl_seconds = marker.get("ttl_seconds")
    age_seconds = marker.get("age_seconds")
    if age_seconds is None:
        marker_note = f"no marker age observed; configured marker ttl is {ttl_seconds}s"
    else:
        marker_note = f"observed marker age is {age_seconds}s; configured marker ttl is {ttl_seconds}s"
    return _error_content({
        "error": {
            "type": "dry_run_required",
            "message": (
                f"{tool_name} requires a successful dry_run=true preview before the real operation "
                f"({marker_note})."
            ),
            "recoverable": True,
            "subject": subject,
            "marker": {
                "exists": bool(marker.get("exists")),
                "expired": bool(marker.get("expired")),
                "age_seconds": age_seconds,
                "ttl_seconds": ttl_seconds,
            },
            "next_actions": [safe_first_call, exact_cli],
        }
    })


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, Any] = {
    "operator_profiles":     handle_operator_profiles,
    "operator_boxes":        handle_operator_boxes,
    "operator_box_status":   handle_operator_box_status,
    "operator_provision":    handle_operator_provision,
    "operator_teardown":     handle_operator_teardown,
    "operator_box_exec":     handle_operator_box_exec,
    "operator_compose_up":   handle_operator_compose_up,
    "operator_compose_down": handle_operator_compose_down,
    "operator_doctor":       handle_operator_doctor,
    "operator_render":       handle_operator_render,
}


def dispatch_tool(name: str, params: dict) -> dict:
    handler = _DISPATCH.get(name)
    if handler is None:
        return _error_content({
            "error": {
                "type": "unknown_tool",
                "message": f"Unknown tool: '{name}'.",
                "available_tools": sorted(_DISPATCH.keys()),
                "next_actions": ["operator_boxes", "operator_profiles", "operator_doctor"],
                "recoverable": False,
            }
        })
    return handler(params)


# ---------------------------------------------------------------------------
# MCP protocol handlers
# ---------------------------------------------------------------------------

def handle_initialize(_params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "skillbox operator — fleet and container lifecycle from outside the box. "
            "1. Run operator_boxes to see the current fleet. "
            "2. Run operator_profiles to see available box sizes. "
            "3. Use operator_provision with dry_run=true before creating infrastructure and inspect "
            "credential_status; missing credentials must be added by the operator to the operator "
            "secret file (${SKILLBOX_STATE_ROOT}/operator/.env.box, default "
            "./.skillbox-state/operator/.env.box) — NOT to the repo root, which is readable by "
            "in-container agents. "
            "4. CONFIRM WITH USER before operator_teardown — it destroys infrastructure. "
            "5. Use operator_box_exec to run commands on remote boxes. Read-only inspection "
            "commands run immediately; a MUTATING or unknown command is rejected until you "
            "preview the IDENTICAL command with dry_run=true (which stamps a per-command marker). "
            "6. Use operator_doctor to validate the local repo state. "
            "SAFETY: Destructive tools (teardown, compose_down) AND mutating operator_box_exec "
            "commands are gated server-side and by a PreToolUse hook. The gate BLOCKS execution if: "
            "(a) there are uncommitted changes (run /commit first), or (b) no matching dry_run=true "
            "was run first. Always dry-run, then confirm with user, then execute."
        ),
    }


def handle_tools_list() -> dict:
    return {"tools": TOOLS}


def handle_tools_call(params: dict) -> dict:
    return dispatch_tool(params.get("name", ""), params.get("arguments") or {})


# ---------------------------------------------------------------------------
# JSON-RPC stdio loop
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Any] = {
    "initialize":  lambda p: handle_initialize(p),
    "tools/list":  lambda _p: handle_tools_list(),
    "tools/call":  lambda p: handle_tools_call(p),
    "ping":        lambda _p: {},
}


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, sort_keys=True) + "\n")
    sys.stdout.flush()


def send_error(msg_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def main() -> None:
    load_operator_secret(".env")
    load_operator_secret(".env.box")
    print(f"[operator-mcp] starting — repo: {REPO_ROOT}", file=sys.stderr, flush=True)

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            send_error(None, -32700, f"Parse error: {exc}")
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        if msg_id is None:
            continue

        handler = _HANDLERS.get(method)
        if handler is None:
            send_error(msg_id, -32601, f"Method not found: {method}")
            continue

        try:
            result = handler(params)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            print(f"[operator-mcp] error in {method}: {exc}", file=sys.stderr, flush=True)
            send_error(msg_id, -32603, f"Internal error in {method}")
            continue

        send({"jsonrpc": "2.0", "id": msg_id, "result": result})


if __name__ == "__main__":
    main()
