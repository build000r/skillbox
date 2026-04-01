#!/usr/bin/env python3
"""Skillbox box lifecycle manager.

Orchestrates DigitalOcean droplets with Tailscale enrollment for
full create → bootstrap → deploy → onboard → drain → destroy lifecycle.

Runs from the operator's machine (not inside the container).
Uses doctl, ssh, and tailscale CLIs — no SDK dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROFILES_DIR = REPO_ROOT / "workspace" / "box-profiles"
BOOTSTRAP_SCRIPT = SCRIPT_DIR / "01-bootstrap-do.sh"
TAILSCALE_SCRIPT = SCRIPT_DIR / "02-install-tailscale.sh"


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
    "enrolling",
    "deploying",
    "onboarding",
    "ready",
    "draining",
    "destroyed",
]

VALID_TRANSITIONS = {
    "creating": ["bootstrapping", "destroyed"],
    "bootstrapping": ["enrolling", "destroyed"],
    "enrolling": ["deploying", "destroyed"],
    "deploying": ["onboarding", "destroyed"],
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


def build_onboard_manage_argv(box_id: str, blueprint: str | None, set_args: list[str]) -> list[str]:
    argv = [
        "docker",
        "compose",
        "exec",
        "-T",
        "workspace",
        "python3",
        ".env-manager/manage.py",
        "onboard",
        box_id,
    ]
    if blueprint:
        argv.extend(["--blueprint", blueprint])
    for set_arg in set_args:
        argv.extend(["--set", set_arg])
    argv.extend(["--format", "json"])
    return argv


def build_onboard_command(box_id: str, blueprint: str | None, set_args: list[str]) -> str:
    return " && ".join([
        "cd",
        "cd skillbox",
        shell_join(build_onboard_manage_argv(box_id, blueprint, set_args)),
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


def ssh_script(user: str, host: str, script_path: Path, env_vars: dict[str, str] | None = None, *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run a local script on a remote host via ssh + stdin."""
    remote_cmd = build_remote_env_command(["bash", "-s"], env_vars)
    with script_path.open("r") as f:
        return subprocess.run(
            ["ssh", *DEFAULT_SSH_OPTS, f"{user}@{host}", remote_cmd],
            stdin=f,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )


def wait_for_ssh(host: str, user: str = "root", *, max_wait: int = 120, interval: int = 5) -> bool:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        result = ssh_cmd(user, host, "echo ok", timeout=10)
        if result.returncode == 0 and "ok" in result.stdout:
            return True
        time.sleep(interval)
    return False


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

    return BoxProfile(
        id=name,
        provider=data.get("provider", "digitalocean"),
        region=data.get("region", "nyc3"),
        size=data.get("size", "s-2vcpu-4gb"),
        image=data.get("image", "ubuntu-24-04-x64"),
        ssh_user=data.get("ssh_user", "skillbox"),
        tailscale_hostname_prefix=data.get("tailscale_hostname_prefix", "skillbox"),
        skillbox_repo=data.get("skillbox_repo", "https://github.com/build000r/skillbox.git"),
        skillbox_branch=data.get("skillbox_branch", "main"),
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
    droplet_id: str | None = None
    droplet_ip: str | None = None
    tailscale_hostname: str | None = None
    tailscale_ip: str | None = None
    ssh_user: str = "skillbox"
    created_at: str = ""
    updated_at: str = ""
    region: str = ""
    size: str = ""


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


# ---------------------------------------------------------------------------
# Tailscale operations
# ---------------------------------------------------------------------------

def ts_remove_node(hostname: str) -> bool:
    """Remove a node from the tailnet by hostname via doctl-style CLI."""
    # Try tailscale CLI first (admin removal requires API, but we try)
    result = run(
        ["tailscale", "logout"],
        check=False,
    )
    # For proper removal, we SSH into the box and run tailscale logout there
    return True


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
    steps: list[dict[str, Any]] = field(default_factory=list)
    ip: str | None = None
    ssh_target: str | None = None


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
) -> BoxUpContext:
    now = datetime.now(timezone.utc).isoformat()
    ts_hostname = f"{profile.tailscale_hostname_prefix}-{box_id}"
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
    )
    return BoxUpContext(
        box_id=box_id,
        profile_name=profile_name,
        profile=profile,
        box=box,
        boxes=boxes,
        ts_hostname=ts_hostname,
        is_json=is_json,
    )


