"""Bounded local adapters for the agent operations brain.

These adapters normalize optional local tools into small evidence packets.
They never call the network directly and never turn a missing optional binary
into a hard failure for graph or next-action commands.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .evidence import collect_runtime_evidence

try:
    # Single source of truth for redaction. ``redact_diagnostic_text`` is kept as
    # a thin alias because agent_snapshots and tests import this name from here.
    from .shared import REDACTION_MARKER as REDACTION_MARKER
    from .shared import redact_text as redact_diagnostic_text
except Exception:  # pragma: no cover - fallback only matters if shared import is broken.
    REDACTION_MARKER = "[REDACTED]"

    def redact_diagnostic_text(text: str) -> str:
        pattern = re.compile(
            r"(?i)(authorization:\s*bearer\s+|token=|password=|api[_-]?key=)([^\s]+)"
        )
        return pattern.sub(lambda match: f"{match.group(1)}{REDACTION_MARKER}", str(text))

DEFAULT_TIMEOUTS = {
    "br": 1.5,
    "bv": 2.5,
    "sbp": 2.5,
    "ntm": 1.5,
}
ADAPTER_TIMEOUT_ENV = "SKILLBOX_ADAPTER_TIMEOUT"
MAX_ADAPTER_TIMEOUT_SECONDS = 30.0
DEFAULT_PULSE_MAX_AGE_SECONDS = 120.0
PREVIEW_LIMIT = 500
AdapterArgsBuilder = Callable[[Mapping[str, Any]], list[str]]
AdapterParser = Callable[[str], Any]


@dataclass(frozen=True)
class AdapterSpec:
    name: str
    binary: str
    args_builder: AdapterArgsBuilder
    timeout_default: float
    parse: AdapterParser


@dataclass(frozen=True)
class AdapterResult:
    status: str
    payload: Any
    raw_excerpt: str
    elapsed_ms: int
    source_command: list[str]
    timeout_seconds: float
    timeout_source: str
    warnings: list[dict[str, Any]]
    source: str
    kind: str = "command"
    cwd: Path | None = None
    exit_code: int | None = None
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source": self.source,
            "kind": self.kind,
            "ok": self.ok,
            "status": self.status,
            "command": list(self.source_command),
            "source_command": list(self.source_command),
            "duration_ms": self.elapsed_ms,
            "elapsed_ms": self.elapsed_ms,
            "timeout_seconds": self.timeout_seconds,
            "timeout_source": self.timeout_source,
            "warnings": list(self.warnings),
        }
        if self.cwd is not None:
            result["cwd"] = str(self.cwd)
        if self.exit_code is not None:
            result["exit_code"] = self.exit_code
        if self.payload is not None:
            result["payload"] = self.payload
        if self.raw_excerpt:
            result["raw_excerpt"] = self.raw_excerpt
            result["stdout_preview"] = self.raw_excerpt
        if self.stderr:
            result["stderr"] = self.stderr
        return result


class AdapterParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


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


def _adapter_env_suffix(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in str(name).upper())


def _float_from_env(raw: str, *, label: str) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if value <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return value


def _cap_timeout(value: float, *, source: str, warnings: list[dict[str, Any]]) -> float:
    if value > MAX_ADAPTER_TIMEOUT_SECONDS:
        warnings.append(
            _warning(
                "ADAPTER_TIMEOUT_CAPPED",
                f"adapter timeout capped at {MAX_ADAPTER_TIMEOUT_SECONDS:g}s",
                requested_timeout_seconds=value,
                cap_seconds=MAX_ADAPTER_TIMEOUT_SECONDS,
                timeout_source=source,
            )
        )
        return MAX_ADAPTER_TIMEOUT_SECONDS
    return value


def _resolve_adapter_timeout(
    spec: AdapterSpec,
    *,
    timeout_seconds: float | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[float, str, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    merged_env = {**os.environ, **dict(env or {})}

    if timeout_seconds is not None:
        try:
            timeout = _float_from_env(str(timeout_seconds), label="timeout_seconds")
        except ValueError as exc:
            warnings.append(_warning("ADAPTER_TIMEOUT_CONFIG_INVALID", str(exc), timeout_source="argument"))
            timeout = float(spec.timeout_default)
        return _cap_timeout(timeout, source="argument", warnings=warnings), "argument", warnings

    specific_name = f"{ADAPTER_TIMEOUT_ENV}_{_adapter_env_suffix(spec.name)}"
    raw_specific = str(merged_env.get(specific_name) or "").strip()
    if raw_specific:
        try:
            timeout = _float_from_env(raw_specific, label=specific_name)
            return _cap_timeout(timeout, source=specific_name, warnings=warnings), specific_name, warnings
        except ValueError as exc:
            warnings.append(_warning("ADAPTER_TIMEOUT_CONFIG_INVALID", str(exc), timeout_source=specific_name))

    raw_global = str(merged_env.get(ADAPTER_TIMEOUT_ENV) or "").strip()
    if raw_global:
        try:
            multiplier = _float_from_env(raw_global, label=ADAPTER_TIMEOUT_ENV)
            timeout = float(spec.timeout_default) * multiplier
            return _cap_timeout(timeout, source=ADAPTER_TIMEOUT_ENV, warnings=warnings), ADAPTER_TIMEOUT_ENV, warnings
        except ValueError as exc:
            warnings.append(_warning("ADAPTER_TIMEOUT_CONFIG_INVALID", str(exc), timeout_source=ADAPTER_TIMEOUT_ENV))

    timeout = _cap_timeout(float(spec.timeout_default), source="default", warnings=warnings)
    return timeout, "default", warnings


def _parse_json(stdout: str) -> Any:
    return json.loads(stdout) if stdout.strip() else {}


def _parse_text(stdout: str) -> str:
    return stdout


def _parse_toon(_stdout: str) -> Any:
    raise AdapterParseError("MALFORMED_TOON", "TOON parsing is not available in the stdlib adapter")


def _parser_for_format(expected_format: str) -> AdapterParser:
    if expected_format == "json":
        return _parse_json
    if expected_format == "toon":
        return _parse_toon
    return _parse_text


def _adapter_result(
    spec: AdapterSpec,
    *,
    command: list[str],
    status: str,
    started_at: float,
    timeout_seconds: float,
    timeout_source: str,
    warnings: list[dict[str, Any]] | None = None,
    payload: Any = None,
    raw_excerpt: str = "",
    stderr: str = "",
    cwd: Path | None = None,
    exit_code: int | None = None,
) -> AdapterResult:
    return AdapterResult(
        status=status,
        payload=payload,
        raw_excerpt=raw_excerpt,
        elapsed_ms=_duration_ms(started_at),
        source_command=list(command),
        timeout_seconds=timeout_seconds,
        timeout_source=timeout_source,
        warnings=list(warnings or []),
        source=spec.name,
        cwd=cwd,
        exit_code=exit_code,
        stderr=stderr,
    )


def run_adapter(
    spec: AdapterSpec,
    *,
    context: Mapping[str, Any] | None = None,
    cwd: Path | str | None = None,
    timeout_seconds: float | None = None,
    env: Mapping[str, str] | None = None,
    subprocess_run: Any | None = None,
) -> AdapterResult:
    """Run one declarative command adapter and return a bounded result."""
    started_at = time.monotonic()
    cwd_path = Path(cwd).resolve() if cwd is not None else None
    timeout, timeout_source, timeout_warnings = _resolve_adapter_timeout(
        spec,
        timeout_seconds=timeout_seconds,
        env=env,
    )
    command = [str(spec.binary)]
    try:
        command.extend(str(arg) for arg in spec.args_builder(context or {}))
    except Exception as exc:
        return _adapter_result(
            spec,
            command=command,
            status="unavailable",
            started_at=started_at,
            timeout_seconds=timeout,
            timeout_source=timeout_source,
            warnings=[
                *timeout_warnings,
                _warning("ADAPTER_ARGS_FAILED", str(exc), next_actions=["Fix adapter argument builder."]),
            ],
            cwd=cwd_path,
        )

    runner = subprocess.run if subprocess_run is None else subprocess_run
    run_env = None
    if env is not None:
        run_env = {**os.environ, **dict(env)}
    try:
        completed = runner(
            command,
            cwd=str(cwd_path) if cwd_path is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=run_env,
        )
    except FileNotFoundError as exc:
        return _adapter_result(
            spec,
            command=command,
            status="unavailable",
            started_at=started_at,
            timeout_seconds=timeout,
            timeout_source=timeout_source,
            warnings=[
                *timeout_warnings,
                _warning(
                    "UNAVAILABLE_DEPENDENCY",
                    f"{command[0]} is not available on PATH",
                    detail=str(exc),
                    next_actions=[f"Install {command[0]} or skip {spec.name} adapter evidence."],
                ),
            ],
            cwd=cwd_path,
        )
    except subprocess.TimeoutExpired as exc:
        return _adapter_result(
            spec,
            command=command,
            status="timeout",
            started_at=started_at,
            timeout_seconds=timeout,
            timeout_source=timeout_source,
            raw_excerpt=_preview(getattr(exc, "stdout", "") or getattr(exc, "output", "")),
            stderr=_preview(getattr(exc, "stderr", "")),
            warnings=[
                *timeout_warnings,
                _warning(
                    "ADAPTER_TIMEOUT",
                    f"{spec.name} adapter timed out after {timeout:g}s",
                    timeout_seconds=timeout,
                    next_actions=[
                        f"Raise {ADAPTER_TIMEOUT_ENV}_{_adapter_env_suffix(spec.name)} or inspect {command[0]} latency."
                    ],
                ),
            ],
            cwd=cwd_path,
        )
    except OSError as exc:
        return _adapter_result(
            spec,
            command=command,
            status="unavailable",
            started_at=started_at,
            timeout_seconds=timeout,
            timeout_source=timeout_source,
            warnings=[*timeout_warnings, _warning("UNAVAILABLE_DEPENDENCY", str(exc))],
            cwd=cwd_path,
        )
    except Exception as exc:
        return _adapter_result(
            spec,
            command=command,
            status="unavailable",
            started_at=started_at,
            timeout_seconds=timeout,
            timeout_source=timeout_source,
            warnings=[*timeout_warnings, _warning("ADAPTER_RUN_FAILED", str(exc))],
            cwd=cwd_path,
        )

    stderr = _preview(completed.stderr)
    stdout = str(completed.stdout or "")
    warnings = list(timeout_warnings)
    payload: Any = None
    raw_excerpt = ""
    status = "ok"

    try:
        payload = spec.parse(stdout)
    except AdapterParseError as exc:
        status = "parse_error"
        raw_excerpt = _preview(stdout)
        warnings.append(_warning(exc.code, str(exc)))
    except json.JSONDecodeError as exc:
        status = "parse_error"
        raw_excerpt = _preview(stdout)
        warnings.append(_warning("MALFORMED_JSON", str(exc)))
    except Exception as exc:
        status = "parse_error"
        raw_excerpt = _preview(stdout)
        warnings.append(_warning("ADAPTER_PARSE_ERROR", str(exc)))

    if completed.returncode != 0:
        warnings.append(
            _warning(
                "ADAPTER_NONZERO_EXIT",
                f"{spec.name} exited with code {completed.returncode}",
                exit_code=completed.returncode,
                next_actions=[f"Run {' '.join(command)} directly and inspect stderr."],
            )
        )
        if status == "ok":
            status = "nonzero_exit"

    return _adapter_result(
        spec,
        command=command,
        status=status,
        exit_code=completed.returncode,
        started_at=started_at,
        timeout_seconds=timeout,
        timeout_source=timeout_source,
        payload=payload if status != "parse_error" else None,
        raw_excerpt=raw_excerpt,
        warnings=warnings,
        stderr=stderr,
        cwd=cwd_path,
    )


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
    spec = AdapterSpec(
        name=source,
        binary=str(command[0]),
        args_builder=lambda _context: list(command[1:]),
        timeout_default=DEFAULT_TIMEOUTS.get(source, 1.5),
        parse=_parser_for_format(expected_format),
    )
    return run_adapter(
        spec,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        env=env,
        subprocess_run=subprocess_run,
    ).to_payload()


def _json_adapter_spec(name: str, binary: str, args_builder: AdapterArgsBuilder) -> AdapterSpec:
    return AdapterSpec(
        name=name,
        binary=binary,
        args_builder=args_builder,
        timeout_default=DEFAULT_TIMEOUTS.get(name, 1.5),
        parse=_parse_json,
    )


def _run_tool_adapter(
    spec: AdapterSpec,
    *,
    context: Mapping[str, Any] | None = None,
    cwd: Path | str | None = None,
    timeout_seconds: float | None = None,
    subprocess_run: Any | None = None,
) -> dict[str, Any]:
    return run_adapter(
        spec,
        context=context,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    ).to_payload()


def br_ready_adapter(
    root_dir: Path,
    *,
    timeout_seconds: float | None = None,
    subprocess_run: Any | None = None,
) -> dict[str, Any]:
    return _run_tool_adapter(
        _json_adapter_spec("br", "br", lambda _context: ["ready", "--json"]),
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def br_list_adapter(
    root_dir: Path,
    *,
    status: str = "open",
    timeout_seconds: float | None = None,
    subprocess_run: Any | None = None,
) -> dict[str, Any]:
    return _run_tool_adapter(
        _json_adapter_spec("br", "br", lambda context: ["list", f"--status={context['status']}", "--json"]),
        context={"status": status},
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def br_show_adapter(
    root_dir: Path,
    issue_id: str,
    *,
    timeout_seconds: float | None = None,
    subprocess_run: Any | None = None,
) -> dict[str, Any]:
    return _run_tool_adapter(
        _json_adapter_spec("br", "br", lambda context: ["show", str(context["issue_id"]), "--json"]),
        context={"issue_id": issue_id},
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def bv_triage_adapter(
    root_dir: Path,
    *,
    timeout_seconds: float | None = None,
    subprocess_run: Any | None = None,
) -> dict[str, Any]:
    return _run_tool_adapter(
        _json_adapter_spec("bv", "bv", lambda _context: ["--robot-triage", "--format", "json"]),
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def sbp_skills_adapter(
    root_dir: Path,
    *,
    timeout_seconds: float | None = None,
    subprocess_run: Any | None = None,
) -> dict[str, Any]:
    return _run_tool_adapter(
        _json_adapter_spec("sbp", "sbp", lambda _context: ["skills", "--issues-only", "--format", "json"]),
        cwd=root_dir,
        timeout_seconds=timeout_seconds,
        subprocess_run=subprocess_run,
    )


def ntm_activity_adapter(
    session: str,
    *,
    root_dir: Path | None = None,
    timeout_seconds: float | None = None,
    subprocess_run: Any | None = None,
) -> dict[str, Any]:
    return _run_tool_adapter(
        _json_adapter_spec("ntm", "ntm", lambda context: ["activity", str(context["session"]), "--json"]),
        context={"session": session},
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
    "AdapterResult",
    "AdapterSpec",
    "DEFAULT_TIMEOUTS",
    "ADAPTER_TIMEOUT_ENV",
    "MAX_ADAPTER_TIMEOUT_SECONDS",
    "DEFAULT_PULSE_MAX_AGE_SECONDS",
    "redact_diagnostic_text",
    "run_adapter",
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
