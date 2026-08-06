#!/usr/bin/env python3
"""Offline, sanitized project-Orb capability collector and receipt writer."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DECLARATION_SCHEMA = "skillbox.amp-project-orb.capabilities/1"
RECEIPT_SCHEMA = "skillbox.amp-project-orb.readiness/1"
HOOK_STATUS_SCHEMA = "skillbox.amp-project-orb.hook-status/1"
ALLOWED_STATES = {"ready", "configured", "degraded", "blocked", "forbidden"}
STATUS_REASONS = {
    "setup_unexpected", "setup_ready", "resume_unexpected", "resume_ready",
    "timeout_command_missing", "python_missing", "git_missing", "hook_state_invalid",
    "status_write_failed", "disk_capacity_low",
    "disk_probe_timeout", "disk_probe_failed",
    "compileall_timeout", "compileall_failed", "unittest_timeout", "unittest_failed",
    "identity_timeout", "identity_failed", "readiness_timeout", "readiness_failed",
}


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: dict) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    atomic_bytes(path, payload)


def _probe_ready(probe: dict[str, object], env: dict[str, str]) -> bool:
    kind = probe.get("kind")
    if kind == "command":
        name = probe.get("name")
        return isinstance(name, str) and bool(name) and shutil.which(name) is not None
    if kind == "path":
        relative = probe.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            return False
        return (ROOT / relative).exists()
    if kind == "environment_set":
        names = probe.get("names")
        return (
            isinstance(names, list)
            and bool(names)
            and all(isinstance(name, str) and name and bool(env.get(name, "").strip()) for name in names)
        )
    raise ValueError("unsupported capability probe")


def _validated_declaration() -> dict[str, object]:
    declaration = json.loads((ROOT / ".agents/orb-capabilities.json").read_text())
    if (
        not isinstance(declaration, dict)
        or declaration.get("schema_version") != DECLARATION_SCHEMA
        or declaration.get("project_alias") != "build000r/skillbox"
        or not isinstance(declaration.get("capabilities"), list)
    ):
        raise ValueError("invalid project-Orb capability declaration")
    return declaration


def collect(context: str, *, env: dict[str, str] | None = None) -> dict:
    declaration = _validated_declaration()
    observed_env = dict(os.environ if env is None else env)
    results = []
    required_missing = False
    optional_missing = False
    for item in declaration["capabilities"]:
        if not isinstance(item, dict):
            raise TypeError("capability entries must be objects")
        capability_class = item["class"]
        if capability_class == "forbidden_authority":
            state, reason = "forbidden", "authority_forbidden_in_ordinary_orb"
        else:
            if capability_class not in {"required_local", "optional_presence"}:
                raise ValueError("unsupported capability class")
            probes = item.get("probes")
            if not isinstance(probes, list) or not probes:
                raise ValueError("enabled capabilities require probes")
            present = all(
                isinstance(probe, dict) and _probe_ready(probe, observed_env)
                for probe in probes
            )
            if present and capability_class == "required_local":
                state, reason = "ready", "required_local_ready"
            elif present:
                state, reason = "configured", "optional_capability_configured"
            elif capability_class == "required_local":
                state, reason, required_missing = "blocked", "required_local_missing", True
            else:
                state, reason, optional_missing = "degraded", "optional_presence_missing", True
        results.append({"id": item["id"], "class": capability_class, "state": state, "reason_code": reason})
    state = "blocked" if required_missing else ("degraded" if optional_missing else "ready")
    assert state in ALLOWED_STATES
    return {
        "schema_version": RECEIPT_SCHEMA,
        "project_alias": declaration["project_alias"],
        "context": context,
        "state": state,
        "reason_code": {
            "ready": "declared_capabilities_ready",
            "degraded": "optional_capability_not_configured",
            "blocked": "required_local_capability_missing",
        }[state],
        "network_attempted": False,
        "external_readiness_claimed": False,
        "capabilities": results,
    }


def ensure_identity(path: Path) -> None:
    if path.is_file() and not path.is_symlink():
        try:
            value = path.read_text(encoding="ascii").strip()
            if uuid.UUID(value).version == 4:
                os.chmod(path, 0o600)
                return
        except (OSError, UnicodeDecodeError, ValueError):
            pass
    atomic_bytes(path, f"{uuid.uuid4()}\n".encode("ascii"))


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("hook state directories must not be symlinks")
    os.chmod(path, 0o700)


def prepare_hook(state_dir: Path, log_dir: Path, script: str) -> None:
    _private_directory(state_dir)
    _private_directory(log_dir)
    status = state_dir / f"{script}-status.json"
    log = log_dir / f"{script}.log"
    for path in (status, log):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("hook state files must be regular files")
    if status.exists():
        status.unlink()
    atomic_bytes(log, b"")


def write_status() -> None:
    # Only enumerated/fixed caller values are admitted; no environment contents are copied.
    script = os.environ.get("SCRIPT", "")
    status = os.environ.get("STATUS", "")
    failure = os.environ.get("FAILURE_CLASS", "")
    reason = os.environ.get("REASON_CODE", "")
    if script not in {"setup", "resume"} or status not in {"ok", "error"}:
        raise SystemExit(2)
    if failure not in {"none", "setup", "dependency", "capacity", "auth", "validation"}:
        raise SystemExit(2)
    if reason not in STATUS_REASONS:
        raise SystemExit(2)
    receipt = {"schema_version": HOOK_STATUS_SCHEMA, "kind": "skillbox.agent_hook_status", "script": script,
               "status": status, "failure_class": failure, "reason_code": reason,
               "exit_code": int(os.environ["EXIT_CODE"]),
               "duration_seconds": max(0, int(time.time()) - int(os.environ["START"]))}
    atomic_json(Path(os.environ["STATUS_FILE"]), receipt)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--context", choices=("setup", "resume", "manual"), default="manual")
    collect_parser.add_argument("--output", type=Path)
    identity_parser = sub.add_parser("ensure-identity")
    identity_parser.add_argument("--output", type=Path, required=True)
    prepare_parser = sub.add_parser("prepare-hook")
    prepare_parser.add_argument("--state-dir", type=Path, required=True)
    prepare_parser.add_argument("--log-dir", type=Path, required=True)
    prepare_parser.add_argument("--script", choices=("setup", "resume"), required=True)
    sub.add_parser("write-status")
    args = parser.parse_args()
    if args.command == "write-status":
        write_status()
    elif args.command == "prepare-hook":
        prepare_hook(args.state_dir, args.log_dir, args.script)
    elif args.command == "ensure-identity":
        ensure_identity(args.output)
    else:
        if delay := os.environ.get("SKILLBOX_ORB_TEST_READINESS_DELAY_SECONDS"):
            time.sleep(float(delay))
        receipt = collect(args.context)
        if args.output:
            atomic_json(args.output, receipt)
        else:
            print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
