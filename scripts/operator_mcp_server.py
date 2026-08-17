#!/usr/bin/env python3
"""
skillbox operator MCP server — fleet and container lifecycle as native agent tools.

Runs on the operator's machine (outside the container).
Wraps box.py (DO+Tailscale fleet), docker compose (container lifecycle),
and 04-reconcile.py (outer validation) as MCP tools.

Protocol: JSON-RPC 2.0 over stdio (MCP 2024-11-05).
"""
from __future__ import annotations

import contextlib
import json
import os

# Not referenced directly in this module, but it IS part of the module's
# patchable surface: tests/test_operator_mcp_server.py patches
# mock.patch.object(MODULE.subprocess, "run", ...) to drive the run_ssh and
# run_script failure paths. Removing it breaks 8 tests with
# "module has no attribute 'subprocess'".
import subprocess  # noqa: F401
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
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

# Shared operator-side validation, inventory containment, and subprocess
# helpers live in lib.opslib. Redaction aliases are preserved because call
# sites (including the box_exec audit path) and tests reference these names.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from lib.opslib import (  # noqa: E402
    MARKER_SESSION_SCOPE_SESSION,
    MARKER_SOURCE_OPERATOR_MCP,
    StateLeaseUnavailable,
    active_state_lease,
    box_exec_marker_key as _opslib_box_exec_marker_key,
    classify_box_exec_command,
    command_hash,
    dryrun_marker_payload,
    marker_session_scope,
    normalize_command,
    resolve_inventory_path,
    run_checked,
    state_root_lease,
    validate_host,
    validate_identifier,
    validate_ssh_user,
)
from lib.redaction import (  # noqa: E402
    redact_text as redact_diagnostic_text,
    redact_value as _redact_diagnostic_value,
)

# The DCG adapter itself is hoisted into lib.dcglib and shared byte-for-byte
# with `python3 scripts/box.py exec`, so the two operator surfaces cannot give
# different allow/deny answers for the same command. The names below stay in
# THIS module's namespace because call sites — and tests that patch by module
# namespace — reference them here.
from lib import dcglib as _dcglib  # noqa: E402

# The DCG version pin lives in ONE place: .env-manager/runtime_manager/
# dcg_distribution.py. This server consumes it instead of re-declaring a
# version string. The import is guarded so a missing/broken runtime_manager
# cannot take the whole MCP server down at import time — but it is NOT a
# fallback: with no pin we cannot prove the binary is compatible, so the DCG
# adapter treats a failed import as "incompatible" and FAILS CLOSED.
_ENV_MANAGER_DIR = REPO_ROOT / ".env-manager"
DCG_PINNED_VERSION, DCG_PIN_IMPORT_ERROR, _dcg_normalize_version = _dcglib.load_pinned_version(
    _ENV_MANAGER_DIR
)

DRYRUN_MARKER_TTL_SECONDS = 600  # 10 minutes
_DRYRUN_MARKER_STATUS_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# operator_box_exec command policy (server-side gate)
#
# operator_box_exec runs ARBITRARY shell over Tailscale SSH on any inventory
# box. Unlike teardown/compose_down (single fixed effect), the command itself
# is the payload, so the gate lives here on the server (works for every MCP
# client, like the provision dry-run gate) rather than only in the hook.
#
# The CLASSIFIER and the MARKER KEY are pure and now live in lib.opslib, shared
# byte-for-byte with `python3 scripts/box.py exec` so a preview taken through
# either surface authorizes the other. They are re-exported into this module's
# namespace because call sites (and tests that patch by module namespace)
# reference them here.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# DCG (destructive command guard) adapter — FAIL CLOSED
#
# Interface: the supported DCG 0.6.7 robot surface,
#   dcg test --robot --format json --no-color -- <command>
# which STATICALLY evaluates <command> against the enabled packs and prints a
# single JSON object. Exit 0 = allow, 1 = deny, 3/4/5 = config/parse/IO error.
#
# SAFETY INVARIANT (this module's whole reason to exist): the command under
# inspection is passed as ONE argv element to `dcg test`. It is never handed to
# a shell, never `input`-piped into an interpreter, and `dcg test` itself does
# not execute it. Nothing on this path can run the payload.
#
# Authoritative vs advisory:
#   * AUTHORITATIVE — every site that is about to actually execute the command
#     (both `run_ssh` branches in handle_operator_box_exec). A missing binary,
#     a timeout, malformed JSON, an incompatible version, or an unrecognized
#     response all resolve to DENY. There is no "no verdict" outcome.
#   * NON-AUTHORITATIVE — `handle_operator_box_exec` dry_run preview
#     (DCG_ADVISORY_SITES below). A preview executes nothing, so the verdict is
#     reported for the operator's benefit and an unavailable guard degrades to
#     an annotated advisory instead of blocking the preview. The real run that
#     the preview authorizes is still gated authoritatively.
# ---------------------------------------------------------------------------

