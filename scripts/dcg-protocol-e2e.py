#!/usr/bin/env python3
"""Multi-agent DCG protocol e2e harness (skillbox-dcg-agent-protocol-e2e-ln4z).

Invokes the **real** pinned `dcg` binary through the **actual** generated agent
hook protocols, in a disposable home, and proves four things per agent:

  * a harmless command is ALLOWED
  * a destructive command is DENIED
  * malformed input FAILS CLOSED (never a default allow)
  * the guarded command NEVER EXECUTES

The last one is the point of the whole exercise, so it is not taken on trust: a
sentinel path is handed to the harness and asserted absent afterwards. The
harness itself never runs a payload -- it only hands the string to the hook and
reads the verdict -- and the sentinel is the independent check on that claim.

Verdict boundary (observed against dcg 0.6.7, not assumed):
  * allow -> exit 0 and EMPTY stdout
  * deny  -> exit 0 and stdout JSON carrying
             hookSpecificOutput.permissionDecision == "deny"
Exit code alone does not distinguish them, which is exactly the kind of detail a
mocked boundary would get wrong.

Standard library only. Writes nothing outside --home and --output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "dcg.protocol.e2e.receipt/1"
OK_MARKER = "DCG_PROTOCOL_E2E_OK"

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_INDETERMINATE = "indeterminate"

DEFAULT_TIMEOUT_SECONDS = 20


class HarnessError(RuntimeError):
    """Typed harness failure. Every one of these must fail the run closed."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def sha256_file_or_absent(path: Path) -> str:
    path = Path(path)
    return sha256_file(path) if path.is_file() else "absent"


def resolve_binary(explicit: str | None) -> Path:
    candidate = explicit or os.environ.get("SKILLBOX_DCG_BIN") or shutil.which("dcg")
    if not candidate:
        raise HarnessError(
            "no dcg binary found; the verdict boundary must be the real binary, "
            "not a stand-in (pass --binary or put dcg on PATH)"
        )
    path = Path(candidate)
    if not path.is_file():
        raise HarnessError(f"dcg binary not found at {path}")
    if not os.access(path, os.X_OK):
        raise HarnessError(f"dcg binary is not executable: {path}")
    return path


def binary_version(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=False, timeout=30
    )
    if result.returncode != 0:
        raise HarnessError(f"`dcg --version` failed: {result.stderr.strip()}")
    return (result.stdout or "").strip().splitlines()[0].strip()


def implementation_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode != 0:
        raise HarnessError("cannot resolve implementation SHA; identity is required")
    return result.stdout.strip()


