"""Timing helpers for agent-facing JSON payloads.

Two layers live here:

``elapsed_ms``/``attach_elapsed``
    The original *compute* stopwatch. It measures one in-process payload
    function and nothing else. It is deliberately unchanged so existing
    payload builders keep emitting ``meta.elapsed_ms``.

``InvocationTiming``/``attach_component_timing``
    Component profiling for a whole CLI invocation. ``meta.elapsed_ms`` alone
    is a trap: a real ``next`` run measured 9.28s of wall clock while reporting
    ``elapsed_ms`` of 8.953 because interpreter startup, runtime-model build and
    bounded adapter subprocesses all happen *outside* the payload function.
    The invocation recorder collects those phases once, as they happen, so the
    payload can report ``end_to_end_ms``, ``startup_ms``, ``model_ms``,
    ``adapter_collection_ms`` and ``compute_ms`` side by side.

Nothing here re-runs work to obtain a number: every phase is timed in place by
the code that already performs it exactly once.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

# Phase names are shared vocabulary between the CLI, the adapters and the
# standalone latency proof. Keep them stable: they are asserted by goldens.
PHASE_STARTUP = "startup_ms"
PHASE_MODEL = "model_ms"
PHASE_ADAPTER_COLLECTION = "adapter_collection_ms"
PHASE_COMPUTE = "compute_ms"
COMPONENT_META_KEYS = (
    "elapsed_ms",
    "compute_ms",
    "end_to_end_ms",
    "startup_ms",
    "model_ms",
    "adapter_collection_ms",
)


def _process_start_offset_seconds() -> tuple[float, str]:
    """Return seconds already burned by this process before this import.

    Linux exposes process start time in ``/proc/self/stat`` (field 22, in clock
    ticks since boot) which, against ``/proc/uptime``, gives interpreter boot +
    import cost that a ``perf_counter`` taken at import time cannot see. Two
    tiny file reads, once per process. Anywhere else we honestly report that the
    baseline is module import.
    """
    try:
        with open("/proc/self/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
        # The comm field can contain spaces/parens; everything after the last
        # ')' is positionally stable.
        tail = raw[raw.rindex(")") + 2 :].split()
        start_ticks = float(tail[19])
        hertz = float(os.sysconf("SC_CLK_TCK"))
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            uptime = float(handle.read().split()[0])
        offset = uptime - (start_ticks / hertz)
        if offset < 0 or offset > 86400.0:
            return 0.0, "module_import"
        return offset, "proc_self_stat"
    except Exception:
        return 0.0, "module_import"


_MODULE_IMPORT_PERF = time.perf_counter()
_PRE_IMPORT_SECONDS, PROCESS_START_SOURCE = _process_start_offset_seconds()


def timer_start() -> float:
    """Return a monotonic start marker for elapsed payload metadata."""
    return time.perf_counter()


def elapsed_ms(start: float) -> float:
    """Return elapsed milliseconds rounded for compact JSON output."""
    return round((time.perf_counter() - start) * 1000.0, 3)


def attach_elapsed(payload: dict[str, Any], start: float) -> dict[str, Any]:
    """Attach ``meta.elapsed_ms`` to a JSON payload and return it."""
    meta = _meta_of(payload)
    meta["elapsed_ms"] = elapsed_ms(start)
    return payload


def process_elapsed_ms() -> float:
    """Return milliseconds since this process started, best effort.

    On Linux this includes interpreter startup and module imports; elsewhere it
    is measured from the moment this module was imported and
    ``PROCESS_START_SOURCE`` says so.
    """
    seconds = (time.perf_counter() - _MODULE_IMPORT_PERF) + _PRE_IMPORT_SECONDS
    return round(max(0.0, seconds) * 1000.0, 3)


def _meta_of(payload: dict[str, Any]) -> dict[str, Any]:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        payload["meta"] = meta
    return meta


class InvocationTiming:
    """Accumulates named component phases for a single CLI invocation.

    Phases accumulate additively so a surface that collects adapters twice
    reports the true total rather than the last slice. ``details`` carries
    non-scalar context (per-adapter durations, statuses) without polluting the
    numeric phase map.
    """

    __slots__ = ("_phases", "_details", "_started_at")

    def __init__(self) -> None:
        self._phases: dict[str, float] = {}
        self._details: dict[str, Any] = {}
        self._started_at: float = time.perf_counter()

    @property
    def started_at(self) -> float:
        return self._started_at

    def elapsed_since_start_ms(self) -> float:
        return round(max(0.0, time.perf_counter() - self._started_at) * 1000.0, 3)

    def record(self, name: str, duration_ms: float) -> None:
        """Add ``duration_ms`` to the phase called ``name``."""
        try:
            value = float(duration_ms)
        except (TypeError, ValueError):
            return
        if value < 0:
            value = 0.0
        self._phases[name] = round(self._phases.get(name, 0.0) + value, 3)

    def set_detail(self, name: str, value: Any) -> None:
        self._details[name] = value

    def get(self, name: str) -> float | None:
        return self._phases.get(name)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time a block of work exactly once and record it under ``name``."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, (time.perf_counter() - started) * 1000.0)

    def phases(self) -> dict[str, float]:
        return dict(self._phases)

    def details(self) -> dict[str, Any]:
        return dict(self._details)

    def reset(self) -> None:
        self._phases.clear()
        self._details.clear()
        self._started_at = time.perf_counter()


_CURRENT = InvocationTiming()
_INVOCATION_COUNT = 0


def current_invocation() -> InvocationTiming:
    """Return the process-wide invocation recorder."""
    return _CURRENT


def invocation_index() -> int:
    """How many invocations this process has dispatched (0 before the first)."""
    return _INVOCATION_COUNT


def reset_invocation() -> InvocationTiming:
    """Start a new invocation: clear phases and re-baseline the clock."""
    global _INVOCATION_COUNT
    _INVOCATION_COUNT += 1
    _CURRENT.reset()
    return _CURRENT


def invocation_startup_ms() -> float:
    """Startup cost attributable to *this* invocation.

    A one-shot CLI process pays interpreter boot plus module import once, and
    that whole slice belongs to its single invocation. The MCP server instead
    calls ``cli.main()`` repeatedly inside one long-lived process: charging the
    second call with the server's entire process age would be a lie an order of
    magnitude larger than the work. Warm invocations pay no startup, and
    ``meta.timing.invocation_index`` says which case you are reading.
    """
    return process_elapsed_ms() if _INVOCATION_COUNT <= 1 else 0.0


def record_phase(name: str, duration_ms: float) -> None:
    _CURRENT.record(name, duration_ms)


def record_detail(name: str, value: Any) -> None:
    _CURRENT.set_detail(name, value)


@contextmanager
def phase(name: str) -> Iterator[None]:
    with _CURRENT.phase(name):
        yield


def attach_component_timing(
    payload: dict[str, Any],
    *,
    invocation: InvocationTiming | None = None,
    compute_ms: float | None = None,
    end_to_end_ms: float | None = None,
) -> dict[str, Any]:
    """Attach the component breakdown to ``payload['meta']`` and return it.

    ``compute_ms`` defaults to the ``elapsed_ms`` the payload builder already
    recorded, so the in-process stopwatch keeps its meaning and gains an honest
    name. Phases that were never recorded are omitted rather than reported as
    zero -- ``--no-adapters`` genuinely has no adapter collection cost, and a
    fabricated ``0.0`` would read as "adapters are free".
    """
    if not isinstance(payload, dict):
        return payload
    recorder = invocation if invocation is not None else _CURRENT
    meta = _meta_of(payload)

    if compute_ms is None:
        existing = meta.get("elapsed_ms")
        if isinstance(existing, (int, float)) and not isinstance(existing, bool):
            compute_ms = float(existing)
    if compute_ms is not None:
        meta["compute_ms"] = round(float(compute_ms), 3)
        meta.setdefault("elapsed_ms", meta["compute_ms"])

    for name in (PHASE_STARTUP, PHASE_MODEL, PHASE_ADAPTER_COLLECTION):
        value = recorder.get(name)
        if value is not None:
            meta[name] = value

    if end_to_end_ms is None:
        # Startup happened before the invocation clock was baselined, so it is
        # added rather than double counted.
        end_to_end_ms = (recorder.get(PHASE_STARTUP) or 0.0) + recorder.elapsed_since_start_ms()
    meta["end_to_end_ms"] = round(float(end_to_end_ms), 3)

    timing: dict[str, Any] = {
        "process_start_source": PROCESS_START_SOURCE,
        "invocation_index": _INVOCATION_COUNT,
        "phases": recorder.phases(),
    }
    details = recorder.details()
    if details:
        timing.update(details)
    meta["timing"] = timing
    return payload


def component_meta_keys(meta: dict[str, Any] | None) -> list[str]:
    """Return the component timing keys actually present on ``meta``."""
    if not isinstance(meta, dict):
        return []
    return [key for key in COMPONENT_META_KEYS if key in meta]


__all__ = [
    "COMPONENT_META_KEYS",
    "InvocationTiming",
    "PHASE_ADAPTER_COLLECTION",
    "PHASE_COMPUTE",
    "PHASE_MODEL",
    "PHASE_STARTUP",
    "PROCESS_START_SOURCE",
    "attach_component_timing",
    "attach_elapsed",
    "component_meta_keys",
    "current_invocation",
    "elapsed_ms",
    "invocation_index",
    "invocation_startup_ms",
    "phase",
    "process_elapsed_ms",
    "record_detail",
    "record_phase",
    "reset_invocation",
    "timer_start",
]
