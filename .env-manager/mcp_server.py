#!/usr/bin/env python3
"""
skillbox MCP server — the runtime graph as native agent tools.

Exposes manage.py commands as MCP tools over stdio (JSON-RPC 2.0, MCP 2024-11-05).
Claude Code loads this automatically via home/.claude/settings.json mcpServers config.

Discipline the server enforces: assess → scope → dry-run → act → verify.
  1. Always run skillbox_status before mutating.
  2. Always pass dry_run=true first for sync/up/down/restart/onboard.
  3. Scope with client= and service= to avoid unintended side effects.
  4. Run skillbox_doctor after every mutating operation to verify success.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
MANAGE_PY = SCRIPT_DIR / "manage.py"
SERVER_NAME = "skillbox"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

_CLIENT_PROP: dict = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Client overlay IDs to activate (e.g. ['personal'] or ['acme-studio']). "
        "Omit for core scope only. Discover IDs with skillbox_status."
    ),
}
_PROFILE_PROP: dict = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Profiles to activate (e.g. ['surfaces']). Omit for default profiles.",
}
_SERVICE_PROP: dict = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Limit to specific service IDs. Omit for all services in scope. "
        "Discover IDs with skillbox_status."
    ),
}
_TASK_PROP: dict = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Limit to specific task IDs. Omit for all tasks in scope. "
        "Discover IDs with skillbox_status."
    ),
}
_DRY_RUN_PROP: dict = {
    "type": "boolean",
    "description": "Preview changes without applying them. ALWAYS use first for mutating operations.",
    "default": False,
}
_WAIT_SECONDS_PROP: dict = {
    "type": "number",
    "description": "Seconds to wait for healthchecks (default varies by command).",
}

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    # --- Inspect cluster (read-only) ---
    {
        "name": "skillbox_status",
        "description": (
            "Report current runtime state for repos, services, tasks, skills, logs, and checks. "
            "Returns structured JSON: repos[].present, services[].state (running/stopped/dead), "
            "tasks[].state (ready/pending/blocked), checks[].ok. "
            "RUN THIS FIRST before any mutating operation. "
            "Do NOT mutate if services are in unexpected states — diagnose first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"client": _CLIENT_PROP, "profile": _PROFILE_PROP},
        },
    },
    {
        "name": "skillbox_doctor",
        "description": (
            "Validate runtime graph health: filesystem paths, installed skill integrity, "
            "service configs, declared checks. "
            "Returns checks[].status ('pass'/'warn'/'fail') with codes and messages. "
            "Fix all 'fail' items before proceeding. "
            "Run after every mutating operation to confirm success."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"client": _CLIENT_PROP, "profile": _PROFILE_PROP},
        },
    },
    {
        "name": "skillbox_render",
        "description": (
            "Print the fully-resolved runtime graph with all placeholders expanded: "
            "repos, services, tasks, skills, env_files, logs, checks. "
            "Use to understand what the active scope contains before making changes. "
            "Read-only, no side effects."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"client": _CLIENT_PROP, "profile": _PROFILE_PROP},
        },
    },
    {
        "name": "skillbox_logs",
        "description": (
            "Tail recent log output for declared services. "
            "ALWAYS read logs before restarting a failed service — the answer is usually there. "
            "Scope with service= to target a specific service."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "service": _SERVICE_PROP,
                "lines": {
                    "type": "integer",
                    "description": "Lines to return per service (default: 40).",
                    "default": 40,
                },
            },
        },
    },
    # --- Mutate cluster (use dry_run first) ---
    {
        "name": "skillbox_sync",
        "description": (
            "Create managed directories, clone declared repos, download artifacts, "
            "install declared skills. Idempotent — safe to re-run. "
            "Run after adding new repos or skills to runtime.yaml or a client overlay. "
            "Use dry_run=true first to preview."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "dry_run": _DRY_RUN_PROP,
            },
        },
    },
    {
        "name": "skillbox_up",
        "description": (
            "Sync runtime state, run required bootstrap tasks, then start declared services "
            "in dependency order. Waits for healthchecks before returning. "
            "Scope with service= to start one service and its prerequisites. "
            "Use dry_run=true to preview the service graph."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "service": _SERVICE_PROP,
                "dry_run": _DRY_RUN_PROP,
                "wait_seconds": _WAIT_SECONDS_PROP,
            },
        },
    },
    {
        "name": "skillbox_down",
        "description": (
            "Stop managed services in reverse dependency order. "
            "Scope with service= to stop one service and its dependents. "
            "CONFIRM WITH USER before running unscoped — stops all services. "
            "Use dry_run=true to preview which services would stop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "service": _SERVICE_PROP,
                "dry_run": _DRY_RUN_PROP,
                "wait_seconds": _WAIT_SECONDS_PROP,
            },
        },
    },
    {
        "name": "skillbox_restart",
        "description": (
            "Stop services, sync runtime state, run bootstrap tasks, then restart in dependency order. "
            "Use after code changes, config updates, or when a service is unhealthy. "
            "Scope with service= to restart one service. "
            "Use dry_run=true to preview."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "service": _SERVICE_PROP,
                "dry_run": _DRY_RUN_PROP,
                "wait_seconds": _WAIT_SECONDS_PROP,
            },
        },
    },
    # --- Bootstrap & context cluster ---
    {
        "name": "skillbox_bootstrap",
        "description": (
            "Run declared one-shot bootstrap tasks (e.g. npm install, db migration) in dependency order. "
            "Tasks are idempotent by success check — re-running a completed task is safe. "
            "Scope with task= to run a specific task. "
            "Use dry_run=true to preview."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "task": _TASK_PROP,
                "dry_run": _DRY_RUN_PROP,
            },
        },
    },
    {
        "name": "skillbox_context",
        "description": (
            "Regenerate home/.claude/CLAUDE.md and home/.codex/AGENTS.md from the resolved runtime graph. "
            "Run after adding repos, services, skills, or tasks to make them visible to the next agent session. "
            "Use dry_run=true to preview the generated content without writing files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "dry_run": _DRY_RUN_PROP,
            },
        },
    },
    # --- Scaffold & macro cluster ---
    {
        "name": "skillbox_onboard",
        "description": (
            "Full onboard macro: scaffold client overlay → sync → bootstrap → up → context → verify. "
            "Use to bring a new project online in one operation. "
            "Specify a blueprint to auto-wire repos, services, and checks. "
            "ALWAYS use dry_run=true first. "
            "Use skillbox_client_init with list_blueprints=true to discover available blueprints."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["client_id"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": (
                        "Lowercase client slug. Pattern: [a-z0-9]+(-[a-z0-9]+)*. "
                        "Examples: 'acme-studio', 'personal', 'my-api'."
                    ),
                },
                "blueprint": {
                    "type": "string",
                    "description": (
                        "Blueprint name from workspace/client-blueprints/. "
                        "Use skillbox_client_init with list_blueprints=true to see options."
                    ),
                },
                "label": {"type": "string", "description": "Human-friendly display name."},
                "root_path": {"type": "string", "description": "Runtime root path override."},
                "default_cwd": {"type": "string", "description": "Default working directory override."},
                "set_vars": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Blueprint variables as KEY=VALUE strings. "
                        "Example: ['PRIMARY_REPO_URL=https://github.com/acme/app.git', 'SERVICE_COMMAND=pnpm dev']."
                    ),
                },
                "force": {"type": "boolean", "description": "Overwrite existing scaffold files.", "default": False},
                "dry_run": _DRY_RUN_PROP,
                "wait_seconds": _WAIT_SECONDS_PROP,
            },
        },
    },
    {
        "name": "skillbox_client_init",
        "description": (
            "Scaffold a new client overlay or list available blueprints. "
            "Use list_blueprints=true to discover blueprints and their required variables — do this before skillbox_onboard. "
            "Use dry_run=true to preview the files that would be created. "
            "After scaffolding, run skillbox_onboard or the sync/bootstrap/up sequence manually."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Lowercase client slug. Required unless list_blueprints=true.",
                },
                "blueprint": {"type": "string", "description": "Blueprint to apply."},
                "label": {"type": "string", "description": "Human-friendly display name."},
                "root_path": {"type": "string", "description": "Runtime root path override."},
                "default_cwd": {"type": "string", "description": "Default working directory override."},
                "set_vars": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Blueprint variables as KEY=VALUE strings.",
                },
                "force": {"type": "boolean", "description": "Overwrite existing files.", "default": False},
                "list_blueprints": {
                    "type": "boolean",
                    "description": "List available blueprints and their required variables instead of scaffolding.",
                    "default": False,
                },
                "dry_run": _DRY_RUN_PROP,
            },
        },
    },
]

# ---------------------------------------------------------------------------
# manage.py invocation
# ---------------------------------------------------------------------------

def run_manage(args: list[str]) -> tuple[bool, int, Any]:
    """
    Invoke manage.py with given args. Returns (ok, exit_code, parsed_output).
    ok=True for exit 0 (success) or exit 2 (drift — still parseable and useful).
    ok=False for exit 1 (error) or failures.
    """
    if not MANAGE_PY.exists():
        return False, -1, {
            "error": {
                "type": "manage_not_found",
                "message": (
                    f"manage.py not found at {MANAGE_PY}. "
                    "The skillbox MCP server must run inside the workspace container "
                    "where /workspace/.env-manager/manage.py exists."
                ),
                "recoverable": False,
                "recovery_hint": "Run 'make up && make shell' to enter the workspace container.",
            }
        }

    cmd = [sys.executable, str(MANAGE_PY)] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, -1, {
            "error": {
                "type": "timeout",
                "message": "manage.py timed out after 120 seconds.",
                "recoverable": True,
                "recovery_hint": "Check service logs with skillbox_logs — a service may be hanging.",
                "next_actions": ["skillbox_logs"],
            }
        }

    if proc.stderr.strip():
        print(f"[skillbox-mcp] stderr: {proc.stderr.strip()}", file=sys.stderr, flush=True)

    stdout = proc.stdout.strip()
    if stdout:
        try:
            return proc.returncode in (0, 2), proc.returncode, json.loads(stdout)
        except json.JSONDecodeError:
            return proc.returncode == 0, proc.returncode, {"text": stdout}

    return proc.returncode == 0, proc.returncode, {"exit_code": proc.returncode}


def build_args(command: str, params: dict, positional: str | None = None) -> list[str]:
    """Translate tool params into a manage.py argv list."""
    args: list[str] = [command]
    if positional is not None:
        args.append(positional)
    args += ["--format", "json"]

    for c in (params.get("client") or []):
        args += ["--client", str(c)]
    for p in (params.get("profile") or []):
        args += ["--profile", str(p)]
    for s in (params.get("service") or []):
        args += ["--service", str(s)]
    for t in (params.get("task") or []):
        args += ["--task", str(t)]
    if params.get("dry_run"):
        args.append("--dry-run")
    if params.get("lines") is not None:
        args += ["--lines", str(int(params["lines"]))]
    if params.get("wait_seconds") is not None:
        args += ["--wait-seconds", str(float(params["wait_seconds"]))]
    if params.get("blueprint"):
        args += ["--blueprint", str(params["blueprint"])]
    if params.get("label"):
        args += ["--label", str(params["label"])]
    if params.get("root_path"):
        args += ["--root-path", str(params["root_path"])]
    if params.get("default_cwd"):
        args += ["--default-cwd", str(params["default_cwd"])]
    for sv in (params.get("set_vars") or []):
        args += ["--set", str(sv)]
    if params.get("force"):
        args.append("--force")
    if params.get("list_blueprints"):
        args.append("--list-blueprints")

    return args


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

# Maps tool name → (manage command, key in params for positional arg or None)
_DISPATCH: dict[str, tuple[str, str | None]] = {
    "skillbox_status":      ("status",      None),
    "skillbox_doctor":      ("doctor",      None),
    "skillbox_render":      ("render",      None),
    "skillbox_logs":        ("logs",        None),
    "skillbox_sync":        ("sync",        None),
    "skillbox_up":          ("up",          None),
    "skillbox_down":        ("down",        None),
    "skillbox_restart":     ("restart",     None),
    "skillbox_bootstrap":   ("bootstrap",   None),
    "skillbox_context":     ("context",     None),
    "skillbox_onboard":     ("onboard",     "client_id"),
    "skillbox_client_init": ("client-init", "client_id"),
}


def dispatch_tool(name: str, params: dict) -> dict:
    """Dispatch a tool call to manage.py and return a MCP content block."""
    if name not in _DISPATCH:
        return _error_content({
            "error": {
                "type": "unknown_tool",
                "message": f"Unknown tool: '{name}'.",
                "available_tools": sorted(_DISPATCH.keys()),
                "recoverable": False,
            }
        })

    command, positional_key = _DISPATCH[name]
    positional = str(params[positional_key]) if positional_key and positional_key in params else None

    if positional_key and positional is None and not params.get("list_blueprints"):
        return _error_content({
            "error": {
                "type": "missing_required_parameter",
                "message": f"'{positional_key}' is required for {name}.",
                "recoverable": True,
                "recovery_hint": f"Provide a {positional_key} value, e.g. 'acme-studio'.",
            }
        })

    args = build_args(command, params, positional)
    ok, exit_code, data = run_manage(args)

    # Annotate exit code so agents know what happened without parsing error fields.
    if isinstance(data, dict):
        data["_exit_code"] = exit_code

    return _ok_content(data) if ok else _error_content(data)


def _ok_content(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}


def _error_content(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}], "isError": True}


# ---------------------------------------------------------------------------
# MCP protocol handlers
# ---------------------------------------------------------------------------

def handle_initialize(_params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "skillbox runtime manager. "
            "Discipline: assess → scope → dry-run → act → verify. "
            "1. Run skillbox_status before any mutation. "
            "2. Pass dry_run=true first for sync/up/down/restart/bootstrap/onboard. "
            "3. Scope with client= and service= — avoid unintended side effects. "
            "4. Run skillbox_doctor after every mutation to confirm success. "
            "5. Run skillbox_logs before escalating a service error."
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
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def send_error(msg_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def main() -> None:
    print(f"[skillbox-mcp] starting — manage.py: {MANAGE_PY}", file=sys.stderr, flush=True)

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

        # Notifications (no id) require no response.
        if msg_id is None:
            continue

        handler = _HANDLERS.get(method)
        if handler is None:
            send_error(msg_id, -32601, f"Method not found: {method}")
            continue

        try:
            result = handler(params)
        except Exception as exc:  # noqa: BLE001
            print(f"[skillbox-mcp] unhandled error in {method}: {exc}", file=sys.stderr, flush=True)
            send_error(msg_id, -32603, f"Internal error: {exc}")
            continue

        send({"jsonrpc": "2.0", "id": msg_id, "result": result})


if __name__ == "__main__":
    main()