def build_home(home: Path, binary: Path) -> dict[str, Any]:
    """Materialize a disposable home carrying the REAL generated hook documents.

    The hook documents come from runtime_manager.dcg_reconcile's own generators,
    so this exercises the protocol Skillbox actually installs rather than a
    hand-written approximation.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".env-manager"))
    from runtime_manager import dcg_reconcile as R  # noqa: PLC0415

    home = Path(home)
    layout = R.layout(home, binary)
    for target in (layout.claude_settings, layout.codex_hooks, layout.grok_hook, layout.policy_config):
        target.parent.mkdir(parents=True, exist_ok=True)

    claude_doc = {"hooks": {R.HOOK_EVENT: [R.claude_matcher_group(binary)]}}
    codex_doc = {"hooks": {R.HOOK_EVENT: [R.claude_matcher_group(binary)]}}
    grok_doc = R.grok_hook_document(binary)

    layout.claude_settings.write_text(json.dumps(claude_doc, indent=2) + "\n", encoding="utf-8")
    layout.codex_hooks.write_text(json.dumps(codex_doc, indent=2) + "\n", encoding="utf-8")
    layout.grok_hook.write_text(json.dumps(grok_doc, indent=2) + "\n", encoding="utf-8")

    # A policy must exist for the run to be meaningful; absent policy is a fault.
    if not layout.policy_config.is_file():
        from runtime_manager import dcg_policy as P  # noqa: PLC0415

        layout.policy_config.write_text(P.render(), encoding="utf-8")

    return {
        "home": str(home),
        "claude_settings": layout.claude_settings,
        "codex_hooks": layout.codex_hooks,
        "grok_hook": layout.grok_hook,
        "codex_config": layout.codex_config,
        "policy_config": layout.policy_config,
    }


def hook_command_for(agent: str, paths: dict[str, Any]) -> list[str]:
    """The command the agent itself would run, read out of the generated hook."""
    if agent == "claude":
        document = json.loads(Path(paths["claude_settings"]).read_text(encoding="utf-8"))
        groups = document["hooks"]["PreToolUse"]
    elif agent == "codex":
        document = json.loads(Path(paths["codex_hooks"]).read_text(encoding="utf-8"))
        groups = document["hooks"]["PreToolUse"]
    elif agent == "grok":
        document = json.loads(Path(paths["grok_hook"]).read_text(encoding="utf-8"))
        groups = document["hooks"]["PreToolUse"]
    else:
        raise HarnessError(f"unsupported agent: {agent}")
    for group in groups:
        for entry in group.get("hooks") or []:
            if entry.get("type") == "command" and entry.get("command"):
                return [str(entry["command"])]
    raise HarnessError(f"no hook command registered for {agent}")


def classify(stdout: str) -> str:
    """Map real hook stdout onto a verdict. Unknown shapes are NOT allow."""
    text = (stdout or "").strip()
    if not text:
        return DECISION_ALLOW
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return DECISION_INDETERMINATE
    decision = (
        (payload.get("hookSpecificOutput") or {}).get("permissionDecision")
        if isinstance(payload, dict)
        else None
    )
    if decision == "deny":
        return DECISION_DENY
    if decision in ("allow", "ask"):
        return DECISION_ALLOW
    return DECISION_INDETERMINATE


def invoke_hook(
    command: list[str], payload_bytes: bytes, home: Path, timeout: int
) -> dict[str, Any]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    started = time.time()
    try:
        result = subprocess.run(
            command, input=payload_bytes, capture_output=True, check=False,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        # A hook that hangs must fail closed, not silently allow.
        return {
            "decision": DECISION_INDETERMINATE,
            "exit_code": None,
            "timed_out": True,
            "elapsed_s": round(time.time() - started, 3),
            "payload_sha256": sha256_bytes(payload_bytes),
            "executed": False,
        }
    stdout = result.stdout.decode("utf-8", "replace")
    return {
        "decision": classify(stdout),
        "exit_code": result.returncode,
        "timed_out": False,
        "elapsed_s": round(time.time() - started, 3),
        "payload_sha256": sha256_bytes(payload_bytes),
        "executed": False,
        "reason": (
            json.loads(stdout).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")[:200]
            if stdout.strip().startswith("{")
            else ""
        ),
    }


def run(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    fixture_root = Path(args.fixture_root)
    protocol = json.loads((fixture_root / "protocol.json").read_text(encoding="utf-8"))

    binary = resolve_binary(args.binary)
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    paths = build_home(home, binary)

    sentinel = Path(args.sentinel) if args.sentinel else home / "EXECUTED_SENTINEL"
    if sentinel.exists():
        raise HarnessError(f"execution sentinel already present before the run: {sentinel}")

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    agents: list[dict[str, Any]] = []
    for spec in protocol["agents"]:
        name = spec["name"]
        command = hook_command_for(name, paths)
        safe_bytes = (fixture_root / "payloads" / spec["safe"]).read_bytes()
        destructive_bytes = (fixture_root / "payloads" / spec["destructive"]).read_bytes()
        agents.append(
            {
                "name": name,
                "agent": name,
                "hook_relpath": spec["hook_relpath"],
                "hook_command": command,
                "safe": invoke_hook(command, safe_bytes, home, args.timeout),
                "destructive": invoke_hook(command, destructive_bytes, home, args.timeout),
                "executed": False,
                "timestamp": stamp,
            }
        )

    malformed_bytes = (fixture_root / "payloads" / protocol["malformed_payload"]).read_bytes()
    malformed = invoke_hook(hook_command_for("claude", paths), malformed_bytes, home, args.timeout)

    limitations = []
    for probe in protocol.get("limitation_probes") or []:
        probe_bytes = (fixture_root / "payloads" / probe["payload"]).read_bytes()
        observed = invoke_hook(hook_command_for("codex", paths), probe_bytes, home, args.timeout)
        limitations.append(
            {
                "name": probe["name"],
                "why": probe["why"],
                "payload_sha256": observed["payload_sha256"],
                "observed_decision": observed["decision"],
                "guarded": observed["decision"] == DECISION_DENY,
            }
        )

    # Codex trust is a REAL limitation, not a formality: Codex will not run a
    # hook it has not trusted, so a byte-perfect hooks.json can guard nothing.
    codex_config = Path(paths["codex_config"])
    limitations.append(
        {
            "name": "codex-trust",
            "why": "Codex runs a hook only after the operator trusts it; an untrusted hook guards nothing",
            "trust_state": "absent" if not codex_config.is_file() else "present",
            "guarded": codex_config.is_file(),
        }
    )

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "implementation_sha": implementation_sha(repo_root),
        "binary_version": binary_version(binary),
        "binary_sha256": sha256_file(binary),
        "policy_sha256": sha256_file_or_absent(paths["policy_config"]),
        "hook_state_sha256": sha256_bytes(
            b"".join(
                sha256_file_or_absent(Path(paths[key])).encode("utf-8")
                for key in ("claude_settings", "codex_hooks", "grok_hook", "codex_config")
            )
        ),
        "timestamp": stamp,
        "home": str(home),
        "agents": agents,
        "malformed": malformed,
        "limitations": limitations,
        "sentinel": {"path": str(sentinel), "present": sentinel.exists()},
        "executed": False,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    problems = []
    for entry in agents:
        if entry["safe"]["decision"] != DECISION_ALLOW:
            problems.append(f"{entry['name']}: harmless command was not allowed")
        if entry["destructive"]["decision"] != DECISION_DENY:
            problems.append(f"{entry['name']}: destructive command was not denied")
    if malformed["decision"] == DECISION_ALLOW:
        problems.append("malformed payload was ALLOWED; it must fail closed")
    if sentinel.exists():
        problems.append("execution sentinel appeared; a guarded command RAN")

    if problems:
        for problem in problems:
            print(f"DCG_PROTOCOL_E2E_FAIL {problem}", file=sys.stderr)
        return 1

    print(f"{OK_MARKER} receipt={args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DCG multi-agent protocol e2e harness")
    parser.add_argument("--fixture-root", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--binary", default=None)
    parser.add_argument("--sentinel", default=None)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except HarnessError as exc:
        print(f"DCG_PROTOCOL_E2E_FAIL {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
