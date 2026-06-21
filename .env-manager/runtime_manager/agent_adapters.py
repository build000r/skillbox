"""Bounded local adapters for the agent operations brain.

These adapters normalize optional local tools into small evidence packets.
They never call the network directly and never turn a missing optional binary
into a hard failure for graph or next-action commands.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from .evidence import collect_runtime_evidence
# Single source of truth for redaction. ``redact_diagnostic_text`` is kept as a
# thin alias to the shared ``redact_text`` because agent_snapshots and the
# adapter tests import this name from here.
from .shared import REDACTION_MARKER, redact_text as redact_diagnostic_text

DEFAULT_TIMEOUTS = {
    "br": 1.5,
    "bv": 2.5,
    "sbp": 2.5,
    "ntm": 1.5,
}
DEFAULT_PULSE_MAX_AGE_SECONDS = 120.0
PREVIEW_LIMIT = 500


def _preview(text: str | None, *, limit: int = PREVIEW_LIMIT) -> str:
    value = redact_diagnostic_text(str(text or "").strip())
    if len(value) <= limit:
        return value
    return value[:limit] + "...[truncated]"


def _duration_ms(started_at: float) -> int:
    return int(round((time.monotonic() - started_at) * 1000))


def _warning(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _command_result(
    *,
    source: str,
    command: list[str],
    status: str,
    ok: bool,
    duration_ms: int,
    exit_code: int | None = None,
    payload: Any = None,
    warnings: list[dict[str, Any]] | None = None,
    stdout_preview: str = "",
    stderr: str = "",
    cwd: Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": source,
        "kind": "command",
        "ok": ok,
        "status": status,
        "command": list(command),
        "duration_ms": duration_ms,
        "warnings": list(warnings or []),
    }
    if cwd is not None:
        result["cwd"] = str(cwd)
    if exit_code is not None:
        result["exit_code"] = exit_code
    if payload is not None:
        result["payload"] = payload
    if stdout_preview:
        result["stdout_preview"] = stdout_preview
    if stderr:
        result["stderr"] = stderr
    return result


def run_command_adapter(
    source: str,
    command: list[str],
    *,
    cwd: Path | str | None = None,
    timeout_seconds: float | None = None,
    expected_format: str = "json",
    env: Mapping[str, str] | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    """Run a local optional tool and normalize stdout/stderr into evidence."""
    started_at = time.monotonic()
    cwd_path = Path(cwd).resolve() if cwd is not None else None
    timeout = DEFAULT_TIMEOUTS.get(source, 1.5) if timeout_seconds is None else timeout_seconds
    run_env = None
    if env is not None:
        run_env = {**os.environ, **dict(env)}
    try:
        completed = subprocess_run(
            command,
            cwd=str(cwd_path) if cwd_path is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=run_env,
        )
    except FileNotFoundError as exc:
        return _command_result(
            source=source,
            command=command,
            cwd=cwd_path,
            ok=False,
            status="unavailable",
            duration_ms=_duration_ms(started_at),
            warnings=[
                _warning(
                    "UNAVAILABLE_DEPENDENCY",
                    f"{command[0]} is not available on PATH",
                    detail=str(exc),
                )
            ],
        )
    except subprocess.TimeoutExpired as exc:
        return _command_result(
            source=source,
            command=command,
            cwd=cwd_path,
            ok=False,
            status="timeout",
            duration_ms=_duration_ms(started_at),
            stdout_preview=_preview(getattr(exc, "stdout", "")),
            stderr=_preview(getattr(exc, "stderr", "")),
            warnings=[
                _warning(
                    "ADAPTER_TIMEOUT",
                    f"{source} adapter timed out after {timeout:g}s",
                    timeout_seconds=timeout,
                )
            ],
        )
    except OSError as exc:
        return _command_result(
            source=source,
            command=command,
            cwd=cwd_path,
            ok=False,
            status="unavailable",
            duration_ms=_duration_ms(started_at),
            warnings=[_warning("UNAVAILABLE_DEPENDENCY", str(exc))],
        )

    warnings: list[dict[str, Any]] = []
    stderr = _preview(completed.stderr)
    stdout = str(completed.stdout or "")
    parsed_payload: Any = None
    stdout_preview = ""
    parse_failed = False
    if expected_format == "json":
        try:
            parsed_payload = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError as exc:
            parse_failed = True
            stdout_preview = _preview(stdout)
            warnings.append(_warning("MALFORMED_JSON", str(exc)))
    elif expected_format == "toon":
        parse_failed = True
        stdout_preview = _preview(stdout)
        warnings.append(_warning("MALFORMED_TOON", "TOON parsing is not available in the stdlib adapter"))
    else:
        parsed_payload = stdout

    if completed.returncode != 0:
        warnings.append(
            _warning(
                "ADAPTER_NONZERO_EXIT",
                f"{source} exited with code {completed.returncode}",
                exit_code=completed.returncode,
            )
        )
    ok = completed.returncode == 0 and not parse_failed
    status = "ok" if ok else "degraded"
    return _command_result(
        source=source,
        command=command,
        cwd=cwd_path,
        ok=ok,
        status=status,
        exit_code=completed.returncode,
        duration_ms=_duration_ms(started_at),
        payload=parsed_payload if not parse_failed else None,
        warnings=warnings,
        stdout_preview=stdout_preview,
        stderr=stderr,
    )


def br_ready_adapter(
    root_dir: Path,
    *,
    timeout_seconds: float | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    return run_command_adapter(
        "br",
        ["br", "ready", "--json"],
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def br_list_adapter(
    root_dir: Path,
    *,
    status: str = "open",
    timeout_seconds: float | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    return run_command_adapter(
        "br",
        ["br", "list", f"--status={status}", "--json"],
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def br_show_adapter(
    root_dir: Path,
    issue_id: str,
    *,
    timeout_seconds: float | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    return run_command_adapter(
        "br",
        ["br", "show", issue_id, "--json"],
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def bv_triage_adapter(
    root_dir: Path,
    *,
    timeout_seconds: float | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    return run_command_adapter(
        "bv",
        ["bv", "--robot-triage", "--format", "json"],
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def sbp_skills_adapter(
    root_dir: Path,
    *,
    timeout_seconds: float | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    return run_command_adapter(
        "sbp",
        ["sbp", "skills", "--issues-only", "--format", "json"],
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def ntm_activity_adapter(
    session: str,
    *,
    root_dir: Path | None = None,
    timeout_seconds: float | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    return run_command_adapter(
        "ntm",
        ["ntm", "activity", session, "--json"],
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def runtime_evidence_adapter(
    root_dir: Path,
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    declared_servers: list[str] | None = None,
) -> dict[str, Any]:
    started_at = time.monotonic()
    try:
        payload = collect_runtime_evidence(
            root_dir,
            model,
            cwd=cwd,
            declared_servers=declared_servers,
        )
    except Exception as exc:
        return {
            "source": "evidence",
            "kind": "in_process",
            "ok": False,
            "status": "degraded",
            "duration_ms": _duration_ms(started_at),
            "warnings": [_warning("EVIDENCE_COLLECTION_FAILED", str(exc))],
        }
    return {
        "source": "evidence",
        "kind": "in_process",
        "ok": True,
        "status": "ok",
        "duration_ms": _duration_ms(started_at),
        "warnings": [],
        "payload": payload,
    }


def _pulse_state_candidates(root_dir: Path) -> list[Path]:
    return [
        root_dir / ".skillbox-state" / "logs" / "runtime" / "pulse.state.json",
        root_dir / "logs" / "runtime" / "pulse.state.json",
    ]


def pulse_state_adapter(
    root_dir: Path,
    *,
    now: float | None = None,
    max_age_seconds: float = DEFAULT_PULSE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    started_at = time.monotonic()
    state_path = next((path for path in _pulse_state_candidates(root_dir) if path.is_file()), None)
    if state_path is None:
        return {
            "source": "pulse",
            "kind": "file",
            "ok": False,
            "status": "unavailable",
            "duration_ms": _duration_ms(started_at),
            "path": None,
            "warnings": [_warning("PULSE_STATE_MISSING", "pulse state file was not found")],
        }
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "source": "pulse",
            "kind": "file",
            "ok": False,
            "status": "degraded",
            "duration_ms": _duration_ms(started_at),
            "path": str(state_path),
            "warnings": [_warning("PULSE_STATE_UNREADABLE", str(exc))],
        }

    warnings: list[dict[str, Any]] = []
    updated_at = payload.get("updated_at")
    observed_now = time.time() if now is None else now
    age_seconds = None
    if isinstance(updated_at, (int, float)):
        age_seconds = round(max(0.0, observed_now - float(updated_at)), 3)
        if age_seconds > max_age_seconds:
            warnings.append(
                _warning(
                    "STALE_PULSE_STATE",
                    f"pulse state is older than {max_age_seconds:g}s",
                    age_seconds=age_seconds,
                )
            )
    else:
        warnings.append(_warning("PULSE_STATE_MISSING_TIMESTAMP", "pulse state has no numeric updated_at"))
    return {
        "source": "pulse",
        "kind": "file",
        "ok": not warnings,
        "status": "ok" if not warnings else "degraded",
        "duration_ms": _duration_ms(started_at),
        "path": str(state_path),
        "age_seconds": age_seconds,
        "warnings": warnings,
        "payload": payload,
    }


def collect_agent_adapter_evidence(
    root_dir: Path,
    *,
    model: dict[str, Any] | None = None,
    cwd: str | None = None,
    ntm_session: str | None = None,
) -> dict[str, Any]:
    """Collect bounded adapter evidence for graph/next consumers."""
    adapters: dict[str, Any] = {
        "br_ready": br_ready_adapter(root_dir),
        "br_open": br_list_adapter(root_dir, status="open"),
        "bv_triage": bv_triage_adapter(root_dir),
        "sbp_skills": sbp_skills_adapter(root_dir),
        "pulse": pulse_state_adapter(root_dir),
    }
    if ntm_session:
        adapters["ntm_activity"] = ntm_activity_adapter(ntm_session, root_dir=root_dir)
    if model is not None:
        adapters["evidence"] = runtime_evidence_adapter(root_dir, model, cwd=cwd)
    warnings = [
        warning
        for adapter in adapters.values()
        for warning in (adapter.get("warnings") or [])
    ]
    return {
        "ok": all(bool(adapter.get("ok")) for adapter in adapters.values()),
        "adapters": adapters,
        "warnings": warnings,
    }


__all__ = [
    "DEFAULT_TIMEOUTS",
    "DEFAULT_PULSE_MAX_AGE_SECONDS",
    "redact_diagnostic_text",
    "run_command_adapter",
    "br_ready_adapter",
    "br_list_adapter",
    "br_show_adapter",
    "bv_triage_adapter",
    "sbp_skills_adapter",
    "ntm_activity_adapter",
    "runtime_evidence_adapter",
    "pulse_state_adapter",
    "collect_agent_adapter_evidence",
]