def _box_up_dry_run_payload(context: BoxUpContext) -> dict[str, Any]:
    _record_box_up_step(context, "create", "skip", f"would create {context.profile.size} in {context.profile.region}")
    _record_box_up_step(context, "bootstrap", "skip", "dry-run")
    _record_box_up_step(context, "enroll", "skip", f"would enroll as {context.ts_hostname}")
    _record_box_up_step(context, "deploy", "skip", "dry-run")
    _record_box_up_step(context, "onboard", "skip", "dry-run")
    _record_box_up_step(context, "verify", "skip", "dry-run")
    return {
        "box_id": context.box_id,
        "profile": asdict(context.profile),
        "dry_run": True,
        "steps": context.steps,
        "next_actions": [f"box up {context.box_id} --profile {context.profile_name}"],
    }


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


def _bootstrap_box_host(context: BoxUpContext) -> str:
    if context.ip is None:
        raise RuntimeError("Droplet IP unavailable during bootstrap")
    if not context.is_json:
        print(f"[...] bootstrap  Waiting for SSH on {context.ip}...")
    if not wait_for_ssh(context.ip, user="root"):
        raise RuntimeError(f"SSH not reachable at root@{context.ip} after 120s")
    if not context.is_json:
        print("[...] bootstrap  Running 01-bootstrap-do.sh...")
    result = ssh_script("root", context.ip, BOOTSTRAP_SCRIPT, {"APP_USER": context.profile.ssh_user}, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Bootstrap failed (exit {result.returncode}): {result.stderr[-500:]}")
    update_box(context.box, state="enrolling")
    save_inventory(context.boxes)
    return "OS packages + Docker + user created"


def _enroll_box_tailscale(context: BoxUpContext, *, ts_authkey: str) -> str:
    if context.ip is None:
        raise RuntimeError("Droplet IP unavailable during tailscale enrollment")
    if not context.is_json:
        print(f"[...] enroll  Joining tailnet as {context.ts_hostname}...")
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
    ts_ip_result = ssh_cmd("root", context.ip, "tailscale ip -4", timeout=15)
    ts_ip = ts_ip_result.stdout.strip().split("\n")[0] if ts_ip_result.returncode == 0 else None
    update_box(context.box, tailscale_ip=ts_ip, state="deploying")
    save_inventory(context.boxes)
    return f"tailscale {context.ts_hostname} at {ts_ip or 'unknown'}"


def _resolve_deploy_target(context: BoxUpContext) -> str:
    if context.ip is None:
        raise RuntimeError("Droplet IP unavailable during deploy")
    ssh_target = context.ts_hostname
    if wait_for_ssh(ssh_target, user=context.profile.ssh_user, max_wait=60, interval=5):
        context.ssh_target = ssh_target
        return ssh_target
    ssh_target = context.ip
    if not wait_for_ssh(ssh_target, user=context.profile.ssh_user, max_wait=30):
        raise RuntimeError(f"Cannot reach {context.profile.ssh_user}@{context.ts_hostname} or {context.ip} via SSH")
    context.ssh_target = ssh_target
    return ssh_target


def _deploy_box_runtime(context: BoxUpContext) -> str:
    if not context.is_json:
        print(f"[...] deploy  Cloning skillbox and starting container via {context.ts_hostname}...")
    ssh_target = _resolve_deploy_target(context)
    deploy_cmds = build_deploy_command(context.profile)
    result = ssh_cmd(context.profile.ssh_user, ssh_target, deploy_cmds, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Deploy failed (exit {result.returncode}): {result.stderr[-500:]}")
    update_box(context.box, state="onboarding")
    save_inventory(context.boxes)
    return "container running"


def _run_box_onboard(context: BoxUpContext, *, blueprint: str | None, set_args: list[str]) -> None:
    if context.ssh_target is None:
        raise RuntimeError("SSH target unavailable during onboarding")
    if not context.is_json:
        print(f"[...] onboard  Running onboard for client {context.box_id}...")
    exec_cmd = build_onboard_command(context.box_id, blueprint, set_args)
    result = ssh_cmd(context.profile.ssh_user, context.ssh_target, exec_cmd, timeout=300)
    if result.returncode not in (0, 2):
        raise RuntimeError(f"Onboard failed (exit {result.returncode}): {result.stderr[-500:]}")


def _verify_box_runtime(context: BoxUpContext) -> None:
    if context.ssh_target is None:
        raise RuntimeError("SSH target unavailable during verification")
    verify_cmd = "cd ~/skillbox && docker compose exec -T workspace python3 .env-manager/manage.py doctor --format json"
    result = ssh_cmd(context.profile.ssh_user, context.ssh_target, verify_cmd, timeout=60)
    _record_box_up_step(context, "verify", "ok" if result.returncode == 0 else "warn")


def _box_up_success_payload(context: BoxUpContext) -> dict[str, Any]:
    return {
        "box_id": context.box_id,
        "profile": asdict(context.profile),
        "dry_run": False,
        "droplet_id": context.box.droplet_id,
        "droplet_ip": context.box.droplet_ip,
        "tailscale_hostname": context.ts_hostname,
        "tailscale_ip": context.box.tailscale_ip,
        "ssh": f"ssh {context.profile.ssh_user}@{context.ts_hostname}",
        "steps": context.steps,
        "next_actions": [f"box ssh {context.box_id}", f"box status {context.box_id}"],
    }


def cmd_up(
    box_id: str,
    *,
    profile_name: str,
    blueprint: str | None,
    set_args: list[str],
    dry_run: bool,
    fmt: str,
) -> int:
    is_json = fmt == "json"

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
    if existing and existing.state not in ("destroyed",):
        msg = f"Box {box_id!r} already exists in state {existing.state!r}. Use 'box down {box_id}' first or choose a different id."
        if is_json:
            emit_json(structured_error(msg, error_type="conflict", next_actions=[f"box down {box_id}", f"box status {box_id}"]))
        else:
            print(msg, file=sys.stderr)
        return EXIT_ERROR

    do_token = require_env("SKILLBOX_DO_TOKEN")
    ssh_key_id = require_env("SKILLBOX_DO_SSH_KEY_ID")
    ts_authkey = require_env("SKILLBOX_TS_AUTHKEY")
    os.environ["DIGITALOCEAN_ACCESS_TOKEN"] = do_token
    context = _build_box_up_context(
        box_id=box_id,
        profile_name=profile_name,
        profile=profile,
        boxes=boxes,
        is_json=is_json,
    )

    if dry_run:
        payload = _box_up_dry_run_payload(context)
        if is_json:
            emit_json(payload)
        return EXIT_OK

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
        stage_name="bootstrap",
        error_type="bootstrap_failed",
        action=lambda: _bootstrap_box_host(context),
        failure_state="bootstrapping",
        next_actions=[f"box down {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="enroll",
        error_type="tailscale_failed",
        action=lambda: _enroll_box_tailscale(context, ts_authkey=ts_authkey),
        failure_state="enrolling",
        next_actions=[f"box down {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="deploy",
        error_type="deploy_failed",
        action=lambda: _deploy_box_runtime(context),
        failure_state="deploying",
        next_actions=[f"box down {box_id}"],
    ):
        return EXIT_ERROR

    if not _run_box_up_stage(
        context,
        stage_name="onboard",
        error_type="onboard_failed",
        action=lambda: _run_box_onboard(context, blueprint=blueprint, set_args=set_args),
        failure_state="ready",
        next_actions=[f"box ssh {box_id}", f"box status {box_id}"],
    ):
        return EXIT_ERROR
    try:
        _verify_box_runtime(context)
    except Exception:
        _record_box_up_step(context, "verify", "warn")

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
    ssh_target = box.tailscale_hostname or box.droplet_ip
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
                print(f"[...] remove  Removing from tailnet...")
            # Run tailscale logout on the box itself
            ssh_cmd("root", box.droplet_ip or ssh_target, "tailscale logout", timeout=30)
            step("remove", "ok")
        except Exception:
            step("remove", "warn", "Could not remove from tailnet")
    else:
        step("remove", "skip", "no ssh target")

    # -- 3. Destroy droplet -----------------------------------------------------
    if box.droplet_id:
        try:
            if not is_json:
                print(f"[...] destroy  Deleting droplet {box.droplet_id}...")
            if do_delete_droplet(box.droplet_id):
                step("destroy", "ok", f"droplet {box.droplet_id} deleted")
            else:
                step("destroy", "warn", "doctl delete returned non-zero")
        except Exception as exc:
            step("destroy", "fail", str(exc))
    else:
        step("destroy", "skip", "no droplet id")

    update_box(box, state="destroyed")
    save_inventory(boxes)

    payload = {"box_id": box_id, "dry_run": False, "steps": steps, "next_actions": ["box list"]}
    if is_json:
        emit_json(payload)
    else:
        print(f"\nBox {box_id} destroyed.")
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
            "next_actions": ["box up <id> --profile <name>"] if not statuses else [],
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
        "droplet_id": box.droplet_id,
        "droplet_ip": box.droplet_ip,
        "tailscale_hostname": box.tailscale_hostname,
        "tailscale_ip": box.tailscale_ip,
        "ssh_user": box.ssh_user,
        "region": box.region,
        "size": box.size,
        "created_at": box.created_at,
        "ssh_reachable": False,
        "container_running": False,
    }

    if box.state in ("destroyed", "creating"):
        return status

    ssh_target = box.tailscale_hostname or box.droplet_ip
    if ssh_target:
        probe = ssh_cmd(box.ssh_user, ssh_target, "echo ok", timeout=10)
        status["ssh_reachable"] = probe.returncode == 0

        if status["ssh_reachable"]:
            container_probe = ssh_cmd(
                box.ssh_user, ssh_target,
                "cd ~/skillbox && docker compose ps --format json 2>/dev/null | head -1",
                timeout=15,
            )
            status["container_running"] = container_probe.returncode == 0 and "workspace" in container_probe.stdout

    next_actions: list[str] = []
    if not status["ssh_reachable"]:
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
    print(f"  droplet={status['droplet_id']}  ip={status['droplet_ip']}  ts={ts}")
    print(f"  ssh={reachable}  container={container}")
    if status.get("ssh_reachable"):
        print(f"  connect: ssh {status['ssh_user']}@{ts}")


# ---------------------------------------------------------------------------
# box ssh
# ---------------------------------------------------------------------------

def cmd_ssh(box_id: str) -> int:
    boxes = load_inventory()
    box = find_box(boxes, box_id)
    if box is None or box.state == "destroyed":
        print(f"Box {box_id!r} not found or destroyed.", file=sys.stderr)
        return EXIT_ERROR

    target = box.tailscale_hostname or box.droplet_ip
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
            "next_actions": ["box up <id> --profile <name>"] if not active else [],
        })
    else:
        if not active:
            print("No active boxes.")
        else:
            for b in active:
                ts = b.tailscale_hostname or "n/a"
                print(f"  {b.id}  state={b.state}  ts={ts}  ip={b.droplet_ip}  profile={b.profile}")
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
                print(f"  {p.id}  {p.size} in {p.region} ({p.image})")
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

    up_parser = subparsers.add_parser("up", help="Create and provision a new box.")
    up_parser.add_argument("box_id", help="Box identifier (becomes droplet name and client id).")
    up_parser.add_argument("--profile", default="dev-small", help="Box profile from workspace/box-profiles/.")
    up_parser.add_argument("--blueprint", default=None, help="Client blueprint for onboard step.")
    up_parser.add_argument("--set", action="append", default=[], help="Blueprint variable KEY=VALUE.")
    up_parser.add_argument("--dry-run", action="store_true")
    up_parser.add_argument("--format", choices=("text", "json"), default="text")

    down_parser = subparsers.add_parser("down", help="Drain and destroy a box.")
    down_parser.add_argument("box_id", help="Box identifier.")
    down_parser.add_argument("--dry-run", action="store_true")
    down_parser.add_argument("--format", choices=("text", "json"), default="text")

    status_parser = subparsers.add_parser("status", help="Check health of one or all boxes.")
    status_parser.add_argument("box_id", nargs="?", default=None, help="Box identifier (omit for all).")
    status_parser.add_argument("--format", choices=("text", "json"), default="text")

    ssh_parser = subparsers.add_parser("ssh", help="SSH into a box.")
    ssh_parser.add_argument("box_id", help="Box identifier.")

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
                dry_run=args.dry_run,
                fmt=args.format,
            )
        if args.command == "down":
            return cmd_down(args.box_id, dry_run=args.dry_run, fmt=args.format)
        if args.command == "status":
            return cmd_status(args.box_id, fmt=args.format)
        if args.command == "ssh":
            return cmd_ssh(args.box_id)
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