DCG_BINARY_NAME = _dcglib.DCG_BINARY_NAME
DCG_BINARY_ENV = _dcglib.DCG_BINARY_ENV
DCG_EVAL_TIMEOUT_SECONDS = _dcglib.DCG_EVAL_TIMEOUT_SECONDS
DCG_ROBOT_SCHEMA_VERSION = _dcglib.DCG_ROBOT_SCHEMA_VERSION
DCG_INTERFACE = _dcglib.DCG_INTERFACE

# The ONLY call sites permitted to treat a DCG failure as non-blocking. Each is
# named here and covered by a dedicated non-authoritative test.
DCG_ADVISORY_SITES = ("operator_box_exec:dry_run_preview",)

# Decision strings understood by this adapter. Anything else is an
# "unsupported_response" and fails closed.
_DCG_ALLOW_DECISIONS = _dcglib.DCG_ALLOW_DECISIONS
_DCG_DENY_DECISIONS = _dcglib.DCG_DENY_DECISIONS


def _dcg_binary_path() -> str:
    """Resolve the pinned DCG binary, or "" when it cannot be found.

    Order: explicit ``SKILLBOX_DCG_BIN`` override, then ``PATH``, then the
    default install target ``~/.local/bin/dcg`` used by the distribution
    contract. Returning "" is a fail-closed signal, never a skip.
    """
    return _dcglib.resolve_dcg_binary()


