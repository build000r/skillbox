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
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .agent_timing import PHASE_ADAPTER_COLLECTION, record_detail, record_phase
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

# Bounded concurrency for adapter collection.
#
# Serial collection made a brain invocation cost the *sum* of every external
# tool budget: a measured ``next`` run spent 7856ms of its 8930ms end-to-end in
# adapter collection, with bv and sbp each burning their full 2.5s timeout back
# to back. The adapters are independent read-only probes, so they can overlap --
# but this box is shared with live agent sessions, so the fan-out is capped
# rather than unbounded. Four covers the expensive probes (br x2, bv, sbp,
# evidence) in effectively one wave while leaving headroom on a loaded host.
DEFAULT_ADAPTER_MAX_WORKERS = 4
ADAPTER_MAX_WORKERS_ENV = "SKILLBOX_ADAPTER_MAX_WORKERS"
MAX_ADAPTER_WORKERS = 8
ADAPTER_THREAD_NAME_PREFIX = "sbx-adapter"

AdapterArgsBuilder = Callable[[Mapping[str, Any]], list[str]]
AdapterParser = Callable[[str], Any]
AdapterCallable = Callable[[], Any]
AdapterTask = tuple[str, AdapterCallable]


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


def _resolve_adapter_max_workers(
    *,
    max_workers: int | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, list[dict[str, Any]]]:
    """Resolve the concurrency cap from an argument, the environment, or the default."""
    warnings: list[dict[str, Any]] = []
    if max_workers is not None:
        raw: Any = max_workers
        source = "argument"
    else:
        merged_env = {**os.environ, **dict(env or {})}
        raw = str(merged_env.get(ADAPTER_MAX_WORKERS_ENV) or "").strip()
        if not raw:
            return DEFAULT_ADAPTER_MAX_WORKERS, "default", warnings
        source = ADAPTER_MAX_WORKERS_ENV

    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        warnings.append(
            _warning(
                "ADAPTER_WORKERS_CONFIG_INVALID",
                f"{source} must be an integer worker count",
                requested_max_workers=str(raw),
                max_workers_source=source,
            )
        )
        return DEFAULT_ADAPTER_MAX_WORKERS, "default", warnings

    if value < 1:
        warnings.append(
            _warning(
                "ADAPTER_WORKERS_CONFIG_INVALID",
                f"{source} must be at least 1",
                requested_max_workers=value,
                max_workers_source=source,
            )
        )
        return 1, source, warnings
    if value > MAX_ADAPTER_WORKERS:
        warnings.append(
            _warning(
                "ADAPTER_WORKERS_CAPPED",
                f"adapter concurrency capped at {MAX_ADAPTER_WORKERS}",
                requested_max_workers=value,
                cap=MAX_ADAPTER_WORKERS,
                max_workers_source=source,
            )
        )
        return MAX_ADAPTER_WORKERS, source, warnings
    return value, source, warnings


class _ConcurrencyGauge:
    """Records the peak number of adapters in flight at once.

    This is the observable that makes "bounded" a fact rather than a claim: the
    proof and the tests read ``peak`` instead of trusting the executor.
    """

    __slots__ = ("_lock", "_active", "peak")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    @contextmanager
    def track(self) -> Iterator[None]:
        with self._lock:
            self._active += 1
            if self._active > self.peak:
                self.peak = self._active
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1


def _degraded_adapter_payload(
    name: str,
    *,
    code: str,
    message: str,
    started_at: float,
) -> dict[str, Any]:
    return {
        "source": str(name),
        "kind": "in_process",
        "ok": False,
        "status": "unavailable",
        "duration_ms": _duration_ms(started_at),
        "warnings": [_warning(code, message)],
    }


