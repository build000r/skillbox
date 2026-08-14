"""Destructive command guard (DCG) adapter — ONE implementation, two surfaces.

This module is the hoisted core of the adapter that used to live inside
``scripts/operator_mcp_server.py``. It is consumed by BOTH operator surfaces:

* ``scripts/operator_mcp_server.py`` (MCP tool ``operator_box_exec``)
* ``python3 scripts/box.py exec`` (the robot-CLI replacement)

so a policy fix can never land on one surface only, and the two cannot drift
into different allow/deny answers for the same command.

INTERFACE: the supported DCG 0.6.7 robot surface,
    dcg test --robot --format json --no-color -- <command>
which STATICALLY evaluates ``<command>`` against the enabled packs and prints a
single JSON object. Exit 0 = allow, 1 = deny, 3/4/5 = config/parse/IO error.

SAFETY INVARIANT (this module's whole reason to exist): the command under
inspection is passed as ONE argv element to ``dcg test``. It is never handed to
a shell, never ``input``-piped into an interpreter, and ``dcg test`` itself does
not execute it. Nothing on this path can run the payload.

FAIL CLOSED: every failure mode — no version pin, no binary, spawn failure,
timeout, non-JSON output, wrong schema, wrong version, unrecognized decision —
resolves to ``verdict="unavailable"``, and :func:`dcg_blocks_execution` treats
anything that is not an explicit ``allow`` as a block. There is no "no verdict"
outcome that a caller could mistake for permission.

CALLER-INJECTED DEPENDENCIES: :func:`evaluate_command` takes its binary
resolver, subprocess runner, redactor, and version normalizer as parameters
rather than reading module globals. That is deliberate: each surface keeps
those names in ITS OWN module namespace, so surface tests that patch
``MODULE.run_checked`` / ``MODULE._dcg_binary_path`` / ``MODULE.DCG_PINNED_VERSION``
keep working against the shared implementation.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

DCG_BINARY_NAME = "dcg"
DCG_BINARY_ENV = "SKILLBOX_DCG_BIN"
DCG_EVAL_TIMEOUT_SECONDS = 10
DCG_ROBOT_SCHEMA_VERSION = 1
DCG_INTERFACE = "dcg test --robot --format json"

#: Decision strings understood by this adapter. Anything else is an
#: "unsupported_response" and fails closed.
DCG_ALLOW_DECISIONS = frozenset({"allow", "warn"})
DCG_DENY_DECISIONS = frozenset({"deny", "block"})


def resolve_dcg_binary() -> str:
    """Resolve the pinned DCG binary, or "" when it cannot be found.

    Order: explicit ``SKILLBOX_DCG_BIN`` override, then ``PATH``, then the
    default install target ``~/.local/bin/dcg`` used by the distribution
    contract. Returning "" is a fail-closed signal, never a skip.
    """
    override = str(os.environ.get(DCG_BINARY_ENV) or "").strip()
    if override:
        return override if Path(override).is_file() else ""
    found = shutil.which(DCG_BINARY_NAME)
    if found:
        return found
    default_target = Path.home() / ".local" / "bin" / DCG_BINARY_NAME
    return str(default_target) if default_target.is_file() else ""


def load_pinned_version(env_manager_dir: Path) -> tuple[str, str, Callable[[str], str]]:
    """Load the DCG version pin from its SINGLE source of truth.

    The pin lives in ``.env-manager/runtime_manager/dcg_distribution.py``.
    Consumers must never re-declare a version string. The import is guarded so
    a missing/broken runtime_manager cannot take an operator surface down at
    import time — but it is NOT a fallback: with no pin we cannot prove the
    binary is compatible, so the caller stores the empty version and the
    adapter treats it as "incompatible" and FAILS CLOSED.

    Returns ``(pinned_version, pin_import_error, normalize_version)``.
    """
    if env_manager_dir.is_dir() and str(env_manager_dir) not in sys.path:
        sys.path.insert(0, str(env_manager_dir))
    try:
        from runtime_manager.dcg_distribution import (  # noqa: PLC0415
            DCG_VERSION,
            normalize_version,
        )
    except Exception as exc:  # noqa: BLE001 - any import failure fails closed
        pin_error = f"{type(exc).__name__}: {exc}"

        def _unavailable(_text: str) -> str:
            raise RuntimeError(pin_error)

        return "", pin_error, _unavailable
    return DCG_VERSION, "", normalize_version


def dcg_argv(binary: str, command: str) -> list[str]:
    """Build the `dcg test` argv. The payload is EXACTLY ONE trailing element."""
    return [binary, "test", "--robot", "--format", "json", "--no-color", "--", command]


def dcg_result(
    verdict: str,
    reason_code: str,
    reason: str,
    *,
    expected_version: str,
    interface: str = DCG_INTERFACE,
    **extra: Any,
) -> dict[str, Any]:
    """Build the adapter's stable verdict record.

    ``verdict`` is one of ``allow`` / ``deny`` / ``unavailable``. Callers must
    never infer "no opinion" from this: :func:`dcg_blocks_execution` maps
    anything that is not ``allow`` to a block.
    """
    record: dict[str, Any] = {
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": reason,
        "available": verdict in {"allow", "deny"},
        "fail_closed": verdict == "unavailable",
        "interface": interface,
        "expected_version": expected_version or "<pin unavailable>",
    }
    record.update(extra)
    return record


def dcg_blocks_execution(verdict: dict[str, Any] | None) -> bool:
    """True unless DCG explicitly allowed the command.

    ``None``, ``unavailable``, and ``deny`` all block. This is the single
    predicate every authoritative call site uses, so "silently no verdict"
    is not expressible.
    """
    if not isinstance(verdict, dict):
        return True
    return verdict.get("verdict") != "allow"


def evaluate_command(
    command: str,
    *,
    timeout: int,
    pinned_version: str,
    pin_import_error: str,
    resolve_binary: Callable[[], str],
    run_command: Callable[..., dict[str, Any]],
    redact: Callable[[str], str],
    normalize_version: Callable[[str], str],
    interface: str = DCG_INTERFACE,
    schema_version: int = DCG_ROBOT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Statically evaluate *command* with the pinned DCG binary.

    NEVER executes *command*: it is passed as a single argv element to
    ``dcg test``, which only pattern-matches it against the enabled packs.

    Always returns a verdict record. Every failure mode returns
    ``verdict="unavailable"``, which :func:`dcg_blocks_execution` blocks on.
    """

    def _result(verdict: str, reason_code: str, reason: str, **extra: Any) -> dict[str, Any]:
        return dcg_result(
            verdict,
            reason_code,
            reason,
            expected_version=pinned_version,
            interface=interface,
            **extra,
        )

    if not pinned_version:
        return _result(
            "unavailable",
            "pin_unavailable",
            (
                "the DCG version pin (.env-manager/runtime_manager/"
                f"dcg_distribution.py) could not be loaded: {pin_import_error}"
            ),
        )

    dcg_bin = resolve_binary()
    if not dcg_bin:
        return _result(
            "unavailable",
            "binary_missing",
            (
                f"the pinned DCG {pinned_version} binary is not installed "
                f"(looked at ${DCG_BINARY_ENV}, PATH, and ~/.local/bin/dcg)"
            ),
        )

    # redact=False so redaction cannot corrupt the JSON we are about to parse;
    # every string we surface below is redacted explicitly instead.
    result = run_command(dcg_argv(dcg_bin, command), timeout=timeout, redact=False)
    error_code = str(result.get("error_code") or "")
    if error_code == "TIMEOUT":
        return _result(
            "unavailable",
            "timeout",
            f"DCG did not answer within {timeout}s",
            binary=dcg_bin,
        )
    if error_code:
        return _result(
            "unavailable",
            "invocation_failed",
            (
                f"could not run the pinned DCG binary ({error_code}): "
                + redact(str(result.get("stderr_redacted") or ""))[:200]
            ),
            binary=dcg_bin,
        )

    raw_stdout = str(result.get("stdout") or "").strip()
    try:
        report = json.loads(raw_stdout)
    except (json.JSONDecodeError, ValueError):
        return _result(
            "unavailable",
            "malformed_output",
            (
                "DCG did not return parseable JSON: "
                + (redact(raw_stdout)[:200] or "<empty stdout>")
            ),
            binary=dcg_bin,
            exit_code=result.get("rc"),
        )
    if not isinstance(report, dict):
        return _result(
            "unavailable",
            "malformed_output",
            f"DCG returned a JSON {type(report).__name__}, expected an object",
            binary=dcg_bin,
            exit_code=result.get("rc"),
        )

    reported_schema = report.get("schema_version")
    if reported_schema != schema_version:
        return _result(
            "unavailable",
            "incompatible_version",
            (
                f"DCG robot schema_version {reported_schema!r} is not the "
                f"supported {schema_version}"
            ),
            binary=dcg_bin,
            exit_code=result.get("rc"),
        )

    reported_raw = str(report.get("dcg_version") or "")
    try:
        reported_version = normalize_version(reported_raw)
    except Exception as exc:  # noqa: BLE001 - unparseable version fails closed
        return _result(
            "unavailable",
            "incompatible_version",
            f"could not read a version out of DCG's response: {exc}",
            binary=dcg_bin,
            dcg_version=reported_raw,
        )
    if reported_version != pinned_version:
        return _result(
            "unavailable",
            "incompatible_version",
            (
                f"DCG reports {reported_version}, but the repo pin is "
                f"{pinned_version}"
            ),
            binary=dcg_bin,
            dcg_version=reported_version,
        )

    decision = str(report.get("decision") or "").strip().lower()
    common: dict[str, Any] = {
        "binary": dcg_bin,
        "dcg_version": reported_version,
        "decision": decision,
        "exit_code": result.get("rc"),
    }
    if decision in DCG_DENY_DECISIONS:
        return _result(
            "deny",
            "guard_denied",
            redact(str(report.get("reason") or "DCG denied this command")),
            rule_id=report.get("rule_id") or "",
            pack_id=report.get("pack_id") or "",
            severity=report.get("severity") or "",
            **common,
        )
    if decision in DCG_ALLOW_DECISIONS:
        return _result(
            "allow",
            "guard_allowed",
            f"DCG {reported_version} decision={decision}",
            warned=decision == "warn",
            **common,
        )
    return _result(
        "unavailable",
        "unsupported_response",
        f"DCG returned an unrecognized decision {decision or '<missing>'!r}",
        **common,
    )