def _dcg_result(
    verdict: str,
    reason_code: str,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build the adapter's stable verdict record.

    ``verdict`` is one of ``allow`` / ``deny`` / ``unavailable``. Callers must
    never infer "no opinion" from this: :func:`dcg_blocks_execution` maps
    anything that is not ``allow`` to a block.
    """
    return _dcglib.dcg_result(
        verdict,
        reason_code,
        reason,
        expected_version=DCG_PINNED_VERSION,
        interface=DCG_INTERFACE,
        **extra,
    )


def dcg_blocks_execution(verdict: dict[str, Any] | None) -> bool:
    """True unless DCG explicitly allowed the command.

    ``None``, ``unavailable``, and ``deny`` all block. This is the single
    predicate every authoritative call site uses, so "silently no verdict"
    is not expressible.
    """
    return _dcglib.dcg_blocks_execution(verdict)


def evaluate_command_with_dcg(
    command: str,
    *,
    timeout: int = DCG_EVAL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Statically evaluate *command* with the pinned DCG binary.

    NEVER executes *command*: it is passed as a single argv element to
    ``dcg test``, which only pattern-matches it against the enabled packs.

    Always returns a verdict record. Every failure mode — no pin, no binary,
    spawn failure, timeout, non-JSON output, wrong schema, wrong version,
    unrecognized decision — returns ``verdict="unavailable"``, which
    :func:`dcg_blocks_execution` treats as a block.

    The implementation lives in :mod:`lib.dcglib` and is shared with
    ``scripts/box.py exec``. Its dependencies are resolved from THIS module's
    namespace at call time, so patching ``MODULE.run_checked`` /
    ``MODULE._dcg_binary_path`` / ``MODULE.DCG_PINNED_VERSION`` still drives it.
    """
    return _dcglib.evaluate_command(
        command,
        timeout=timeout,
        pinned_version=DCG_PINNED_VERSION,
        pin_import_error=DCG_PIN_IMPORT_ERROR,
        resolve_binary=_dcg_binary_path,
        run_command=run_checked,
        redact=redact_diagnostic_text,
        normalize_version=_dcg_normalize_version,
        interface=DCG_INTERFACE,
        schema_version=DCG_ROBOT_SCHEMA_VERSION,
    )


def dcg_advisory(command: str, *, site: str) -> dict[str, Any]:
    """Verdict for an explicitly NON-AUTHORITATIVE call site.

    *site* must be one of :data:`DCG_ADVISORY_SITES`. The returned record is
    identical to the authoritative one plus ``authoritative: False`` and the
    site name, so an operator reading a preview can see that an unavailable
    guard did not block *this* step — and will block the real run.
    """
    if site not in DCG_ADVISORY_SITES:
        raise ValueError(f"{site!r} is not a declared non-authoritative DCG site")
    return _dcglib.annotate_advisory(evaluate_command_with_dcg(command), site=site)


#: Name this surface answers to in a refusal. `box.py exec` passes its own.
DCG_SURFACE_NAME = "operator_box_exec"


def _dcg_denied_error(box_id: str, command: str, verdict: dict[str, Any]) -> dict[str, Any]:
    """Structured MCP error for an authoritative DCG block. Nothing ran.

    The refusal text and remediation come from :func:`lib.dcglib.dcg_denial`,
    which `box.py exec` also uses, so both surfaces refuse for the same stated
    reason with the same next actions — only the envelope differs.
    """
    denial = _dcglib.dcg_denial(DCG_SURFACE_NAME, verdict)
    return {
        "error": {
            "type": denial["error_type"],
            "message": denial["message"],
            "recoverable": denial["recoverable"],
            "subject": box_id,
            "command_hash": command_hash(command),
            "executed": False,
            "dcg": verdict,
            "next_actions": denial["next_actions"],
        }
    }


def _validate_identifier(value: str, kind: str) -> str:
    """Validate that *value* is a safe slug identifier.

    Rejects path separators, leading dashes, and anything not matching
    the slug pattern ``^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$``.

    Returns the validated value on success; raises ValueError otherwise.
    """
    return validate_identifier(value, kind)


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
    return validate_ssh_user(value, kind=kind)


def _validate_host(value: str, kind: str = "host") -> str:
    return validate_host(value, kind=kind)


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
            exact_cli=(
                "python3 scripts/box.py exec <box-id> --dry-run --format json -- <command>"
            ),
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
            exact_cli="python3 scripts/box.py compose-up --dry-run --format json",
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
            exact_cli="python3 scripts/box.py compose-down --dry-run --format json",
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
# DEPRECATION — skillbox-mcp-deprecation-epic-vniq.4
#
# This server is superseded by the robot CLI. scripts/box.py now carries the
# same gates IN-PROCESS, so dropping the MCP server weakens nothing:
#
#   * clean-tree refusal on every real mutation,
#   * the dry-run marker store, byte-identical to this module's (a preview taken
#     through either surface authorizes the other, and a marker minted for
#     command A can never authorize command B),
#   * the authoritative DCG guard on box.py exec — the same lib.dcglib adapter,
#     at the same two authoritative call sites, failing closed the same way.
#
# The module is KEPT so existing registrations keep working. Every tool
# description now names its CLI replacement, and x_skillbox_contract carries
# deprecated/cli_replacement for machine consumers. New work should use the CLI
# plus skills/box-fleet-operator.
#
# Tool parity is 10/10. doctor/render replace to scripts/04-reconcile.py rather
# than box.py — an accepted boundary, not a gap: outer validation has always
# lived in the reconcile script and this server only shelled out to it.
# ---------------------------------------------------------------------------

DEPRECATED = True
DEPRECATION_REPLACEMENT_SKILL = "skills/box-fleet-operator"
DEPRECATION_SUMMARY = (
    "DEPRECATED: the skillbox-operator MCP server is superseded by the robot CLI "
    "(scripts/box.py), which carries the same clean-tree, dry-run-marker and "
    f"destructive-command-guard gates. See {DEPRECATION_REPLACEMENT_SKILL}."
)


def _deprecation_note(exact_cli: str) -> str:
    return f" {DEPRECATION_SUMMARY} Prefer: `{exact_cli}`."


def _apply_deprecation(tools: list[dict]) -> None:
    """Stamp the CLI replacement onto every tool description and contract."""
    for tool in tools:
        contract = tool["x_skillbox_contract"]
        contract["deprecated"] = True
        contract["cli_replacement"] = contract["exact_cli"]
        tool["description"] = tool["description"] + _deprecation_note(contract["exact_cli"])


_apply_deprecation(TOOLS)

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
# are refused outright — loading a secret file from inside the workspace mount is
# a hard error, never a fallback.
OPERATOR_SECRET_FILENAMES = (".env", ".env.box")


def operator_secret_dir() -> Path:
    """Resolve the canonical operator-secret directory under the state root."""
    state_root = os.environ.get("SKILLBOX_STATE_ROOT", "").strip() or "./.skillbox-state"
    base = Path(state_root)
    if not base.is_absolute():
        base = REPO_ROOT / base
    return (base / "operator").resolve()


def load_operator_secret(name: str) -> None:
    """Load an operator secret file from the sanctioned state-root location.

    Refuses (SystemExit) when only the legacy repo-root copy exists: that path
    sits inside the workspace bind mount, so loading it would hand live
    credentials to any in-container agent. No-op when neither file exists.
    """
    new_path = operator_secret_dir() / name
    legacy_path = REPO_ROOT / name
    if new_path.is_file():
        load_dotenv(new_path)
        return
    if legacy_path.is_file():
        raise SystemExit(
            f"[skillbox] REFUSING to load secrets from {legacy_path}: it is inside the "
            f"workspace bind mount and readable by any in-container agent.\n"
            f"[skillbox] Move it to the sanctioned operator location, then re-run:\n"
            f"    mkdir -p {operator_secret_dir()} && mv {legacy_path} {new_path} && chmod 600 {new_path}"
        )
    # neither present: leave os.environ untouched; existing missing-credential UX handles it.


# box.py subcommands that are read-only and safe to run in-process. Importing
# box once and calling box.main() directly skips the per-request interpreter
# start + module import overhead. Mutating lifecycle commands (up/down/
# upgrade/register/unregister/...) stay on the subprocess path for isolation.
_INPROCESS_BOX_COMMANDS = frozenset({"list", "profiles", "status"})
_BOX_MODULE: Any = None


def _box_module() -> Any:
    global _BOX_MODULE
    if _BOX_MODULE is None:
        import box as box_module  # SCRIPT_DIR is already on sys.path

        _BOX_MODULE = box_module
    return _BOX_MODULE


@contextlib.contextmanager
def _captured_process_output(stdout_file: Any, stderr_file: Any):
    """Redirect fd 1/2 to temp files so in-process command output (including
    output from any child processes it spawns) never leaks into the JSON-RPC
    stdout stream — the same containment subprocess capture gave us."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout_fd = os.dup(1)
    saved_stderr_fd = os.dup(2)
    try:
        os.dup2(stdout_file.fileno(), 1)
        os.dup2(stderr_file.fileno(), 2)
        yield
    finally:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        try:
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        os.dup2(saved_stdout_fd, 1)
        os.dup2(saved_stderr_fd, 2)
        os.close(saved_stdout_fd)
        os.close(saved_stderr_fd)


def _coerce_exit_code(raw: Any) -> int:
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    return 1


def _run_box_in_process(args: list[str]) -> tuple[int, str, str] | None:
    """Run box.py's command dispatch in-process. Returns (rc, stdout, stderr)
    mirroring a captured subprocess, or None when the in-process path is
    unavailable (caller falls back to subprocess)."""
    try:
        box = _box_module()
    except Exception:  # noqa: BLE001 - any bootstrap failure falls back to subprocess.
        return None

    saved_environ = dict(os.environ)
    saved_cwd = os.getcwd()
    try:
        os.chdir(REPO_ROOT)
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, tempfile.TemporaryFile(
            mode="w+", encoding="utf-8"
        ) as stderr_file:
            with _captured_process_output(stdout_file, stderr_file):
                try:
                    rc = _coerce_exit_code(box.main(list(args)))
                except SystemExit as exc:
                    rc = _coerce_exit_code(exc.code)
                except Exception:  # noqa: BLE001 - mirror a crashing subprocess: traceback + exit 1.
                    traceback.print_exc()
                    rc = 1
            stdout_file.seek(0)
            stderr_file.seek(0)
            return rc, stdout_file.read(), stderr_file.read()
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_environ)


def _finalize_script_result(
    script: Path,
    rc: int,
    stdout: str,
    stderr_redacted: str,
) -> tuple[bool, int, Any]:
    """Shared stdout/stderr → (ok, rc, payload) contract for both the
    in-process and subprocess script dispatch paths."""
    if stderr_redacted.strip():
        print(f"[operator-mcp] {script.name} stderr: {stderr_redacted.strip()}", file=sys.stderr, flush=True)

    stdout = stdout.strip()
    if stdout:
        try:
            return rc == 0, rc, _redact_diagnostic_value(json.loads(stdout))
        except json.JSONDecodeError:
            return rc == 0, rc, {"text": redact_diagnostic_text(stdout)}

    return rc == 0, rc, {"exit_code": rc}


def run_script(
    script: Path,
    args: list[str],
    *,
    timeout: int = 300,
) -> tuple[bool, int, Any]:
    """Run a Python script and parse JSON output. Read-only box.py commands
    run in-process (interpreter + import paid once per server); everything
    else runs as a subprocess."""
    if not script.exists():
        return False, -1, {
            "error": {
                "type": "script_not_found",
                "message": f"{script.name} not found at {script}.",
                "recoverable": False,
                "recovery_hint": "Are you running from the skillbox repo root?",
            }
        }

    if script == BOX_PY and args and args[0] in _INPROCESS_BOX_COMMANDS:
        in_process = _run_box_in_process(args)
        if in_process is not None:
            rc, stdout_text, stderr_text = in_process
            return _finalize_script_result(script, rc, stdout_text, redact_diagnostic_text(stderr_text))

    if script == BOX_PY and active_state_lease() is not None:
        # `box.py` acquires the same single-writer lease on the same canonical
        # root. Holding it across a SUBPROCESS child cannot be reused the way an
        # in-process nested owner is -- the child has its own registry and its
        # own file description -- so it would block until the timeout and
        # surface as a mystery hang. Refusing names the bug instead.
        return False, -1, {
            "error": {
                "type": "state_lease_held_across_child",
                "message": (
                    "refusing to spawn box.py while this process holds the state-root "
                    "mutation lease; the child would deadlock waiting for it"
                ),
                "recoverable": False,
                "recovery_hint": (
                    "Release the lease before delegating: the wrapper is not the final "
                    "mutation owner, box.py is."
                ),
            }
        }

    cmd = [sys.executable, str(script)] + args
    result = run_checked(cmd, timeout=timeout, cwd=REPO_ROOT)
    if result.get("error_code") == "TIMEOUT":
        return False, -1, {
            "error": {
                "type": "timeout",
                "message": f"{script.name} timed out after {timeout}s.",
                "recoverable": True,
            }
        }
    if result.get("error_code") == "COMMAND_NOT_FOUND":
        return False, -1, {
            "error": {
                "type": "python_not_found",
                "message": "python executable not found.",
                "recoverable": False,
            }
        }

    return _finalize_script_result(
        script,
        int(result["rc"]),
        str(result.get("stdout") or ""),
        str(result.get("stderr_redacted") or ""),
    )


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
    result = run_checked(cmd, timeout=timeout, cwd=REPO_ROOT)
    if result.get("error_code") == "COMMAND_NOT_FOUND":
        return False, -1, {
            "error": {
                "type": "docker_not_found",
                "message": "docker not found. Install Docker to manage containers.",
                "recoverable": False,
            }
        }
    if result.get("error_code") == "TIMEOUT":
        return False, -1, {
            "error": {
                "type": "timeout",
                "message": f"docker compose timed out after {timeout}s.",
                "recoverable": True,
            }
        }

    stdout = str(result.get("stdout") or "").strip()
    stderr = str(result.get("stderr_redacted") or "").strip()
    rc = int(result["rc"])
    ok = rc == 0

    # Try JSON parse (docker compose ps --format json)
    if stdout:
        try:
            return ok, rc, _redact_diagnostic_value(json.loads(stdout))
        except json.JSONDecodeError:
            pass

    return ok, rc, {
        "exit_code": rc,
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
    result = run_checked(cmd, timeout=timeout)
    if result.get("error_code") == "COMMAND_NOT_FOUND":
        return False, -1, {
            "error": {
                "type": "ssh_not_found",
                "message": "ssh not found.",
                "recoverable": False,
            }
        }
    if result.get("error_code") == "TIMEOUT":
        return False, -1, {
            "error": {
                "type": "timeout",
                "message": f"SSH command timed out after {timeout}s.",
                "recoverable": True,
                "recovery_hint": "The box may be unreachable. Check operator_box_status.",
            }
        }

    stdout = str(result.get("stdout") or "").strip()
    rc = int(result["rc"])
    ok = rc == 0

    # Try JSON parse
    if stdout:
        try:
            return ok, rc, _redact_diagnostic_value(json.loads(stdout))
        except json.JSONDecodeError:
            pass

    return ok, rc, {
        "exit_code": rc,
        "stdout": redact_diagnostic_text(stdout),
        "stderr": redact_diagnostic_text(str(result.get("stderr_redacted") or "").strip()),
    }


# ---------------------------------------------------------------------------
# Inventory helpers (read-only, for box_exec routing)
# ---------------------------------------------------------------------------

def load_inventory() -> list[dict]:
    inv_path = resolve_inventory_path(repo_root=REPO_ROOT)
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
    elif not dry_run_param:
        # CONSUME-ON-DISPATCH: a provision that failed partway leaves a
        # half-built droplet, so the retry needs a fresh preview. (box.py's own
        # dispatch consumes the same marker; this keeps the two in agreement.)
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
        # Preview carries no confirmation: --dry-run is the one path box.py lets
        # through unconfirmed, so stamping a confirmation here would be a lie.
        args.append("--dry-run")
    elif not _has_dryrun_marker("operator_teardown", box_id_param):
        return _dry_run_required_error(
            "operator_teardown",
            box_id_param,
            "operator_teardown(box_id='<id>', dry_run=true)",
            "python3 scripts/box.py down <box-id> --dry-run --format json",
            marker_status=_dryrun_marker_rejection_status("operator_teardown", box_id_param),
        )
    else:
        # A real run only reaches here behind a marker bound to this box id, so
        # the wrapper can satisfy the CLI's identity-bound gate by naming the
        # same box. --confirm <box-id> (never --yes) keeps box.py's exact-match
        # check meaningful: a wrong box_id fails there instead of being waved
        # through by a blanket flag.
        args.extend(["--confirm", box_id_param])

    ok, _code, data = run_script(BOX_PY, args, timeout=300)
    emit_event("operator.teardown", box_id_param, {"ok": ok, "dry_run": dry_run_param})

    # Stamp dry-run marker so the PreToolUse hook allows the real run next.
    if ok and dry_run_param:
        _stamp_dryrun_marker("operator_teardown", box_id_param)
    elif not dry_run_param:
        # CONSUME-ON-DISPATCH: a teardown that failed partway has still
        # destroyed state, so the retry must be previewed again.
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

    def _dcg_gate(audit_verdict: str) -> dict | None:
        """AUTHORITATIVE DCG gate. Returns an error payload, or None to proceed.

        Runs immediately before an actual ``run_ssh``. A deny AND every
        unavailable/malformed/timeout/incompatible outcome block execution;
        there is no path where a missing verdict lets the command through.
        """
        verdict = evaluate_command_with_dcg(command_param)
        if not dcg_blocks_execution(verdict):
            return None
        emit_box_exec_audit(
            box_id_param,
            command_param,
            verdict=audit_verdict,
            reason=f"dcg {verdict['reason_code']}: {verdict['reason']}",
        )
        return _error_content(_dcg_denied_error(box_id_param, command_param, verdict))

    # Read-only allowlisted commands run unconditionally — no dry-run friction.
    if classification["verdict"] == "read-only" and not dry_run_param:
        blocked = _dcg_gate("deny-dcg-readonly")
        if blocked is not None:
            return blocked
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
        # NON-AUTHORITATIVE site (DCG_ADVISORY_SITES[0]): a preview executes
        # nothing, so an unavailable guard is annotated, not fatal. The real run
        # this preview authorizes still passes the authoritative gate below.
        payload["dcg"] = dcg_advisory(command_param, site="operator_box_exec:dry_run_preview")
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
        marker_note = _dryrun_marker_rejection_note(marker_status)
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
                "marker": _dryrun_marker_error_payload(marker_status),
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

    # Marker present and valid — but the marker only proves the operator
    # previewed this exact command. The guard still has to allow it.
    blocked = _dcg_gate("deny-dcg-marker")
    if blocked is not None:
        return blocked

    # Marker present and valid — authorize a single real run, then consume it.
    emit_box_exec_audit(
        box_id_param,
        command_param,
        verdict="allow-marker",
        reason=f"matching dry-run marker for command hash {cmd_hash}",
    )
    # CONSUME-ON-DISPATCH (same rule as `box.py exec`): the marker is spent the
    # moment the real run is issued, not after it succeeds. A command that
    # mutates the box and THEN fails must not leave a replayable marker — one
    # preview authorizes one ATTEMPT, and the retry needs a fresh preview.
    _clear_dryrun_marker("operator_box_exec", marker_key)
    ok, _code, data = run_ssh(validated_user, validated_host, command_param, timeout=timeout)
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

    # CONSUME-ON-DISPATCH: a `compose down` that fails partway has still stopped
    # containers, so the retry is authorized by a fresh preview of the new state.
    _clear_dryrun_marker("operator_compose_down", "local")
    ok, code, data = run_compose(["down"], timeout=120)
    emit_event("operator.compose_down", "local", {"ok": ok})
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

def _dryrun_marker_dir() -> Path:
    return REPO_ROOT / ".skillbox-state" / "dryrun-markers"


def _dryrun_marker_path(tool_name: str, box_id: str) -> Path:
    """Return the marker path after validating identifiers."""
    _validate_identifier(tool_name, "tool_name")
    _validate_identifier(box_id, "box_id")
    return _dryrun_marker_dir() / f".skillbox-dryrun-{tool_name}-{box_id}"


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
    return _opslib_box_exec_marker_key(box_id, command)


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


def _process_start_time(pid: int) -> str:
    proc_stat = Path("/proc") / str(pid) / "stat"
    try:
        text = proc_stat.read_text(encoding="utf-8")
        tail = text.rsplit(")", 1)[1].strip().split()
        if len(tail) > 19:
            return tail[19]
    except (OSError, IndexError):
        pass
    try:
        return str((Path("/proc") / str(pid)).stat().st_ctime_ns)
    except OSError:
        return "unknown"


def _dryrun_session_id() -> str:
    explicit = str(os.environ.get("CLAUDE_SESSION_ID") or "").strip()
    if explicit:
        return explicit
    parent_pid = os.getppid()
    return f"ppid:{parent_pid}:start:{_process_start_time(parent_pid)}"


def _dryrun_marker_cache_key(tool_name: str, box_id: str) -> tuple[str, str, str]:
    return (tool_name, box_id, _dryrun_session_id())


def _utc_timestamp(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _created_at_epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_dryrun_marker_payload(marker: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"unreadable marker: {exc.__class__.__name__}"
    if not raw:
        return None, "empty legacy marker"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "legacy mtime-only marker"
    if not isinstance(payload, dict):
        return None, "marker JSON payload is not an object"
    return payload, None


def _marker_stat_age(marker: Path, now: float) -> int | None:
    try:
        return max(0, int(now - marker.stat().st_mtime))
    except OSError:
        return None


def _dryrun_marker_status_from_path(
    marker: Path,
    *,
    tool_name: str,
    box_id: str,
    ttl_seconds: int,
    now: float,
    check_session: bool,
) -> dict[str, Any]:
    current_session = _dryrun_session_id()
    status: dict[str, Any] = {
        "path": str(marker),
        "exists": False,
        "valid": False,
        "expired": False,
        "session_mismatch": False,
        "reason": "absent",
        "age_seconds": None,
        "ttl_seconds": ttl_seconds,
        "format": "absent",
        "tool": tool_name,
        "key": box_id,
        "marker_session": None,
        "session_scope": None,
        "current_session": current_session,
        "created_at": None,
        "warning": None,
    }
    if not marker.is_file():
        return status

    status["exists"] = True
    payload, warning = _read_dryrun_marker_payload(marker)
    if payload is None:
        status["format"] = "legacy"
        status["warning"] = warning or "legacy mtime-only marker"
        age_seconds = _marker_stat_age(marker, now)
    else:
        status["format"] = "json"
        marker_tool = str(payload.get("tool") or "")
        marker_key = str(payload.get("key") or "")
        marker_session = str(payload.get("session") or "").strip()
        # The marker's OWN declaration of whether it is session-bound. Reading
        # the declared scope (instead of inferring one from the presence of a
        # `session` field) is what makes CLI markers session-agnostic BY
        # CONTRACT rather than by accident — see lib.opslib's marker contract.
        session_scope = marker_session_scope(payload)
        created_at = payload.get("created_at")
        status.update(
            {
                "marker_tool": marker_tool,
                "marker_key": marker_key,
                "marker_session": marker_session or None,
                "session_scope": session_scope,
                "created_at": created_at,
            }
        )
        created_epoch = _created_at_epoch(created_at)
        ages = [
            age
            for age in (
                max(0, int(now - created_epoch)) if created_epoch is not None else None,
                _marker_stat_age(marker, now),
            )
            if age is not None
        ]
        age_seconds = max(ages) if ages else None

    status["age_seconds"] = age_seconds
    if age_seconds is None:
        status["reason"] = "unreadable"
        return status
    if age_seconds > ttl_seconds:
        status["expired"] = True
        status["reason"] = "expired"
        return status
    if payload is not None and (
        (tool_name and marker_tool and marker_tool != tool_name)
        or (box_id and marker_key and marker_key != box_id)
    ):
        status["reason"] = "payload-mismatch"
        return status
    if (
        payload is not None
        and check_session
        and session_scope == MARKER_SESSION_SCOPE_SESSION
        and marker_session
        and current_session
        and marker_session != current_session
    ):
        status["session_mismatch"] = True
        status["reason"] = "session-mismatch"
        return status
    status["valid"] = True
    status["reason"] = "valid"
    return status


def _gc_expired_dryrun_markers(*, skip_path: Path | None = None) -> None:
    marker_dir = _dryrun_marker_dir()
    if not marker_dir.is_dir():
        return
    ttl_seconds = _dryrun_marker_ttl_seconds()
    now = time.time()
    for marker in marker_dir.glob(".skillbox-dryrun-*"):
        if skip_path is not None and marker == skip_path:
            continue
        status = _dryrun_marker_status_from_path(
            marker,
            tool_name="",
            box_id="",
            ttl_seconds=ttl_seconds,
            now=now,
            check_session=False,
        )
        if not status.get("expired"):
            continue
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


#: Marker tool name -> the manifest boundary that owns its local write. Closed
#: on purpose: an unmapped tool means a new authorizing write shipped without an
#: inventory row, and that must be a loud refusal rather than an ungated write.
_MARKER_BOUNDARIES = {
    "operator_provision": "operator_mcp.operator_provision",
    "operator_teardown": "operator_mcp.operator_teardown",
    "operator_box_exec": "operator_mcp.operator_box_exec",
    "operator_compose_down": "operator_mcp.operator_compose_down",
}


def _marker_boundary(tool_name: str) -> str:
    boundary = _MARKER_BOUNDARIES.get(str(tool_name))
    if boundary is None:
        raise StateLeaseUnavailable(
            f"no state-mutation boundary is declared for marker tool {tool_name!r}; "
            "refusing to write an authorizing marker ungated"
        )
    return boundary


@contextlib.contextmanager
def _marker_mutation_lease(boundary_id: str) -> Any:
    """Hold the state-root lease for one direct, local marker write.

    These are the only writes this server performs itself; everything else it
    does is delegated to `box.py`, which is its own final mutation owner. So the
    lease is taken HERE and held for exactly the length of the marker write --
    never across the child (see `run_script`).

    A marker is small, but it is the thing that authorizes a destructive run.
    Two servers minting or consuming one concurrently is precisely the race the
    single-writer contract exists to remove.
    """
    try:
        with state_root_lease(
            boundary_id, repo_root=REPO_ROOT, annotations={"surface": "operator_mcp"}
        ) as held:
            yield held
    except StateLeaseUnavailable:
        # Fail closed on the authorizing write: no marker is better than a
        # marker minted outside the contract that is supposed to serialize it.
        raise


def _stamp_dryrun_marker(tool_name: str, box_id: str) -> None:
    """Create a temp marker so the PreToolUse hook knows a dry-run was done.

    Session-SCOPED (``session_scope="session"``): this server has a stable
    session id, so its own previews stay bound to the session that took them.
    `box.py` mints session-agnostic markers on purpose — see the marker contract
    in ``lib.opslib``, which owns the payload shape for BOTH writers.
    """
    boundary = _marker_boundary(tool_name)
    _gc_expired_dryrun_markers()
    marker = _dryrun_marker_path(tool_name, box_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = dryrun_marker_payload(
        tool_name,
        box_id,
        source=MARKER_SOURCE_OPERATOR_MCP,
        created_at=_utc_timestamp(),
        session=_dryrun_session_id(),
    )
    with _marker_mutation_lease(boundary):
        marker.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _dryrun_marker_status(tool_name: str, box_id: str) -> dict[str, Any]:
    marker = _dryrun_marker_path(tool_name, box_id)
    ttl_seconds = _dryrun_marker_ttl_seconds()
    status = _dryrun_marker_status_from_path(
        marker,
        tool_name=tool_name,
        box_id=box_id,
        ttl_seconds=ttl_seconds,
        now=time.time(),
        check_session=True,
    )
    if status.get("expired"):
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
    _gc_expired_dryrun_markers(skip_path=marker)
    return status


def _has_dryrun_marker(tool_name: str, box_id: str) -> bool:
    """Check if a valid, non-expired dry-run marker exists."""
    status = _dryrun_marker_status(tool_name, box_id)
    cache_key = _dryrun_marker_cache_key(tool_name, box_id)
    if status["valid"]:
        _DRYRUN_MARKER_STATUS_CACHE.pop(cache_key, None)
    else:
        _DRYRUN_MARKER_STATUS_CACHE[cache_key] = status
    return bool(status["valid"])


def _dryrun_marker_rejection_status(tool_name: str, box_id: str) -> dict[str, Any]:
    return _DRYRUN_MARKER_STATUS_CACHE.pop(_dryrun_marker_cache_key(tool_name, box_id), None) or _dryrun_marker_status(tool_name, box_id)


def _clear_dryrun_marker(tool_name: str, box_id: str) -> None:
    """Remove the dry-run marker after a successful real operation.

    Consumption is gated for the same reason minting is: a marker consumed by
    one writer while another is deciding whether it is still valid is the race,
    not the write itself. A lease failure propagates -- it is never swallowed
    into the best-effort OSError branch below, because failing to serialize is
    not the same class of problem as a marker that was already gone.
    """
    boundary = _marker_boundary(tool_name)
    with _marker_mutation_lease(boundary):
        try:
            marker = _dryrun_marker_path(tool_name, box_id)
            marker.unlink(missing_ok=True)
            _DRYRUN_MARKER_STATUS_CACHE.pop(_dryrun_marker_cache_key(tool_name, box_id), None)
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


def _dryrun_marker_rejection_note(marker: dict[str, Any]) -> str:
    ttl_seconds = marker.get("ttl_seconds")
    age_seconds = marker.get("age_seconds")
    reason = str(marker.get("reason") or "absent")
    if reason == "expired":
        return f"marker expired; observed marker age is {age_seconds}s; configured marker ttl is {ttl_seconds}s"
    if reason == "session-mismatch":
        return (
            "marker was created by a different session "
            f"(marker session={marker.get('marker_session')!r}, current session={marker.get('current_session')!r})"
        )
    if reason == "absent":
        return f"no marker exists; configured marker ttl is {ttl_seconds}s"
    if reason == "payload-mismatch":
        return "marker payload does not match the requested tool/key"
    if age_seconds is None:
        return f"no marker age observed; configured marker ttl is {ttl_seconds}s"
    return f"marker is not valid ({reason}); observed marker age is {age_seconds}s; configured marker ttl is {ttl_seconds}s"


def _dryrun_marker_error_payload(marker: dict[str, Any]) -> dict[str, Any]:
    return {
        "exists": bool(marker.get("exists")),
        "expired": bool(marker.get("expired")),
        "session_mismatch": bool(marker.get("session_mismatch")),
        "reason": marker.get("reason") or "absent",
        "age_seconds": marker.get("age_seconds"),
        "ttl_seconds": marker.get("ttl_seconds"),
        "format": marker.get("format"),
        "marker_session": marker.get("marker_session"),
        "current_session": marker.get("current_session"),
        "created_at": marker.get("created_at"),
        "warning": marker.get("warning"),
    }


def _dry_run_required_error(
    tool_name: str,
    subject: str,
    safe_first_call: str,
    exact_cli: str,
    *,
    marker_status: dict[str, Any] | None = None,
) -> dict:
    marker = marker_status or {
        "ttl_seconds": _dryrun_marker_ttl_seconds(),
        "age_seconds": None,
        "reason": "absent",
        "current_session": _dryrun_session_id(),
    }
    marker_note = _dryrun_marker_rejection_note(marker)
    return _error_content({
        "error": {
            "type": "dry_run_required",
            "message": (
                f"{tool_name} requires a successful dry_run=true preview before the real operation "
                f"({marker_note})."
            ),
            "recoverable": True,
            "subject": subject,
            "marker": _dryrun_marker_error_payload(marker),
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
            f"{DEPRECATION_SUMMARY} Every tool below names its exact CLI replacement; "
            "run `python3 scripts/box.py capabilities --format json` (mcp_status) for the "
            "live map. Use the CLI for new work — this server remains only so existing "
            "registrations keep working. "
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