def _call_adapter(name: str, fn: AdapterCallable, gauge: _ConcurrencyGauge) -> dict[str, Any]:
    """Run one adapter callable so no failure can escape into its siblings.

    Per-adapter timeouts are enforced inside ``run_adapter`` by the subprocess
    itself, so a timeout consumes only its own worker: the other adapters keep
    running and the collection still ends at the slowest one.
    """
    started_at = time.monotonic()
    with gauge.track():
        try:
            payload = fn()
        except Exception as exc:  # noqa: BLE001 - one adapter must never fail the set
            return _degraded_adapter_payload(
                name,
                code="ADAPTER_CALL_FAILED",
                message=str(exc),
                started_at=started_at,
            )
    if not isinstance(payload, dict):
        return _degraded_adapter_payload(
            name,
            code="ADAPTER_RESULT_INVALID",
            message=f"{name} adapter returned {type(payload).__name__}, expected a mapping",
            started_at=started_at,
        )
    return payload


def _future_payload(name: str, future: "Future[dict[str, Any]]", started_at: float) -> dict[str, Any]:
    try:
        return future.result()
    except Exception as exc:  # noqa: BLE001 - dispatch failures degrade like any other adapter
        return _degraded_adapter_payload(
            name,
            code="ADAPTER_DISPATCH_FAILED",
            message=str(exc),
            started_at=started_at,
        )


def collect_adapters_bounded(
    tasks: Sequence[AdapterTask] | Iterable[AdapterTask],
    *,
    max_workers: int | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run independent adapter callables under a bounded thread pool.

    Contract:

    * **Bounded.** At most ``max_workers`` adapters are ever in flight; the
      observed peak is reported back so callers can verify it.
    * **Deterministic.** Results are keyed in *declared* order, never completion
      order, so payload and golden stability survive parallelism.
    * **Isolated.** An exception, a timeout, or a nonsense return value degrades
      exactly one adapter entry and never the collection.
    * **Unchanged when trivial.** Zero or one adapter runs inline on the calling
      thread with no pool at all.
    """
    ordered: list[AdapterTask] = [(str(name), fn) for name, fn in tasks]
    workers, workers_source, warnings = _resolve_adapter_max_workers(
        max_workers=max_workers,
        env=env,
    )
    effective = max(1, min(workers, len(ordered))) if ordered else 1
    gauge = _ConcurrencyGauge()
    started_at = time.monotonic()
    results: dict[str, Any] = {}

    if len(ordered) <= 1 or effective <= 1:
        mode = "serial"
        for name, fn in ordered:
            results[name] = _call_adapter(name, fn, gauge)
    else:
        mode = "parallel"
        futures: dict[str, Future[dict[str, Any]]] = {}
        with ThreadPoolExecutor(
            max_workers=effective,
            thread_name_prefix=ADAPTER_THREAD_NAME_PREFIX,
        ) as pool:
            for name, fn in ordered:
                futures[name] = pool.submit(_call_adapter, name, fn, gauge)
        # Exiting the pool joined every worker, so nothing is still running and
        # nothing was abandoned. Rebuild by declared name, not completion order.
        for name, _fn in ordered:
            results[name] = _future_payload(name, futures[name], started_at)

    concurrency = {
        "mode": mode,
        "adapter_count": len(ordered),
        "max_workers": effective,
        "requested_max_workers": workers,
        "max_workers_source": workers_source,
        "peak_in_flight": gauge.peak,
        "warnings": warnings,
    }
    return results, concurrency


def adapter_timing_summary(
    adapters: Mapping[str, Any],
    *,
    collection_ms: float | None = None,
    concurrency: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize per-adapter durations and degraded statuses.

    Per-adapter ``duration_ms`` stays untouched on each adapter payload; this is
    a roll-up so a caller can see *which* adapter ate the wall clock without
    walking every packet. ``collection_ms`` is the real collection wall time.
    Under bounded parallel collection it tracks the *slowest* adapter rather
    than ``sum_adapter_ms``.

    ``concurrency`` is optional and only appears when supplied, so the key set
    of a bare summary stays pinned by the golden contract. When it is supplied
    it gains ``parallel_speedup`` -- ``sum_adapter_ms / collection_ms`` -- the
    single field that makes a silent regression back to serial collection
    visible.
    """
    durations: dict[str, float] = {}
    statuses: dict[str, str] = {}
    unavailable: list[str] = []
    timeouts: list[str] = []
    for name, adapter in (adapters or {}).items():
        if not isinstance(adapter, dict):
            continue
        raw_duration = adapter.get("duration_ms", adapter.get("elapsed_ms"))
        if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool):
            durations[str(name)] = round(float(raw_duration), 3)
        status = str(adapter.get("status") or ("ok" if adapter.get("ok") else "unknown"))
        statuses[str(name)] = status
        if status == "timeout":
            timeouts.append(str(name))
        elif status == "unavailable":
            unavailable.append(str(name))

    slowest: dict[str, Any] | None = None
    if durations:
        slowest_name = max(durations, key=lambda key: durations[key])
        slowest = {"name": slowest_name, "duration_ms": durations[slowest_name]}

    sum_adapter_ms = round(sum(durations.values()), 3)
    summary: dict[str, Any] = {
        "adapter_count": len(statuses),
        "collection_ms": round(float(collection_ms), 3) if collection_ms is not None else None,
        "sum_adapter_ms": sum_adapter_ms,
        "durations_ms": durations,
        "statuses": statuses,
        "slowest": slowest,
        "timeouts": sorted(timeouts),
        "unavailable": sorted(unavailable),
    }
    if concurrency is not None:
        block = dict(concurrency)
        wall = summary["collection_ms"]
        if isinstance(wall, (int, float)) and wall > 0:
            block["parallel_speedup"] = round(sum_adapter_ms / float(wall), 3)
        summary["concurrency"] = block
    return summary


