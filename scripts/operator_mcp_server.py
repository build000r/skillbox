#!/usr/bin/env python3
"""
skillbox operator MCP server — fleet and container lifecycle as native agent tools.

Runs on the operator's machine (outside the container).
Wraps box.py (DO+Tailscale fleet), docker compose (container lifecycle),
and 04-reconcile.py (outer validation) as MCP tools.

Protocol: JSON-RPC 2.0 over stdio (MCP 2024-11-05).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BOX_PY = SCRIPT_DIR / "box.py"
RECONCILE_PY = SCRIPT_DIR / "04-reconcile.py"
ENV_FILE = REPO_ROOT / ".env"
ENV_BOX_FILE = REPO_ROOT / ".env.box"

SERVER_NAME = "skillbox-operator"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

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
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "operator_boxes",
        "description": (
            "List all active boxes from inventory (workspace/boxes.json). "
            "Shows box ID, state, profile, droplet IP, and Tailscale hostname. "
            "RUN THIS FIRST to understand the current fleet before any operation."
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
            "Requires env: SKILLBOX_DO_TOKEN, SKILLBOX_DO_SSH_KEY_ID, SKILLBOX_TS_AUTHKEY."
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
                    "description": "Client blueprint for the onboard step (e.g. 'git-repo-http-service-bootstrap').",
                },
                "set_vars": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Blueprint variables as KEY=VALUE strings. "
                        "Example: ['PRIMARY_REPO_URL=https://github.com/acme/app.git']."
                    ),
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
            "For interactive SSH, use 'make box-ssh BOX=<id>' instead."
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
            },
        },
    },
    # --- Local container lifecycle ---
    {
        "name": "operator_compose_up",
        "description": (
            "Build the workspace image and start the local container (docker compose build + up -d). "
            "Use on the operator machine to bring up the local skillbox workspace. "
            "Pass build=false to skip the image build and only start."
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
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "operator_render",
        "description": (
            "Print the resolved sandbox model: box shape, runtime paths, ports, dependencies. "
            "Uses scripts/04-reconcile.py render. Read-only, no side effects. "
            "Use to understand what the current configuration will produce."
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
        print(f"[operator-mcp] {script.name} stderr: {proc.stderr.strip()}", file=sys.stderr, flush=True)

    stdout = proc.stdout.strip()
    if stdout:
        try:
            return proc.returncode in (0, 2), proc.returncode, json.loads(stdout)
        except json.JSONDecodeError:
            return proc.returncode == 0, proc.returncode, {"text": stdout}

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
            return ok, proc.returncode, json.loads(stdout)
        except json.JSONDecodeError:
            pass

    return ok, proc.returncode, {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
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
    cmd = ["ssh", *ssh_opts, f"{user}@{host}", command]
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
            return ok, proc.returncode, json.loads(stdout)
        except json.JSONDecodeError:
            pass

    return ok, proc.returncode, {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": proc.stderr.strip(),
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
    box_id = params.get("box_id")
    if box_id:
        args.insert(1, str(box_id))
    ok, _code, data = run_script(BOX_PY, args)
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_provision(params: dict) -> dict:
    box_id = params.get("box_id")
    if not box_id:
        return _error_content({
            "error": {
                "type": "missing_required_parameter",
                "message": "'box_id' is required for operator_provision.",
                "recoverable": True,
            }
        })

    args = ["up", str(box_id), "--format", "json"]
    if params.get("profile"):
        args += ["--profile", str(params["profile"])]
    if params.get("deploy_manifest"):
        args += ["--deploy-manifest", str(params["deploy_manifest"])]
    if params.get("blueprint"):
        args += ["--blueprint", str(params["blueprint"])]
    for sv in (params.get("set_vars") or []):
        args += ["--set", str(sv)]
    if params.get("dry_run"):
        args.append("--dry-run")

    ok, _code, data = run_script(BOX_PY, args, timeout=900)
    emit_event("operator.provision", str(box_id), {"ok": ok, "dry_run": bool(params.get("dry_run"))})
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_teardown(params: dict) -> dict:
    box_id = params.get("box_id")
    if not box_id:
        return _error_content({
            "error": {
                "type": "missing_required_parameter",
                "message": "'box_id' is required for operator_teardown.",
                "recoverable": True,
            }
        })

    args = ["down", str(box_id), "--format", "json"]
    is_dry_run = bool(params.get("dry_run"))
    if is_dry_run:
        args.append("--dry-run")

    ok, _code, data = run_script(BOX_PY, args, timeout=300)
    emit_event("operator.teardown", str(box_id), {"ok": ok, "dry_run": is_dry_run})

    # Stamp dry-run marker so the PreToolUse hook allows the real run next.
    if ok and is_dry_run:
        _stamp_dryrun_marker("operator_teardown", str(box_id))

    return _ok_content(data) if ok else _error_content(data)


def handle_operator_box_exec(params: dict) -> dict:
    box_id = params.get("box_id")
    command = params.get("command")
    if not box_id or not command:
        return _error_content({
            "error": {
                "type": "missing_required_parameter",
                "message": "'box_id' and 'command' are required for operator_box_exec.",
                "recoverable": True,
            }
        })

    box = find_box(str(box_id))
    if box is None or box.get("state") == "destroyed":
        return _error_content({
            "error": {
                "type": "box_not_found",
                "message": f"Box '{box_id}' not found or destroyed.",
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
                "message": f"Box '{box_id}' has no reachable address.",
                "recoverable": False,
            }
        })

    timeout = int(params.get("timeout", 120))
    ok, _code, data = run_ssh(user, host, str(command), timeout=timeout)
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_compose_up(params: dict) -> dict:
    results: list[dict[str, Any]] = []

    if params.get("build", True):
        ok, code, data = run_compose(["build"], timeout=600)
        results.append({"step": "build", "ok": ok, "exit_code": code, "detail": data})
        if not ok:
            return _error_content({
                "steps": results,
                "error": {"type": "build_failed", "message": "docker compose build failed.", "recoverable": True},
            })

    ok, code, data = run_compose(["up", "-d"], timeout=120)
    results.append({"step": "up", "ok": ok, "exit_code": code, "detail": data})

    if params.get("surfaces") and ok:
        ok_s, code_s, data_s = run_compose(["--profile", "surfaces", "up", "-d"], timeout=60)
        results.append({"step": "up-surfaces", "ok": ok_s, "exit_code": code_s, "detail": data_s})

    all_ok = all(r["ok"] for r in results)
    emit_event("operator.compose_up", "local", {"ok": all_ok})
    payload = {
        "steps": results,
        "next_actions": ["operator_doctor"] if all_ok else [],
    }
    return _ok_content(payload) if all_ok else _error_content(payload)


def handle_operator_compose_down(params: dict) -> dict:
    is_dry_run = bool(params.get("dry_run"))
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

    ok, code, data = run_compose(["down"], timeout=120)
    emit_event("operator.compose_down", "local", {"ok": ok})
    payload = {"ok": ok, "exit_code": code, "detail": data}
    return _ok_content(payload) if ok else _error_content(payload)


def handle_operator_doctor(_params: dict) -> dict:
    ok, _code, data = run_script(RECONCILE_PY, ["doctor", "--format", "json"])
    return _ok_content(data) if ok else _error_content(data)


def handle_operator_render(params: dict) -> dict:
    args = ["render", "--format", "json"]
    if params.get("with_compose"):
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


# ---------------------------------------------------------------------------
# Dry-run marker (coordinates with PreToolUse hook)
# ---------------------------------------------------------------------------

def _stamp_dryrun_marker(tool_name: str, box_id: str) -> None:
    """Create a temp marker so the PreToolUse hook knows a dry-run was done."""
    marker = REPO_ROOT / ".skillbox-state" / "dryrun-markers" / f".skillbox-dryrun-{tool_name}-{box_id}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"dry-run completed for {tool_name} box={box_id}\n")


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------

def _ok_content(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}]}


def _error_content(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}], "isError": True}


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
            "3. Use operator_provision with dry_run=true before creating infrastructure. "
            "4. CONFIRM WITH USER before operator_teardown — it destroys infrastructure. "
            "5. Use operator_box_exec to run commands on remote boxes. "
            "6. Use operator_doctor to validate the local repo state. "
            "SAFETY: Destructive tools (teardown, compose_down) are gated by a PreToolUse hook. "
            "The hook BLOCKS execution if: (a) there are uncommitted changes (run /commit first), "
            "or (b) no dry_run=true was run first. Always dry-run, then confirm with user, then execute."
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
    load_dotenv(ENV_FILE)
    load_dotenv(ENV_BOX_FILE)
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
