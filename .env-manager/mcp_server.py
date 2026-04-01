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
import time
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
        "name": "skillbox_focus",
        "description": (
            "Activate an existing client workspace with live state collection and enriched context. "
            "Pipeline: sync → bootstrap → start services → collect live state → generate enriched CLAUDE.md. "
            "Returns live_state with repo branches, service health, recent log errors, and an Attention section. "
            "Use for existing clients — use skillbox_onboard for new ones. "
            "Use resume=true to re-activate the last focused client without re-running the full pipeline."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": (
                        "Existing client slug (e.g. 'personal'). "
                        "Required unless resume=true. Must already be onboarded."
                    ),
                },
                "profile": _PROFILE_PROP,
                "service": _SERVICE_PROP,
                "resume": {
                    "type": "boolean",
                    "description": "Re-activate last focus from .focus.json without re-running the full pipeline.",
                    "default": False,
                },
                "wait_seconds": _WAIT_SECONDS_PROP,
            },
        },
    },
    {
        "name": "skillbox_acceptance",
        "description": (
            "Run the first-box readiness gate for an onboarded client. "
            "Pipeline: doctor-pre → sync → focus → mcp-smoke → doctor-post. "
            "Fails when requested MCP surfaces from .mcp.json cannot initialize or complete tools/list."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["client_id"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Existing client slug (for example 'personal').",
                },
                "profile": _PROFILE_PROP,
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
    {
        "name": "skillbox_client_diff",
        "description": (
            "Compare a client projection bundle against the currently published payload in a git-backed control-plane repo. "
            "Use this before client-publish to review what would change. "
            "Returns file-level added/removed/changed paths, runtime-surface deltas "
            "(repos, services, tasks, skills, logs, checks), and publish metadata drift."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["client_id", "target_dir"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Existing client slug to diff, for example 'personal'.",
                },
                "target_dir": {
                    "type": "string",
                    "description": (
                        "Git-backed control-plane repo path visible from inside the box. "
                        "Skillbox compares against clients/<client>/current/ under this repo."
                    ),
                },
                "from_bundle": {
                    "type": "string",
                    "description": "Existing client-project bundle to diff instead of building a fresh one.",
                },
                "profile": _PROFILE_PROP,
            },
        },
    },
    # --- Pulse (reconciliation daemon) ---
    {
        "name": "skillbox_pulse",
        "description": (
            "Query the pulse reconciliation daemon status. "
            "Returns: running (bool), pid, interval, cycle count, heals, "
            "per-service states, per-check states, seconds since last tick. "
            "Use to verify the box is being continuously monitored and to see "
            "which services are supervised. "
            "If pulse is not running, start it with skillbox_up targeting service 'pulse'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    # --- Journal ---
    {
        "name": "skillbox_journal",
        "description": (
            "Query the event journal for recent system and agent activity. "
            "Returns structured events with timestamp, type, subject, and detail. "
            "Use to understand what happened recently: service starts/stops, syncs, "
            "focus activations, task runs, and agent notes from prior sessions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_minutes": {
                    "type": "number",
                    "description": "Only return events from the last N minutes (default: 60).",
                    "default": 60,
                },
                "event_type": {
                    "type": "string",
                    "description": (
                        "Filter to a specific event type. "
                        "System types: service.started, service.stopped, service.start_failed, "
                        "task.started, task.completed, task.failed, sync.completed, "
                        "context.generated, focus.activated, onboard.completed. "
                        "Agent types: agent.note, agent.decision, agent.error."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": "Filter to a specific subject (e.g. a service ID or client ID).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum events to return (default: 50, newest first).",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "skillbox_journal_write",
        "description": (
            "Write an agent event to the journal for cross-session continuity. "
            "Use to record intent, decisions, and outcomes so the next session knows what happened. "
            "Types are auto-prefixed with 'agent.' if not already."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["event_type", "subject"],
            "properties": {
                "event_type": {
                    "type": "string",
                    "description": "Event type (e.g. 'note', 'decision', 'error' — auto-prefixed with 'agent.').",
                },
                "subject": {
                    "type": "string",
                    "description": "What this event is about (e.g. 'auth-refactor', 'db-migration').",
                },
                "detail": {
                    "type": "object",
                    "description": "Optional structured detail.",
                    "additionalProperties": True,
                },
            },
        },
    },
    # --- Context curation ---
    {
        "name": "skillbox_ack",
        "description": (
            "Acknowledge journal events to curate active context. "
            "Acked events are hidden from the Recent Activity section in CLAUDE.md on next focus. "
            "Use after investigating or resolving an issue so the next session sees only unresolved items. "
            "Acks expire after 24h — if a problem recurs, new events surface normally. "
            "Use list=true to see current acks. Use skillbox_journal first to see events to ack."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "description": (
                        "Ack all events of this type. "
                        "Common types: pulse.service_restarted, service.start_failed, "
                        "focus.activated, onboard.completed, agent.note."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": "Ack events for this subject (e.g. a service ID or client ID).",
                },
                "ts": {
                    "type": "number",
                    "description": "Ack a specific event by its exact timestamp.",
                },
                "all": {
                    "type": "boolean",
                    "description": "Ack all unacked events. Use after reviewing the full journal.",
                    "default": False,
                },
                "reason": {
                    "type": "string",
                    "description": "Why this was acknowledged (e.g. 'fixed', 'investigating', 'expected').",
                },
                "list": {
                    "type": "boolean",
                    "description": "List current acks instead of creating new ones.",
                    "default": False,
                },
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
    if params.get("target_dir"):
        args += ["--target-dir", str(params["target_dir"])]
    if params.get("from_bundle"):
        args += ["--from-bundle", str(params["from_bundle"])]
    if params.get("root_path"):
        args += ["--root-path", str(params["root_path"])]
    if params.get("default_cwd"):
        args += ["--default-cwd", str(params["default_cwd"])]
    for sv in (params.get("set_vars") or []):
        args += ["--set", str(sv)]
    if params.get("resume"):
        args.append("--resume")
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
    "skillbox_focus":       ("focus",       "client_id"),
    "skillbox_acceptance":  ("acceptance",  "client_id"),
    "skillbox_client_init": ("client-init", "client_id"),
    "skillbox_client_diff": ("client-diff", "client_id"),
}


def _handle_pulse(_params: dict) -> dict:
    """Read pulse daemon state directly (no manage.py subprocess)."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from pulse import read_state

    state = read_state(SCRIPT_DIR.parent)
    return _ok_content(state)


def _handle_journal(params: dict) -> dict:
    """Query the event journal directly (no manage.py subprocess)."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from manage import query_journal, DEFAULT_ROOT_DIR

    since_minutes = float(params.get("since_minutes", 60))
    since = time.time() - (since_minutes * 60) if since_minutes > 0 else None
    events = query_journal(
        DEFAULT_ROOT_DIR,
        since=since,
        event_type=params.get("event_type"),
        subject=params.get("subject"),
        limit=int(params.get("limit", 50)),
    )
    return _ok_content({"events": events, "count": len(events)})


def _handle_journal_write(params: dict) -> dict:
    """Write an agent event to the journal directly."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from manage import emit_event, DEFAULT_ROOT_DIR

    event_type = str(params.get("event_type", "")).strip()
    subject = str(params.get("subject", "")).strip()
    if not event_type or not subject:
        return _error_content({
            "error": {
                "type": "missing_required_parameter",
                "message": "'event_type' and 'subject' are required.",
                "recoverable": True,
            }
        })
    if not event_type.startswith("agent."):
        event_type = f"agent.{event_type}"
    detail = params.get("detail") or {}
    emit_event(event_type, subject, detail, DEFAULT_ROOT_DIR)
    return _ok_content({"written": True, "event_type": event_type, "subject": subject})


def _handle_ack(params: dict) -> dict:
    """Acknowledge journal events to curate context."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from manage import ack_events, read_acks, DEFAULT_ROOT_DIR

    if params.get("list"):
        ack_data = read_acks(DEFAULT_ROOT_DIR)
        return _ok_content({"acks": ack_data, "count": len(ack_data)})

    event_type = params.get("event_type")
    subject = params.get("subject")
    ts = params.get("ts")
    ack_all = params.get("all", False)
    reason = params.get("reason", "")

    if not event_type and not subject and ts is None and not ack_all:
        return _error_content({
            "error": {
                "type": "missing_parameter",
                "message": "Provide event_type, subject, ts, or all=true to specify which events to ack.",
                "recoverable": True,
                "recovery_hint": "Use skillbox_journal first to see events, then ack by type or subject.",
            }
        })

    if ts is not None:
        ts = float(ts)

    acked_items = ack_events(
        DEFAULT_ROOT_DIR,
        event_type=event_type,
        subject=subject,
        ts=ts,
        ack_all=ack_all,
        reason=reason,
    )
    return _ok_content({
        "acked": acked_items,
        "count": len(acked_items),
        "next_actions": ["skillbox_status"],
    })


def dispatch_tool(name: str, params: dict) -> dict:
    """Dispatch a tool call to manage.py and return a MCP content block."""
    if name == "skillbox_pulse":
        return _handle_pulse(params)
    if name == "skillbox_journal":
        return _handle_journal(params)
    if name == "skillbox_journal_write":
        return _handle_journal_write(params)
    if name == "skillbox_ack":
        return _handle_ack(params)

    if name not in _DISPATCH:
        return _error_content({
            "error": {
                "type": "unknown_tool",
                "message": f"Unknown tool: '{name}'.",
                "available_tools": sorted(list(_DISPATCH.keys()) + ["skillbox_pulse", "skillbox_journal", "skillbox_journal_write", "skillbox_ack"]),
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
