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

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
MANAGE_PY = SCRIPT_DIR / "manage.py"
SERVER_NAME = "skillbox"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
DRYRUN_MARKER_TTL_SECONDS = 600
DRYRUN_MARKER_ROOT = SCRIPT_DIR.parent / ".skillbox-state" / "dryrun-markers"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

LOG_LEVELS = (
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
)
LOG_LEVEL_ORDER = {level: index for index, level in enumerate(LOG_LEVELS)}
CURRENT_LOG_LEVEL = "warning"
MCP_EVENT_CONTEXT_ENV = "SKILLBOX_MCP_EVENT_CONTEXT"
_RUNTIME_MANAGER: Any = None


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _runtime_manager_search_roots() -> list[Path]:
    roots = [SCRIPT_DIR]
    try:
        manage_text = MANAGE_PY.read_text(encoding="utf-8")
    except OSError:
        manage_text = ""
    match = re.search(r"REAL_MANAGE = ['\"](.+?)['\"]", manage_text)
    if match:
        roots.append(Path(match.group(1)).resolve().parent)
    return roots


def _runtime_manager_module() -> Any:
    global _RUNTIME_MANAGER
    if _RUNTIME_MANAGER is not None:
        return _RUNTIME_MANAGER

    last_error: Exception | None = None
    for root in _runtime_manager_search_roots():
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            import runtime_manager as runtime_manager_module
        except ModuleNotFoundError as exc:
            last_error = exc
            continue
        _RUNTIME_MANAGER = runtime_manager_module
        return _RUNTIME_MANAGER

    raise ModuleNotFoundError("No module named 'runtime_manager'") from last_error


def _normalize_log_level(level: Any) -> str:
    normalized = str(level or "").strip().lower()
    if normalized not in LOG_LEVEL_ORDER:
        supported = ", ".join(LOG_LEVELS)
        raise JsonRpcError(-32602, f"Invalid log level: {level!r}. Expected one of: {supported}.")
    return normalized


def _should_emit_log(level: str) -> bool:
    return LOG_LEVEL_ORDER[level] >= LOG_LEVEL_ORDER[CURRENT_LOG_LEVEL]


def emit_log_message(level: str, data: Any, *, logger: str = SERVER_NAME) -> None:
    normalized = _normalize_log_level(level)
    if not _should_emit_log(normalized):
        return
    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {
                "level": normalized,
                "logger": logger,
                "data": data,
            },
        }
    )

# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Identifier validation (path traversal / flag injection guard)
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


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
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "full": {
                    "type": "boolean",
                    "description": (
                        "Return the full raw runtime status payload. Defaults to false so agents get "
                        "the compact inspection summary first."
                    ),
                    "default": False,
                },
            },
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
        "name": "skillbox_skills",
        "description": (
            "Show effective skill availability and scope-policy issues for a cwd/client/profile. "
            "Use this before adding, moving, or globally installing skills. "
            "Returns compact structured JSON with effective skills, matched project categories, "
            "scope violations, missing cwd-scoped skills, and concrete recommendations. "
            "Source-root inventory is opt-in because it can be large."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "cwd": {
                    "type": "string",
                    "description": "Working directory used to match client overlays, project-local skills, and project categories.",
                },
                "include_global": {
                    "type": "boolean",
                    "description": "Inspect ~/.claude/skills and ~/.codex/skills. Defaults to true.",
                    "default": True,
                },
                "include_project": {
                    "type": "boolean",
                    "description": "Inspect repo-local .claude/.codex skill directories near cwd. Defaults to true.",
                    "default": True,
                },
                "show_sources": {
                    "type": "boolean",
                    "description": "Also scan configured skill source roots for unsynced skills. Defaults to false.",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows for text-oriented manage output; JSON still includes compact policy issues.",
                    "default": 80,
                },
            },
        },
    },
    {
        "name": "skillbox_skill_audit",
        "description": (
            "Audit skill-scope policy across configured downstream repos. "
            "Use this when skill links feel messy across many repos: it scans repo roots, "
            "reports missing cwd-scoped skills and project scope violations per repo, and "
            "summarizes global drift once. Read-only, no side effects."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "cwd": {
                    "type": "string",
                    "description": "Working directory used for cwd-aware client matching and the global drift summary.",
                },
                "scan_root": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional roots to scan for git repos. Defaults to skill_install_scan_roots from skill-scope.yaml.",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum directory depth under each scan root when finding git repos.",
                    "default": 3,
                },
                "include_global": {
                    "type": "boolean",
                    "description": "Include the one-time global skill drift summary. Defaults to true.",
                    "default": True,
                },
                "include_clean": {
                    "type": "boolean",
                    "description": "Include clean repo rows, not only repos with issues. Defaults to false.",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum repo rows and names for text-oriented output.",
                    "default": 40,
                },
            },
        },
    },
    {
        "name": "skillbox_mcp_audit",
        "description": (
            "Audit MCP server visibility for both Claude Code and Codex config formats. "
            "Reads Claude project JSON (.mcp.json) and Codex TOML (.codex/config.toml), "
            "compares them against the active Skillbox runtime MCP expectations, and reports "
            "missing, extra, disabled, invalid, and cross-surface parity mismatches. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "cwd": {
                    "type": "string",
                    "description": "Working directory used to find the target repo root.",
                },
                "config_root": {
                    "type": "string",
                    "description": "Explicit repo/config root containing .mcp.json and .codex/config.toml.",
                },
            },
        },
    },
    {
        "name": "skillbox_parity_report",
        "description": (
            "Read-only dev/prod parity report for one client. Compares runtime routes, env files, "
            "healthchecks, deploy modes, and network assumptions against the client's production_stack "
            "contract and returns ready/missing/drift/deferred/not_assessed rows."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["client_id"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Existing client slug to report on, for example 'personal'.",
                },
                "profile": _PROFILE_PROP,
            },
        },
    },
    {
        "name": "skillbox_skill",
        "description": (
            "Plan or apply one skill lifecycle action: add/link, activate, move, remove, prune, or sync "
            "cwd-missing skills from skill-scope policy. Uses the same global/project/category "
            "placement rules as skillbox_skills. activate also returns the SKILL.md activation "
            "packet for the current agent session. Always call with dry_run=true first before "
            "mutating, and pass yes=true for remove/move/prune actions after reviewing the plan."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["plan", "add", "activate", "move", "remove", "prune", "sync"],
                    "description": "Lifecycle action to run.",
                },
                "skill": {
                    "type": "string",
                    "description": "Skill name. Required for plan/add/move/remove; optional for sync/prune.",
                },
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "cwd": {
                    "type": "string",
                    "description": "Working directory used to infer client overlays, repo roots, and categories.",
                },
                "to": {
                    "type": "string",
                    "enum": ["auto", "global", "project", "category"],
                    "description": "Destination scope. auto follows skill-scope policy.",
                    "default": "auto",
                },
                "category": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project category ids to target, such as cli, mcp, frontend, or ios.",
                },
                "source": {
                    "type": "string",
                    "description": "Explicit skill directory or parent source directory.",
                },
                "from_scope": {
                    "type": "string",
                    "enum": ["global", "project", "all"],
                    "description": "Scope to remove from for remove/move/prune.",
                    "default": "all",
                },
                "dry_run": _DRY_RUN_PROP,
                "prune": {
                    "type": "boolean",
                    "description": "For sync, also unlink policy violations.",
                    "default": False,
                },
                "yes": {
                    "type": "boolean",
                    "description": "Confirm remove/move/prune unlink actions after a dry-run review.",
                    "default": False,
                },
                "force": {
                    "type": "boolean",
                    "description": "Replace existing non-symlink files and override global policy blocks.",
                    "default": False,
                },
                "allow_directories": {
                    "type": "boolean",
                    "description": "Allow unlink/prune to remove real skill directories, not just symlinks/files.",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "skillbox_overlay",
        "description": (
            "List, enable, disable, toggle, or activate a skill-scope overlay. "
            "activate is cwd-scoped and does not persist overlay state by default: it links "
            "literal scoped skills into both Claude and Codex project skill roots for cwd, "
            "then returns SKILL.md activation packets for immediate use in the current session. "
            "Use to=global only when the overlay should be installed into operator homes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "on", "off", "toggle", "activate"],
                    "description": "Overlay action. Defaults to list.",
                    "default": "list",
                },
                "name": {
                    "type": "string",
                    "description": "Overlay name, such as marketing. Required except for list.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory used to match overlay-scoped skills and repo roots.",
                },
                "to": {
                    "type": "string",
                    "enum": ["project", "global", "category", "auto"],
                    "description": "Activation destination. Defaults to project so hot overlays stay scoped to cwd.",
                    "default": "project",
                },
                "scope": {
                    "type": "string",
                    "enum": ["project", "global", "all"],
                    "description": "Symlink removal scope for off/toggle. Defaults to project.",
                    "default": "project",
                },
                "category": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project category ids to target when to=category.",
                },
                "source": {
                    "type": "string",
                    "description": "Explicit skill directory or parent source directory for activation.",
                },
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "keep": {
                    "type": "boolean",
                    "description": "For off, keep existing overlay-scoped symlinks instead of unlinking them.",
                    "default": False,
                },
                "dry_run": _DRY_RUN_PROP,
                "force": {
                    "type": "boolean",
                    "description": "Replace existing non-symlink files and override global policy blocks.",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "skillbox_mmdx_open",
        "description": (
            "Fuzzy-find and open a local .mmdx or .mmd diagram through the Buildooor diagrams viewer. "
            "Use this instead of spelling out the mmdx skill script path. "
            "Discovery: pass cwd as the downstream repo/current working directory, then pass query as a "
            "file path, stem, or fuzzy path fragment such as 'skill review realms'. "
            "Omit query or set open=false to list candidates without launching the browser. "
            "Returns selected path, alternatives, viewer URL, and next_actions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "File path, basename, stem, or fuzzy path fragment. Omit to list recent diagrams.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Current downstream repo/workspace directory. Defaults to the MCP server process cwd.",
                },
                "search_root": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit directories to scan. Omit to scan the cwd git root, then cwd.",
                },
                "open": {
                    "type": "boolean",
                    "description": "Open the selected match in the browser. Defaults to true when query resolves.",
                    "default": True,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum candidate rows to return.",
                    "default": 8,
                },
                "tmux": {
                    "type": "boolean",
                    "description": "Open with the MMDX skill's local tmux handoff bridge.",
                    "default": False,
                },
                "tmux_submit": {
                    "type": "boolean",
                    "description": "With tmux=true, press Enter after the viewer sends a handoff packet.",
                    "default": False,
                },
                "allow_parser_install": {
                    "type": "boolean",
                    "description": "Allow the MMDX script to install its Mermaid parser dependency if missing.",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "skillbox_operator_booking",
        "description": (
            "Fetch configured human-operator availability or create an x402 dayrate booking hold through SPAPS. "
            "Uses the active client overlay's human_operator/operator_booking config, the configured publishable key, "
            "and the production rate-limited SPAPS endpoints. For book, pass date, slot, email, and name; "
            "set send_magic_link=true to request the passwordless account email before creating the hold."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["availability", "times", "config", "book"],
                    "description": "availability/times lists bookable slots, config returns sanitized endpoint config, book creates an x402 hold.",
                    "default": "availability",
                },
                "client": _CLIENT_PROP,
                "profile": _PROFILE_PROP,
                "date": {
                    "type": "string",
                    "description": "Booking date in YYYY-MM-DD format. Required for action=book.",
                },
                "slot": {
                    "type": "string",
                    "description": "Slot type to book, such as AM or PM. Required for action=book.",
                },
                "email": {
                    "type": "string",
                    "description": "Client email for the booking and optional magic-link account email.",
                },
                "name": {
                    "type": "string",
                    "description": "Client display name for the booking.",
                },
                "redirect_url": {
                    "type": "string",
                    "description": "Optional magic-link redirect URL for account sign-in.",
                },
                "origin": {
                    "type": "string",
                    "description": "Optional Origin header override for browser-style publishable-key requests.",
                },
                "access_token_env": {
                    "type": "string",
                    "description": "Optional env var containing a verified user JWT; when set, bookings bind to that account.",
                },
                "send_magic_link": {
                    "type": "boolean",
                    "description": "When action=book, request the passwordless account email before creating the x402 hold.",
                    "default": False,
                },
                "dry_run": _DRY_RUN_PROP,
                "limit": {
                    "type": "integer",
                    "description": "Maximum availability slots to return.",
                    "default": 8,
                },
            },
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
                    "minimum": 1,
                    "description": "Lines to return per service (default: 40).",
                    "default": 40,
                },
            },
        },
    },
    {
        "name": "skillbox_events",
        "description": (
            "Replay runtime.log lines and durable session events through one cursor-based feed. "
            "Use this as the orchestrator fallback when you need recent failures, checkpoints, "
            "or long-poll style watching without relying on live MCP notifications."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Optional client slug to scope the event feed.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional durable session id to scope the event feed.",
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque cursor returned by an earlier skillbox_events call.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum events to return (default: 50).",
                    "default": 50,
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Long-poll duration while waiting for new events (default: 0).",
                    "default": 0,
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
        "name": "skillbox_session_start",
        "description": (
            "Create a durable client-scoped session with metadata, handoff file, and append-only events.jsonl. "
            "Use this at the start of tutoring or vibe-coding work so the box can recover the session after a crash."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["client_id"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Existing client slug that owns the session.",
                },
                "label": {"type": "string", "description": "Human-friendly session label."},
                "cwd": {"type": "string", "description": "Working directory for the session."},
                "goal": {"type": "string", "description": "Short statement of intent."},
                "actor": {"type": "string", "description": "Optional operator or agent name."},
            },
        },
    },
    {
        "name": "skillbox_session_event",
        "description": (
            "Append a structured event to an active durable session and mirror a summary into the global runtime log. "
            "Use this to record notes, checkpoints, decisions, or errors during work."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["client_id", "session_id", "event_type"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Existing client slug that owns the session.",
                },
                "session_id": {"type": "string", "description": "Durable session id."},
                "event_type": {
                    "type": "string",
                    "description": "Event type, with or without the session. prefix.",
                },
                "message": {"type": "string", "description": "Optional event message."},
                "actor": {"type": "string", "description": "Optional operator or agent name."},
            },
        },
    },
    {
        "name": "skillbox_session_end",
        "description": (
            "Close an active durable session, persist a summary, and append a terminal session.ended event."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["client_id", "session_id"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Existing client slug that owns the session.",
                },
                "session_id": {"type": "string", "description": "Durable session id."},
                "status": {
                    "type": "string",
                    "description": "Terminal state: completed, failed, or abandoned.",
                    "enum": ["abandoned", "completed", "failed"],
                    "default": "completed",
                },
                "summary": {"type": "string", "description": "Optional closeout summary."},
            },
        },
    },
    {
        "name": "skillbox_session_resume",
        "description": (
            "Resume a previously ended durable session and append a session.resumed event."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["client_id", "session_id"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Existing client slug that owns the session.",
                },
                "session_id": {"type": "string", "description": "Durable session id."},
                "actor": {"type": "string", "description": "Optional operator or agent name."},
                "message": {"type": "string", "description": "Optional resume note."},
            },
        },
    },
    {
        "name": "skillbox_session_status",
        "description": (
            "Read one durable session with recent events or list recent sessions for a client. "
            "Use this after a crash to recover the latest active or recently-ended work."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["client_id"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Existing client slug that owns the session.",
                },
                "session_id": {"type": "string", "description": "Specific durable session id to inspect."},
                "limit": {
                    "type": "integer",
                    "description": "Maximum sessions to return when listing.",
                    "default": 10,
                },
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
    {
        "name": "skillbox_worker_submit",
        "description": (
            "Submit an open-ended task to the skillbox worker broker. Returns a broker-managed "
            "run id, selected runtime, and initial state without coupling the caller to a harness."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task_class", "instruction"],
            "properties": {
                "task_class": {
                    "type": "string",
                    "enum": [
                        "analysis",
                        "interpretation",
                        "recommendation",
                        "drafting",
                        "research",
                        "ops_execution",
                    ],
                },
                "instruction": {"type": "string"},
                "client": {"type": "string", "description": "Overlay id for context resolution."},
                "cwd": {"type": "string", "description": "Working directory hint for context resolution."},
                "repo_hint": {"type": "string", "description": "Optional repo id or path hint."},
                "runtime": {"type": "string", "default": "hermes"},
                "write_scope": {
                    "type": "string",
                    "enum": ["read_only", "propose_only", "repo_patch"],
                    "default": "propose_only",
                },
                "memory_scope": {
                    "type": "string",
                    "enum": ["none", "repo", "client"],
                    "default": "repo",
                },
                "artifact_policy": {"type": "string", "default": "summary_and_files"},
                "harness_session_ref": {"type": "string"},
            },
        },
    },
    {
        "name": "skillbox_worker_status",
        "description": "Read broker-level state for a worker run without exposing runtime-internal payloads.",
        "inputSchema": {
            "type": "object",
            "required": ["run_id"],
            "properties": {"run_id": {"type": "string"}},
        },
    },
    {
        "name": "skillbox_worker_artifacts",
        "description": (
            "Return artifacts and learning proposals for a terminal worker run. "
            "Non-terminal runs return WORKER_RESULT_NOT_READY."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["run_id"],
            "properties": {"run_id": {"type": "string"}},
        },
    },
    {
        "name": "skillbox_worker_promote_learning",
        "description": (
            "Promote a reviewed learning proposal. Pending proposals are rejected with "
            "WORKER_LEARNING_REVIEW_REQUIRED; no skill or memory writeback happens without this explicit call."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["proposal_id", "target_kind", "target_location"],
            "properties": {
                "proposal_id": {"type": "string"},
                "approved_by": {"type": "string"},
                "target_kind": {"type": "string"},
                "target_location": {"type": "string"},
                "promotion_mode": {"type": "string", "default": "promote"},
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
]

# ---------------------------------------------------------------------------
# manage.py invocation
# ---------------------------------------------------------------------------

def _compact_log_context(base: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for source in (base or {}, extra):
        for key, value in source.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, (list, dict)) and not value:
                continue
            payload[str(key)] = value
    return payload


def _clean_param_scalar(params: dict, key: str) -> str:
    return str(params.get(key) or "").strip()


def _clean_param_list(params: dict, key: str) -> list[str]:
    raw = params.get(key)
    if raw is None:
        return []
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    return [str(item).strip() for item in values if str(item).strip()]


def _add_event_identity(context: dict[str, Any], params: dict) -> None:
    for key in ("client_id", "session_id", "actor"):
        value = _clean_param_scalar(params, key)
        if value:
            context[key] = value


def _add_client_scope(context: dict[str, Any], params: dict) -> None:
    client_scope = _clean_param_list(params, "client")
    if not client_scope:
        return
    if len(client_scope) == 1 and "client_id" not in context:
        context["client_id"] = client_scope[0]
        return
    context["client_scope"] = client_scope


def _add_list_scopes(context: dict[str, Any], params: dict) -> None:
    for key in ("profile", "service", "task"):
        values = _clean_param_list(params, key)
        if values:
            context[key] = values


def build_tool_event_context(name: str, command: str, params: dict, request_id: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "mcp_tool_name": name,
        "manage_command": command,
    }
    if request_id is not None:
        context["mcp_request_id"] = str(request_id)

    _add_event_identity(context, params)
    _add_client_scope(context, params)
    _add_list_scopes(context, params)
    return context


def run_manage(args: list[str], *, event_context: dict[str, Any] | None = None) -> tuple[bool, int, Any]:
    """
    Invoke manage.py with given args. Returns (ok, exit_code, parsed_output).
    ok=True for exit 0 (success) or exit 2 (drift — still parseable and useful).
    ok=False for exit 1 (error) or failures.
    """
    log_context = _compact_log_context(event_context, manage_args=args)
    if not MANAGE_PY.exists():
        payload = {
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
        emit_log_message("error", _compact_log_context(log_context, error=payload["error"]), logger="skillbox.manage")
        return False, -1, payload

    # Scale timeout with --wait-seconds when present.  Services are started
    # sequentially so total time can be wait_seconds * service_count.  Use a
    # generous base (120s) plus the declared per-service wait to avoid killing
    # manage.py while it is still polling health checks.
    subprocess_timeout = 120.0
    for i, arg in enumerate(args):
        if arg == "--wait-seconds" and i + 1 < len(args):
            try:
                subprocess_timeout = max(subprocess_timeout, float(args[i + 1]) * 2 + 60)
            except ValueError:
                pass
            break

    cmd = [sys.executable, str(MANAGE_PY)] + args
    proc_env = None
    if event_context:
        proc_env = os.environ.copy()
        proc_env[MCP_EVENT_CONTEXT_ENV] = json.dumps(
            event_context,
            separators=(",", ":"),
            default=str,
        )
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=subprocess_timeout,
            env=proc_env,
        )
    except subprocess.TimeoutExpired:
        payload = {
            "error": {
                "type": "timeout",
                "message": f"manage.py timed out after {subprocess_timeout:.0f} seconds.",
                "recoverable": True,
                "recovery_hint": "Check service logs with skillbox_logs — a service may be hanging.",
                "next_actions": ["skillbox_logs"],
            }
        }
        emit_log_message(
            "error",
            _compact_log_context(log_context, error=payload["error"]),
            logger="skillbox.manage",
        )
        return False, -1, payload

    if proc.stderr.strip():
        stderr_text = proc.stderr.strip()
        emit_log_message(
            "warning" if proc.returncode in (0, 2) else "error",
            _compact_log_context(log_context, exit_code=proc.returncode, stderr=stderr_text),
            logger="skillbox.manage.stderr",
        )
        print(f"[skillbox-mcp] stderr: {proc.stderr.strip()}", file=sys.stderr, flush=True)

    stdout = proc.stdout.strip()
    if stdout:
        try:
            data = json.loads(stdout)
            ok = proc.returncode in (0, 2)
            if not ok:
                emit_log_message(
                    "error",
                    _compact_log_context(log_context, exit_code=proc.returncode, result=data),
                    logger="skillbox.manage",
                )
            return ok, proc.returncode, data
        except json.JSONDecodeError:
            data = {"text": stdout}
            ok = proc.returncode == 0
            if not ok:
                emit_log_message(
                    "error",
                    _compact_log_context(log_context, exit_code=proc.returncode, result=data),
                    logger="skillbox.manage",
                )
            return ok, proc.returncode, data

    ok = proc.returncode == 0
    data = {"exit_code": proc.returncode}
    if not ok:
        emit_log_message(
            "error",
            _compact_log_context(log_context, exit_code=proc.returncode),
            logger="skillbox.manage",
        )
    return ok, proc.returncode, data


_REPEAT_ARG_SPECS: tuple[tuple[str, str], ...] = (
    ("client", "--client"),
    ("profile", "--profile"),
    ("service", "--service"),
    ("task", "--task"),
    ("category", "--category"),
    ("search_root", "--search-root"),
    ("scan_root", "--scan-root"),
    ("set_vars", "--set"),
)
_STRING_ARG_SPECS: tuple[tuple[str, str], ...] = (
    ("blueprint", "--blueprint"),
    ("label", "--label"),
    ("target_dir", "--target-dir"),
    ("from_bundle", "--from-bundle"),
    ("root_path", "--root-path"),
    ("default_cwd", "--default-cwd"),
    ("session_id", "--session-id"),
    ("event_type", "--event-type"),
    ("message", "--message"),
    ("goal", "--goal"),
    ("actor", "--actor"),
    ("summary", "--summary"),
    ("status", "--status"),
    ("cwd", "--cwd"),
    ("config_root", "--config-root"),
    ("to", "--to"),
    ("scope", "--scope"),
    ("source", "--source"),
    ("from_scope", "--from"),
    ("date", "--date"),
    ("slot", "--slot"),
    ("email", "--email"),
    ("name", "--name"),
    ("redirect_url", "--redirect-url"),
    ("origin", "--origin"),
    ("access_token_env", "--access-token-env"),
    ("repo_hint", "--repo-hint"),
    ("runtime", "--runtime"),
    ("write_scope", "--write-scope"),
    ("memory_scope", "--memory-scope"),
    ("artifact_policy", "--artifact-policy"),
    ("harness_session_ref", "--harness-session-ref"),
    ("approved_by", "--approved-by"),
    ("target_kind", "--target-kind"),
    ("target_location", "--target-location"),
    ("promotion_mode", "--promotion-mode"),
)
_BOOL_ARG_SPECS: tuple[tuple[str, str], ...] = (
    ("dry_run", "--dry-run"),
    ("show_sources", "--show-sources"),
    ("show_shadowed", "--show-shadowed"),
    ("issues_only", "--issues-only"),
    ("allow_directories", "--allow-directories"),
    ("yes", "--yes"),
    ("prune", "--prune"),
    ("resume", "--resume"),
    ("force", "--force"),
    ("list_blueprints", "--list-blueprints"),
    ("keep", "--keep"),
    ("tmux", "--tmux"),
    ("tmux_submit", "--tmux-submit"),
    ("allow_parser_install", "--allow-parser-install"),
    ("send_magic_link", "--send-magic-link"),
)


def _append_repeat_args(args: list[str], params: dict) -> None:
    for key, flag in _REPEAT_ARG_SPECS:
        raw_values = params.get(key) or []
        values = [raw_values] if isinstance(raw_values, str) else raw_values
        for value in values:
            args += [flag, str(value)]


def _int_param(params: dict, key: str, *, minimum: int | None = None) -> int:
    try:
        value = int(params[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def _float_param(params: dict, key: str, *, minimum: float | None = None) -> float:
    try:
        value = float(params[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} must be >= {minimum:g}")
    return value


def _append_scalar_args(args: list[str], params: dict) -> None:
    for key, flag in _STRING_ARG_SPECS:
        if params.get(key):
            args += [flag, str(params[key])]
    if params.get("lines") is not None:
        args += ["--lines", str(_int_param(params, "lines", minimum=1))]
    if params.get("wait_seconds") is not None:
        args += ["--wait-seconds", str(_float_param(params, "wait_seconds", minimum=0.0))]
    if params.get("limit") is not None:
        args += ["--limit", str(_int_param(params, "limit", minimum=1))]
    if params.get("max_depth") is not None:
        args += ["--max-depth", str(_int_param(params, "max_depth", minimum=0))]


def _append_bool_args(args: list[str], command: str, params: dict) -> None:
    for key, flag in _BOOL_ARG_SPECS:
        if params.get(key):
            args.append(flag)
    if params.get("include_global") is False or params.get("no_global"):
        args.append("--no-global")
    if params.get("include_project") is False or params.get("no_project"):
        args.append("--no-project")
    if command == "skill-audit" and params.get("include_clean"):
        args.append("--all")
    if params.get("full") and command == "skills":
        args.append("--full")
    if params.get("compact") and command == "status":
        args.append("--compact")
    if command == "mmdx" and params.get("open") is False:
        args.append("--no-open")


def _append_command_positionals(args: list[str], command: str, params: dict) -> None:
    if command == "worker-submit":
        for key in ("task_class", "instruction"):
            if params.get(key):
                args.append(str(params[key]))
        return
    if command in {"worker-status", "worker-artifacts"} and params.get("run_id"):
        args.append(str(params["run_id"]))
        return
    if command == "worker-promote-learning" and params.get("proposal_id"):
        args.append(str(params["proposal_id"]))
        return
    if command == "mmdx" and params.get("query"):
        args.append(str(params["query"]))
    if params.get("skill"):
        args.append(str(params["skill"]))
    if command == "overlay" and params.get("name"):
        args.append(str(params["name"]))


def build_args(command: str, params: dict, positional: str | None = None) -> list[str]:
    """Translate tool params into a manage.py argv list."""
    args: list[str] = [command]
    if positional is not None:
        args.append(positional)
    args += ["--format", "json"]

    _append_repeat_args(args, params)
    _append_command_positionals(args, command, params)
    _append_scalar_args(args, params)
    _append_bool_args(args, command, params)

    return args


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

# Maps tool name → (manage command, key in params for positional arg or None)
_DISPATCH: dict[str, tuple[str, str | None]] = {
    "skillbox_status":      ("status",      None),
    "skillbox_doctor":      ("doctor",      None),
    "skillbox_render":      ("render",      None),
    "skillbox_skills":      ("skills",      None),
    "skillbox_skill_audit": ("skill-audit", None),
    "skillbox_mcp_audit":   ("mcp-audit",   None),
    "skillbox_parity_report": ("parity-report", "client_id"),
    "skillbox_skill":       ("skill",       "action"),
    "skillbox_overlay":     ("overlay",     "action"),
    "skillbox_mmdx_open":   ("mmdx",        None),
    "skillbox_operator_booking": ("operator-booking", "action"),
    "skillbox_logs":        ("logs",        None),
    "skillbox_sync":        ("sync",        None),
    "skillbox_up":          ("up",          None),
    "skillbox_down":        ("down",        None),
    "skillbox_restart":     ("restart",     None),
    "skillbox_bootstrap":   ("bootstrap",   None),
    "skillbox_context":     ("context",     None),
    "skillbox_onboard":     ("onboard",     "client_id"),
    "skillbox_focus":       ("focus",       "client_id"),
    "skillbox_session_start": ("session-start", "client_id"),
    "skillbox_session_event": ("session-event", "client_id"),
    "skillbox_session_end":   ("session-end", "client_id"),
    "skillbox_session_resume": ("session-resume", "client_id"),
    "skillbox_session_status": ("session-status", "client_id"),
    "skillbox_worker_submit": ("worker-submit", None),
    "skillbox_worker_status": ("worker-status", None),
    "skillbox_worker_artifacts": ("worker-artifacts", None),
    "skillbox_worker_promote_learning": ("worker-promote-learning", None),
    "skillbox_acceptance":  ("acceptance",  "client_id"),
    "skillbox_client_init": ("client-init", "client_id"),
    "skillbox_client_diff": ("client-diff", "client_id"),
}

_DRY_RUN_REQUIRED_TOOLS = frozenset(
    {
        "skillbox_sync",
        "skillbox_up",
        "skillbox_down",
        "skillbox_restart",
        "skillbox_bootstrap",
        "skillbox_context",
        "skillbox_onboard",
    }
)


def _handle_pulse(_params: dict) -> dict:
    """Read pulse daemon state directly (no manage.py subprocess)."""
    from pulse import read_state

    state = read_state(SCRIPT_DIR.parent)
    return _ok_content(state)


def _handle_events(params: dict) -> dict:
    # Validate identifier params before use
    for key in ("client_id", "session_id"):
        value = str(params.get(key) or "").strip()
        if value:
            try:
                _validate_identifier(value, key)
            except ValueError as exc:
                return _error_content(
                    {"error": {"type": "invalid_parameter", "message": str(exc), "recoverable": True}}
                )

    runtime_manager = _runtime_manager_module()
    try:
        limit = int(params.get("limit") or runtime_manager.DEFAULT_EVENT_FEED_LIMIT)
        wait_seconds = float(params.get("wait_seconds") or 0.0)
    except (TypeError, ValueError) as exc:
        return _error_content(
            {
                "error": {
                    "type": "invalid_parameter",
                    "message": f"Invalid skillbox_events parameter: {exc}",
                    "recoverable": True,
                }
            }
        )

    payload = runtime_manager.event_feed_payload(
        runtime_manager.DEFAULT_ROOT_DIR,
        client_id=str(params.get("client_id") or "").strip() or None,
        session_id=str(params.get("session_id") or "").strip() or None,
        cursor=str(params.get("cursor") or "").strip() or None,
        limit=limit,
        wait_seconds=wait_seconds,
    )
    return _ok_content(payload)


def _args_without_dry_run(args: list[str]) -> list[str]:
    return [arg for arg in args if arg != "--dry-run"]


def _dryrun_marker_subject(tool_name: str, args: list[str]) -> str:
    material = json.dumps(
        {"tool": tool_name, "args": _args_without_dry_run(args)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _dryrun_marker_path(tool_name: str, subject_hash: str) -> Path:
    safe_tool = re.sub(r"[^a-zA-Z0-9_.-]", "_", tool_name)
    safe_subject = re.sub(r"[^a-zA-Z0-9_.-]", "_", subject_hash)
    return DRYRUN_MARKER_ROOT / f".skillbox-dryrun-{safe_tool}-{safe_subject}"


def _dryrun_marker_info(tool_name: str, args: list[str]) -> dict[str, Any]:
    subject_hash = _dryrun_marker_subject(tool_name, args)
    return {
        "tool": tool_name,
        "subject_hash": subject_hash,
        "ttl_seconds": DRYRUN_MARKER_TTL_SECONDS,
        "next_action": (
            f"Review this preview, then repeat {tool_name} for the same scope without "
            "dry_run before the marker expires."
        ),
    }


def _stamp_dryrun_marker(tool_name: str, args: list[str]) -> dict[str, Any]:
    info = _dryrun_marker_info(tool_name, args)
    marker = _dryrun_marker_path(tool_name, str(info["subject_hash"]))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(info, sort_keys=True) + "\n", encoding="utf-8")
    return info


def _has_dryrun_marker(tool_name: str, args: list[str]) -> bool:
    subject_hash = _dryrun_marker_subject(tool_name, args)
    marker = _dryrun_marker_path(tool_name, subject_hash)
    if not marker.is_file():
        return False
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return False
    if age > DRYRUN_MARKER_TTL_SECONDS:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return False
    return True


def _clear_dryrun_marker(tool_name: str, args: list[str]) -> None:
    subject_hash = _dryrun_marker_subject(tool_name, args)
    marker = _dryrun_marker_path(tool_name, subject_hash)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _runtime_dry_run_required_error(tool_name: str, args: list[str]) -> dict:
    info = _dryrun_marker_info(tool_name, args)
    return _error_content(
        {
            "error": {
                "type": "dry_run_required",
                "message": f"{tool_name} requires a successful dry_run=true preview before dispatching a real runtime mutation.",
                "recoverable": True,
                "tool": tool_name,
                "subject_hash": info["subject_hash"],
                "ttl_seconds": info["ttl_seconds"],
                "next_actions": [
                    f"Call {tool_name} with dry_run=true for this exact scope.",
                    "After reviewing the preview, repeat the same tool call without dry_run before the marker expires.",
                ],
            }
        }
    )


_DIRECT_HANDLERS: dict[str, Any] = {
    "skillbox_events": _handle_events,
    "skillbox_pulse": _handle_pulse,
}


def _available_tool_names() -> list[str]:
    return sorted(list(_DISPATCH.keys()) + list(_DIRECT_HANDLERS.keys()))


def _unknown_tool_error(name: str) -> dict:
    return _error_content(
        {
            "error": {
                "type": "unknown_tool",
                "message": f"Unknown tool: '{name}'.",
                "available_tools": _available_tool_names(),
                "recoverable": False,
            }
        }
    )


def _tool_params_with_defaults(name: str, params: dict) -> dict:
    tool_params = dict(params)
    if name == "skillbox_status" and not tool_params.get("full"):
        tool_params["compact"] = True
    if name == "skillbox_overlay":
        tool_params.setdefault("action", "list")
    if name == "skillbox_operator_booking":
        tool_params.setdefault("action", "availability")
    return tool_params


def _positional_arg(positional_key: str | None, tool_params: dict) -> str | None:
    if not positional_key:
        return None
    value = _clean_param_scalar(tool_params, positional_key)
    return value or None


def _missing_positional_error(name: str, positional_key: str) -> dict:
    return _error_content(
        {
            "error": {
                "type": "missing_required_parameter",
                "message": f"'{positional_key}' is required for {name}.",
                "recoverable": True,
                "recovery_hint": f"Provide a {positional_key} value, e.g. 'acme-studio'.",
            }
        }
    )


def _positional_required(positional_key: str | None, positional: str | None, tool_params: dict) -> bool:
    return bool(positional_key and positional is None and not tool_params.get("list_blueprints"))


def _validate_tool_identifiers(tool_name: str, tool_params: dict) -> str | None:
    """Validate identifier-shaped params. Returns an error message or None."""
    # Validate client_id (scalar, used as positional for onboard/focus/session/acceptance/etc.)
    client_id = str(tool_params.get("client_id") or "").strip()
    if client_id:
        try:
            _validate_identifier(client_id, "client_id")
        except ValueError as exc:
            return str(exc)

    # Validate session_id
    session_id = str(tool_params.get("session_id") or "").strip()
    if session_id:
        try:
            _validate_identifier(session_id, "session_id")
        except ValueError as exc:
            return str(exc)

    # Validate run_id
    run_id = str(tool_params.get("run_id") or "").strip()
    if run_id:
        try:
            _validate_identifier(run_id, "run_id")
        except ValueError as exc:
            return str(exc)

    # Validate proposal_id
    proposal_id = str(tool_params.get("proposal_id") or "").strip()
    if proposal_id:
        try:
            _validate_identifier(proposal_id, "proposal_id")
        except ValueError as exc:
            return str(exc)

    # Validate list-type identifier params (client, profile, service, task)
    for key in ("client", "profile", "service", "task"):
        raw = tool_params.get(key)
        if raw is None:
            continue
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for item in values:
            item_str = str(item).strip()
            if item_str:
                try:
                    _validate_identifier(item_str, key)
                except ValueError as exc:
                    return str(exc)

    # Validate skill name
    skill = str(tool_params.get("skill") or "").strip()
    if skill:
        try:
            _validate_identifier(skill, "skill")
        except ValueError as exc:
            return str(exc)

    # Only overlay uses `name` as an identifier. Other tools, notably
    # operator-booking, use it as a free-form display name.
    overlay_name = str(tool_params.get("name") or "").strip()
    if tool_name == "skillbox_overlay" and overlay_name:
        try:
            _validate_identifier(overlay_name, "name")
        except ValueError as exc:
            return str(exc)

    return None


def _dispatch_manage_tool(
    name: str,
    command: str,
    positional_key: str | None,
    params: dict,
    *,
    request_id: Any = None,
) -> dict:
    tool_params = _tool_params_with_defaults(name, params)

    # Validate identifiers before passing to subprocess
    id_error = _validate_tool_identifiers(name, tool_params)
    if id_error:
        return _error_content({"error": {"type": "invalid_parameter", "message": id_error, "recoverable": True}})

    positional = _positional_arg(positional_key, tool_params)
    if _positional_required(positional_key, positional, tool_params):
        return _missing_positional_error(name, str(positional_key))

    try:
        args = build_args(command, tool_params, positional)
    except (TypeError, ValueError) as exc:
        return _error_content({
            "error": {
                "type": "invalid_parameter",
                "message": str(exc),
                "recoverable": True,
            }
        })
    is_dry_run = bool(tool_params.get("dry_run"))
    if name in _DRY_RUN_REQUIRED_TOOLS and not is_dry_run and not _has_dryrun_marker(name, args):
        return _runtime_dry_run_required_error(name, args)

    event_context = build_tool_event_context(name, command, tool_params, request_id)
    ok, exit_code, data = run_manage(args, event_context=event_context)
    marker_info = None
    if ok and name in _DRY_RUN_REQUIRED_TOOLS and is_dry_run:
        marker_info = _stamp_dryrun_marker(name, args)
    if isinstance(data, dict):
        data["_exit_code"] = exit_code
        if marker_info is not None:
            data["mcp_dry_run_marker"] = marker_info
    if ok and name in _DRY_RUN_REQUIRED_TOOLS and not is_dry_run:
        _clear_dryrun_marker(name, args)
    return _ok_content(data) if ok else _error_content(data)


def dispatch_tool(name: str, params: dict, *, request_id: Any = None) -> dict:
    """Dispatch a tool call to manage.py and return a MCP content block."""
    if name in _DIRECT_HANDLERS:
        return _DIRECT_HANDLERS[name](params)

    route = _DISPATCH.get(name)
    if route is None:
        return _unknown_tool_error(name)

    command, positional_key = route
    return _dispatch_manage_tool(name, command, positional_key, params, request_id=request_id)


def _ok_content(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}


def _error_content(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}], "isError": True}


# ---------------------------------------------------------------------------
# MCP protocol handlers
# ---------------------------------------------------------------------------

def handle_initialize(_params: dict, _request_id: Any = None) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "logging": {},
            "tools": {"listChanged": False},
        },
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "skillbox runtime manager. "
            "Discipline: assess → scope → dry-run → act → verify. "
            "1. Run skillbox_status before any mutation. "
            "2. Pass dry_run=true first for sync/up/down/restart/bootstrap/onboard. "
            "3. Scope with client= and service= — avoid unintended side effects. "
            "4. Run skillbox_doctor after every mutation to confirm success. "
            "5. Run skillbox_logs before escalating a service error. "
            "6. Use skillbox_events to replay runtime/session failures when a previous call already exited."
        ),
    }


def handle_tools_list(_params: dict | None = None, _request_id: Any = None) -> dict:
    return {"tools": TOOLS}


def handle_tools_call(params: dict, request_id: Any = None) -> dict:
    return dispatch_tool(params.get("name", ""), params.get("arguments") or {}, request_id=request_id)


def handle_logging_set_level(params: dict, _request_id: Any = None) -> dict:
    global CURRENT_LOG_LEVEL
    CURRENT_LOG_LEVEL = _normalize_log_level((params or {}).get("level"))
    return {}


# ---------------------------------------------------------------------------
# JSON-RPC stdio loop
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Any] = {
    "initialize":  handle_initialize,
    "logging/setLevel": handle_logging_set_level,
    "tools/list":  handle_tools_list,
    "tools/call":  handle_tools_call,
    "ping":        lambda _p, _request_id=None: {},
}

_NOTIFICATION_HANDLERS: dict[str, Any] = {
    "notifications/cancelled": lambda _p: None,
    "notifications/initialized": lambda _p: None,
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
            handler = _NOTIFICATION_HANDLERS.get(method)
            if handler is not None:
                try:
                    handler(params)
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc(file=sys.stderr)
                    print(f"[skillbox-mcp] notification error in {method}: {exc}", file=sys.stderr, flush=True)
            continue

        handler = _HANDLERS.get(method)
        if handler is None:
            send_error(msg_id, -32601, f"Method not found: {method}")
            continue

        try:
            result = handler(params, msg_id)
        except JsonRpcError as exc:
            send_error(msg_id, exc.code, exc.message)
            continue
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            print(f"[skillbox-mcp] unhandled error in {method}: {exc}", file=sys.stderr, flush=True)
            send_error(msg_id, -32603, f"Internal error in {method}")
            continue

        send({"jsonrpc": "2.0", "id": msg_id, "result": result})


if __name__ == "__main__":
    main()