def collect_agent_adapter_evidence(
    root_dir: Path,
    *,
    model: dict[str, Any] | None = None,
    cwd: str | None = None,
    ntm_session: str | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Collect bounded adapter evidence for graph/next consumers.

    Every adapter below is an independent read-only probe: ``br``/``bv``/``sbp``
    each shell out to their own binary, ``pulse`` reads a state file, and
    ``evidence`` composes read-only in-process surfaces. None consumes another's
    result and none mutates shared state, so they are collected concurrently
    under a bounded pool. The declared order here is also the emitted order.
    """
    started_at = time.monotonic()
    tasks: list[AdapterTask] = [
        ("br_ready", lambda: br_ready_adapter(root_dir)),
        ("br_open", lambda: br_list_adapter(root_dir, status="open")),
        ("bv_triage", lambda: bv_triage_adapter(root_dir)),
        ("sbp_skills", lambda: sbp_skills_adapter(root_dir)),
        ("pulse", lambda: pulse_state_adapter(root_dir)),
    ]
    if ntm_session:
        tasks.append(("ntm_activity", lambda: ntm_activity_adapter(ntm_session, root_dir=root_dir)))
    if model is not None:
        tasks.append(("evidence", lambda: runtime_evidence_adapter(root_dir, model, cwd=cwd)))

    adapters, concurrency = collect_adapters_bounded(tasks, max_workers=max_workers)
    warnings = [
        warning
        for adapter in adapters.values()
        for warning in (adapter.get("warnings") or [])
    ]
    warnings.extend(concurrency.get("warnings") or [])
    collection_ms = float(_duration_ms(started_at))
    timing = adapter_timing_summary(
        adapters,
        collection_ms=collection_ms,
        concurrency=concurrency,
    )
    # Feed the invocation recorder so brain payloads can report adapter wall
    # time next to compute time instead of hiding it.
    record_phase(PHASE_ADAPTER_COLLECTION, collection_ms)
    record_detail("adapters", timing)
    return {
        "ok": all(bool(adapter.get("ok")) for adapter in adapters.values()),
        "adapters": adapters,
        "warnings": warnings,
        "timing": timing,
    }


__all__ = [
    "AdapterResult",
    "AdapterSpec",
    "DEFAULT_TIMEOUTS",
    "ADAPTER_TIMEOUT_ENV",
    "ADAPTER_MAX_WORKERS_ENV",
    "DEFAULT_ADAPTER_MAX_WORKERS",
    "MAX_ADAPTER_TIMEOUT_SECONDS",
    "MAX_ADAPTER_WORKERS",
    "DEFAULT_PULSE_MAX_AGE_SECONDS",
    "adapter_timing_summary",
    "collect_adapters_bounded",
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