def annotate_advisory(verdict: dict[str, Any], *, site: str) -> dict[str, Any]:
    """Mark *verdict* as coming from an explicitly NON-AUTHORITATIVE site.

    The record is the authoritative one plus ``authoritative: False`` and the
    site name, so an operator reading a preview can see that an unavailable
    guard did not block *this* step — and will block the real run.
    """
    verdict["authoritative"] = False
    verdict["site"] = site
    verdict["blocks_execution_here"] = False
    verdict["blocks_real_run"] = dcg_blocks_execution(verdict)
    return verdict


def dcg_denial(surface: str, verdict: dict[str, Any]) -> dict[str, Any]:
    """Shared refusal shaping for an authoritative DCG block. Nothing ran.

    *surface* names the caller ("operator_box_exec", "box.py exec"). The
    returned dict is surface-agnostic (``error_type``/``message``/
    ``recoverable``/``next_actions``); each caller wraps it in its own
    envelope, so the two surfaces refuse for the same reason with the same
    remediation.
    """
    fail_closed = bool(verdict.get("fail_closed"))
    if fail_closed:
        message = (
            f"{surface} refused to run this command because the "
            f"destructive command guard could not render a verdict: {verdict['reason']}. "
            "The guard is authoritative on the execution path, so an unavailable "
            "guard denies rather than allows."
        )
        next_actions = [
            "python3 .env-manager/manage.py sync --profile core",
            "python3 -m runtime_manager.dcg_distribution --binary ~/.local/bin/dcg",
        ]
    else:
        message = (
            f"{surface} refused to run this command: the destructive "
            f"command guard denied it ({verdict.get('rule_id') or 'unknown rule'}). "
            f"{verdict['reason']}"
        )
        next_actions = [
            "Ask the user to run this command manually if it is genuinely required.",
            f"Re-issue {surface} with a narrower, non-destructive command.",
        ]
    return {
        "error_type": "dcg_unavailable" if fail_closed else "dcg_denied",
        "message": message,
        "recoverable": fail_closed,
        "next_actions": next_actions,
    }
