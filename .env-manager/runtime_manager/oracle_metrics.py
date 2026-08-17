"""Latency and reliability metrics for the Oracle subagent.

The Oracle lane handles prompts, private research URLs, browser profiles,
cookies, and account identity.  None of that may reach an operator dashboard,
a log line, or a persisted metrics document.  This module therefore does not
*redact* metrics — it makes a leak structurally impossible:

* every field is either a bounded integer, a bool, or a token drawn from a
  closed vocabulary declared in this file;
* there is no free-text field, no caller/account field, no URL field, and no
  path field anywhere in the contract, so there is nothing to sanitize;
* the only variable-shaped strings are an opaque CSPRNG run ID, a SHA-256
  result digest, and a rendered UTC timestamp, each pinned to an exact regex;
* every emitted document is re-walked by :func:`assert_emission_safe` before it
  leaves the module.  A key or string that is not on the allowlist raises
  :class:`OracleMetricsError` instead of being published.

That last check is deliberately redundant with construction.  It converts "we
believe the schema is closed" into an enforced invariant, so a future field
added without a vocabulary entry fails closed rather than shipping whatever the
caller passed in.

Deliberate non-goals
--------------------
No caller ID, tenant, or session identifier is recorded.  Per-caller
reliability would require exactly the account identity this lane must not
expose, and a salted digest of it would still be a stable correlation handle.
Runs are correlated by their own opaque ``run_id`` only.

Usage sketch::

    timer = PhaseTimer()
    timer.start(PHASE_TOTAL)
    ...
    sample = OracleRunSample(
        run_id=new_run_id(),
        mode="standard",
        state=STATE_COMPLETED,
        stage_reached=STAGE_DELIVERED,
        error_class=ERROR_NONE,
        warm=True,
        attempts=1,
        queue_depth=0,
        inflight=1,
        result_bytes=4096,
        result_digest=digest,
        observed_at_ms=observed_at_ms,
        durations_ms=timer.durations(),
    )
    registry.record(sample)
    payload = registry.render(generated_at_ms=now_ms)
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

ORACLE_METRICS_SAMPLE_SCHEMA = "skillbox.oracle-metrics-sample.v1"
ORACLE_METRICS_SNAPSHOT_SCHEMA = "skillbox.oracle-metrics-snapshot.v1"

SUPPORTED_MODES = ("standard", "deep-research")

STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_DENIED = "denied"
STATE_TIMED_OUT = "timed_out"
STATE_CANCELLED = "cancelled"

#: Terminal run states.  A sample is only ever recorded for a finished run;
#: live progress is carried by the queue gauges instead.
RUN_STATES = (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_DENIED,
    STATE_TIMED_OUT,
    STATE_CANCELLED,
)

STAGE_QUEUED = "queued"
STAGE_ADMITTED = "admitted"
STAGE_STAGED = "staged"
STAGE_BROWSER_READY = "browser_ready"
STAGE_SUBMITTED = "submitted"
STAGE_GENERATING = "generating"
STAGE_DELIVERED = "delivered"

#: How far through the lane the run got, in order.  This is the "where did it
#: die" signal that makes a failure count actionable without any run content.
RUN_STAGES = (
    STAGE_QUEUED,
    STAGE_ADMITTED,
    STAGE_STAGED,
    STAGE_BROWSER_READY,
    STAGE_SUBMITTED,
    STAGE_GENERATING,
    STAGE_DELIVERED,
)

ERROR_NONE = "none"

#: Closed error vocabulary.  Classes, never messages: an exception string could
#: quote a URL, a prompt fragment, or a filesystem path.
ERROR_CLASSES = (
    ERROR_NONE,
    "policy_denied",
    "quota_exceeded",
    "attachment_rejected",
    "browser_unavailable",
    "browser_crashed",
    "auth_expired",
    "navigation_failed",
    "submit_failed",
    "response_timeout",
    "result_empty",
    "transport_error",
    "client_cancelled",
    "internal_error",
)

PHASE_QUEUE_WAIT = "queue_wait"
PHASE_ADMISSION = "admission"
PHASE_BROWSER_ACQUIRE = "browser_acquire"
PHASE_ATTACHMENT_STAGE = "attachment_stage"
PHASE_SUBMIT = "submit"
PHASE_FIRST_OUTPUT = "first_output"
PHASE_GENERATION = "generation"
PHASE_RESULT_WRITE = "result_write"
PHASE_TOTAL = "total"

LATENCY_PHASES = (
    PHASE_QUEUE_WAIT,
    PHASE_ADMISSION,
    PHASE_BROWSER_ACQUIRE,
    PHASE_ATTACHMENT_STAGE,
    PHASE_SUBMIT,
    PHASE_FIRST_OUTPUT,
    PHASE_GENERATION,
    PHASE_RESULT_WRITE,
    PHASE_TOTAL,
)

#: Which error classes each terminal state may carry.  Exhaustive and disjoint;
#: verified at import so the vocabulary cannot drift out from under the tests.
_STATE_ERROR_CLASSES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        STATE_COMPLETED: frozenset({ERROR_NONE}),
        STATE_FAILED: frozenset(
            {
                "browser_unavailable",
                "browser_crashed",
                "auth_expired",
                "navigation_failed",
                "submit_failed",
                "result_empty",
                "transport_error",
                "internal_error",
            }
        ),
        STATE_DENIED: frozenset(
            {"policy_denied", "quota_exceeded", "attachment_rejected"}
        ),
        STATE_TIMED_OUT: frozenset({"response_timeout"}),
        STATE_CANCELLED: frozenset({"client_cancelled"}),
    }
)

#: Which stages each terminal state may report having reached.
_STATE_STAGES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        STATE_COMPLETED: frozenset({STAGE_DELIVERED}),
        STATE_FAILED: frozenset(RUN_STAGES),
        STATE_DENIED: frozenset({STAGE_QUEUED, STAGE_ADMITTED}),
        STATE_TIMED_OUT: frozenset({STAGE_SUBMITTED, STAGE_GENERATING}),
        STATE_CANCELLED: frozenset(RUN_STAGES) - {STAGE_DELIVERED},
    }
)

MAX_DURATION_MS = 24 * 60 * 60 * 1000
MAX_ATTEMPTS = 16
MAX_QUEUE_DEPTH = 4096
MAX_INFLIGHT = 64
MAX_RESULT_BYTES = 1024 * 1024 * 1024
MAX_WINDOW_CAPACITY = 100_000
DEFAULT_WINDOW_CAPACITY = 512

#: 2020-01-01T00:00:00Z .. 2100-01-01T00:00:00Z, in milliseconds.
MIN_EPOCH_MS = 1_577_836_800_000
MAX_EPOCH_MS = 4_102_444_800_000

_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)

_SAMPLE_KEYS = frozenset(
    {
        "schema",
        "run_id",
        "mode",
        "state",
        "stage_reached",
        "error_class",
        "warm",
        "attempts",
        "queue_depth",
        "inflight",
        "result_bytes",
        "result_digest",
        "observed_at_ms",
        "durations_ms",
    }
)

_BOOL_KEYS = frozenset({"warm"})

#: Every structural key that may appear in an emitted document.  Enumerated
#: vocabulary tokens are also legal keys (they index the by-state, by-mode,
#: by-stage, by-error-class, and per-phase maps).
_STRUCTURAL_KEYS = _SAMPLE_KEYS | frozenset(
    {
        "generated_at",
        "window",
        "samples",
        "capacity",
        "oldest_at",
        "newest_at",
        "gauges",
        "queue_depth_max",
        "observed_at",
        "runs",
        "total",
        "cold",
        "by_state",
        "by_mode",
        "by_stage_reached",
        "reliability",
        "success_rate_ppm",
        "attempts_total",
        "retried_runs",
        "by_error_class",
        "latency_ms",
        "count",
        "min",
        "p50",
        "p95",
        "max",
    }
)

_VOCABULARY_TOKENS = frozenset(
    RUN_STATES + RUN_STAGES + ERROR_CLASSES + LATENCY_PHASES + SUPPORTED_MODES
)
_SCHEMA_TOKENS = frozenset(
    {ORACLE_METRICS_SAMPLE_SCHEMA, ORACLE_METRICS_SNAPSHOT_SCHEMA}
)
_ALLOWED_KEYS = _STRUCTURAL_KEYS | _VOCABULARY_TOKENS
_ALLOWED_STRINGS = _VOCABULARY_TOKENS | _SCHEMA_TOKENS

_MAX_EMISSION_DEPTH = 8


class OracleMetricsError(RuntimeError):
    """Stable, non-sensitive metrics rejection.

    The message is a constant.  Only ``code`` varies, and it is drawn from a
    small set of labels declared in this module, so raising can never echo a
    prompt, URL, path, or credential back to the caller.
    """

    def __init__(self, code: str) -> None:
        super().__init__("oracle metrics: rejected")
        self.code = code


def _deny(code: str) -> None:
    raise OracleMetricsError(code)


def _bounded_integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    # ``type(value) is not int`` rather than isinstance: bool is an int
    # subclass, and a stray True must not be accepted as 1.
    if type(value) is not int or not minimum <= value <= maximum:
        _deny(code)
    return value


def _token(value: Any, allowed: tuple[str, ...], code: str) -> str:
    if type(value) is not str or value not in allowed:
        _deny(code)
    return value


def _verify_vocabulary() -> None:
    """Fail at import if the closed vocabulary stopped being closed."""
    if frozenset(_STATE_ERROR_CLASSES) != frozenset(RUN_STATES):
        _deny("vocabulary_invalid")
    if frozenset(_STATE_STAGES) != frozenset(RUN_STATES):
        _deny("vocabulary_invalid")
    seen: set[str] = set()
    for classes in _STATE_ERROR_CLASSES.values():
        if seen & classes:
            _deny("vocabulary_invalid")
        seen |= classes
    if seen != frozenset(ERROR_CLASSES):
        _deny("vocabulary_invalid")
    for stages in _STATE_STAGES.values():
        if not stages or not stages <= frozenset(RUN_STAGES):
            _deny("vocabulary_invalid")
    if len(frozenset(LATENCY_PHASES)) != len(LATENCY_PHASES):
        _deny("vocabulary_invalid")
    if not _VOCABULARY_TOKENS.isdisjoint(_SCHEMA_TOKENS):
        _deny("vocabulary_invalid")


_verify_vocabulary()


def new_run_id() -> str:
    """Return an opaque 32-hex run ID from validated CSPRNG output.

    The ID is generated here rather than derived from any request attribute, so
    it cannot carry prompt, caller, or URL entropy.
    """
    try:
        value = secrets.token_hex(16)
    except Exception:
        _deny("run_id_unavailable")
    if type(value) is not str or _RUN_ID_PATTERN.fullmatch(value) is None:
        _deny("run_id_unavailable")
    return value


def iso_millis(epoch_ms: Any) -> str:
    """Render bounded epoch milliseconds as ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    value = _bounded_integer(epoch_ms, MIN_EPOCH_MS, MAX_EPOCH_MS, "clock_invalid")
    seconds, millis = divmod(value, 1000)
    parts = time.gmtime(seconds)
    return (
        f"{parts.tm_year:04d}-{parts.tm_mon:02d}-{parts.tm_mday:02d}"
        f"T{parts.tm_hour:02d}:{parts.tm_min:02d}:{parts.tm_sec:02d}"
        f".{millis:03d}Z"
    )


def _validated_durations(value: Any, code: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        _deny(code)
    keys = frozenset(value)
    if not keys <= frozenset(LATENCY_PHASES):
        _deny(code)
    if PHASE_TOTAL not in keys:
        _deny(code)
    durations: dict[str, int] = {}
    for phase in LATENCY_PHASES:
        if phase not in keys:
            continue
        durations[phase] = _bounded_integer(value[phase], 0, MAX_DURATION_MS, code)
    total = durations[PHASE_TOTAL]
    for phase, millis in durations.items():
        # Phases may overlap or be sampled independently, so their sum is not a
        # meaningful bound. A single phase longer than the whole run is not.
        if phase != PHASE_TOTAL and millis > total:
            _deny(code)
    return MappingProxyType(durations)


@dataclass(frozen=True)
class OracleRunSample:
    """One finished Oracle run, reduced to non-sensitive facts.

    Every invariant below is re-checked in :meth:`from_mapping`, so a mutated
    instance handed to a registry cannot weaken them: the registry round-trips
    each sample through its own document before accepting it.
    """

    run_id: str
    mode: str
    state: str
    stage_reached: str
    error_class: str
    warm: bool
    attempts: int
    queue_depth: int
    inflight: int
    result_bytes: int
    result_digest: str | None
    observed_at_ms: int
    durations_ms: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        code = "sample_shape_invalid"
        if type(self.run_id) is not str or _RUN_ID_PATTERN.fullmatch(self.run_id) is None:
            _deny(code)
        _token(self.mode, SUPPORTED_MODES, code)
        state = _token(self.state, RUN_STATES, code)
        stage = _token(self.stage_reached, RUN_STAGES, code)
        error_class = _token(self.error_class, ERROR_CLASSES, code)
        if error_class not in _STATE_ERROR_CLASSES[state]:
            _deny(code)
        if stage not in _STATE_STAGES[state]:
            _deny(code)
        if type(self.warm) is not bool:
            _deny(code)
        _bounded_integer(self.attempts, 1, MAX_ATTEMPTS, code)
        _bounded_integer(self.queue_depth, 0, MAX_QUEUE_DEPTH, code)
        _bounded_integer(self.inflight, 0, MAX_INFLIGHT, code)
        _bounded_integer(self.result_bytes, 0, MAX_RESULT_BYTES, code)
        _bounded_integer(self.observed_at_ms, MIN_EPOCH_MS, MAX_EPOCH_MS, code)
        # Run-bound evidence: a completed run must carry a nonempty result and
        # the digest of the bytes that were actually written. Anything else is
        # a receipt with nothing behind it.
        if state == STATE_COMPLETED:
            if self.result_bytes < 1:
                _deny(code)
            if (
                type(self.result_digest) is not str
                or _DIGEST_PATTERN.fullmatch(self.result_digest) is None
            ):
                _deny(code)
        else:
            if self.result_digest is not None or self.result_bytes != 0:
                _deny(code)
        object.__setattr__(
            self, "durations_ms", _validated_durations(self.durations_ms, code)
        )

    @classmethod
    def from_mapping(cls, value: Any) -> OracleRunSample:
        code = "sample_shape_invalid"
        if not isinstance(value, Mapping) or frozenset(value) != _SAMPLE_KEYS:
            _deny(code)
        if value["schema"] != ORACLE_METRICS_SAMPLE_SCHEMA:
            _deny(code)
        return cls(
            run_id=value["run_id"],
            mode=value["mode"],
            state=value["state"],
            stage_reached=value["stage_reached"],
            error_class=value["error_class"],
            warm=value["warm"],
            attempts=value["attempts"],
            queue_depth=value["queue_depth"],
            inflight=value["inflight"],
            result_bytes=value["result_bytes"],
            result_digest=value["result_digest"],
            observed_at_ms=value["observed_at_ms"],
            durations_ms=value["durations_ms"],
        )

    def as_document(self) -> dict[str, Any]:
        document = {
            "schema": ORACLE_METRICS_SAMPLE_SCHEMA,
            "run_id": self.run_id,
            "mode": self.mode,
            "state": self.state,
            "stage_reached": self.stage_reached,
            "error_class": self.error_class,
            "warm": self.warm,
            "attempts": self.attempts,
            "queue_depth": self.queue_depth,
            "inflight": self.inflight,
            "result_bytes": self.result_bytes,
            "result_digest": self.result_digest,
            "observed_at_ms": self.observed_at_ms,
            "durations_ms": dict(self.durations_ms),
        }
        assert_emission_safe(document)
        return document


class PhaseTimer:
    """Monotonic phase timing.

    Wall-clock deltas can go backwards across NTP steps and would turn a
    latency panel into fiction, so phase durations come from
    :func:`time.monotonic_ns` only.  The clock is injectable for tests.
    """

    def __init__(self, monotonic_ns: Any = time.monotonic_ns) -> None:
        if not callable(monotonic_ns):
            _deny("timer_invalid")
        self._monotonic_ns = monotonic_ns
        self._started: dict[str, int] = {}
        self._durations: dict[str, int] = {}

    def _now_ns(self) -> int:
        value = self._monotonic_ns()
        if type(value) is not int:
            _deny("timer_invalid")
        return value

    def start(self, phase: str) -> None:
        name = _token(phase, LATENCY_PHASES, "timer_invalid")
        if name in self._started or name in self._durations:
            _deny("timer_invalid")
        self._started[name] = self._now_ns()

    def stop(self, phase: str) -> int:
        name = _token(phase, LATENCY_PHASES, "timer_invalid")
        if name not in self._started:
            _deny("timer_invalid")
        elapsed_ns = self._now_ns() - self._started.pop(name)
        if elapsed_ns < 0:
            _deny("timer_invalid")
        millis = elapsed_ns // 1_000_000
        self._durations[name] = _bounded_integer(
            millis, 0, MAX_DURATION_MS, "timer_invalid"
        )
        return self._durations[name]

    def durations(self) -> dict[str, int]:
        """Return the finished phases, ordered by :data:`LATENCY_PHASES`."""
        if self._started:
            _deny("timer_invalid")
        return {
            phase: self._durations[phase]
            for phase in LATENCY_PHASES
            if phase in self._durations
        }


def _percentile(sorted_values: list[int], ppm: int) -> int:
    """Nearest-rank percentile over integers — no float, no interpolation."""
    count = len(sorted_values)
    rank = -((-ppm * count) // 1_000_000)
    if rank < 1:
        rank = 1
    if rank > count:
        rank = count
    return sorted_values[rank - 1]


class OracleMetricsRegistry:
    """Bounded in-memory window of run samples plus live queue gauges.

    The window is a ring buffer so an unbounded run stream cannot grow memory,
    and the rendered snapshot has a fixed shape regardless of traffic: every
    enumerated bucket is present, zero-filled, so an operator view and its
    canonical bytes do not change shape when a rare error class first appears.
    """

    def __init__(self, capacity: int = DEFAULT_WINDOW_CAPACITY) -> None:
        self._capacity = _bounded_integer(
            capacity, 1, MAX_WINDOW_CAPACITY, "capacity_invalid"
        )
        self._samples: list[OracleRunSample] = []
        self._queue_depth = 0
        self._queue_depth_max = 0
        self._inflight = 0
        self._concurrency_capacity = 0
        self._gauge_observed_at_ms: int | None = None

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def record(self, sample: Any) -> OracleRunSample:
        """Accept one finished run.

        The sample is round-tripped through its own document, which re-runs
        every validator.  A subclass, or an instance mutated through
        ``object.__setattr__`` after construction, is rejected here rather than
        silently polluting the window.
        """
        if type(sample) is not OracleRunSample:
            _deny("sample_shape_invalid")
        validated = OracleRunSample.from_mapping(sample.as_document())
        self._samples.append(validated)
        if len(self._samples) > self._capacity:
            del self._samples[: len(self._samples) - self._capacity]
        return validated

    def observe_queue(
        self,
        *,
        queue_depth: int,
        inflight: int,
        capacity: int,
        observed_at_ms: int,
    ) -> None:
        """Record the live queue gauges at a point in time."""
        code = "gauge_invalid"
        depth = _bounded_integer(queue_depth, 0, MAX_QUEUE_DEPTH, code)
        in_flight = _bounded_integer(inflight, 0, MAX_INFLIGHT, code)
        concurrency = _bounded_integer(capacity, 0, MAX_INFLIGHT, code)
        observed = _bounded_integer(observed_at_ms, MIN_EPOCH_MS, MAX_EPOCH_MS, code)
        if in_flight > concurrency:
            _deny(code)
        self._queue_depth = depth
        self._queue_depth_max = max(self._queue_depth_max, depth)
        self._inflight = in_flight
        self._concurrency_capacity = concurrency
        self._gauge_observed_at_ms = observed

    def _latency_document(self) -> dict[str, Any]:
        buckets: dict[str, Any] = {}
        for phase in LATENCY_PHASES:
            values = sorted(
                sample.durations_ms[phase]
                for sample in self._samples
                if phase in sample.durations_ms
            )
            if not values:
                buckets[phase] = {
                    "count": 0,
                    "min": None,
                    "p50": None,
                    "p95": None,
                    "max": None,
                }
                continue
            buckets[phase] = {
                "count": len(values),
                "min": values[0],
                "p50": _percentile(values, 500_000),
                "p95": _percentile(values, 950_000),
                "max": values[-1],
            }
        return buckets

    def snapshot(self, *, generated_at_ms: int) -> dict[str, Any]:
        """Render the operator view.

        ``success_rate_ppm`` is parts per million, floored — an integer so the
        canonical encoding stays byte-stable and no float formatting difference
        can show up as a fake metrics change.
        """
        generated_at = iso_millis(generated_at_ms)
        total = len(self._samples)
        by_state = {state: 0 for state in RUN_STATES}
        by_mode = {mode: 0 for mode in SUPPORTED_MODES}
        by_stage = {stage: 0 for stage in RUN_STAGES}
        by_error = {name: 0 for name in ERROR_CLASSES}
        warm = 0
        attempts_total = 0
        retried = 0
        for sample in self._samples:
            by_state[sample.state] += 1
            by_mode[sample.mode] += 1
            by_stage[sample.stage_reached] += 1
            by_error[sample.error_class] += 1
            warm += 1 if sample.warm else 0
            attempts_total += sample.attempts
            retried += 1 if sample.attempts > 1 else 0
        success_rate_ppm = (
            (by_state[STATE_COMPLETED] * 1_000_000) // total if total else 0
        )
        observed_ms = [sample.observed_at_ms for sample in self._samples]
        document = {
            "schema": ORACLE_METRICS_SNAPSHOT_SCHEMA,
            "generated_at": generated_at,
            "window": {
                "samples": total,
                "capacity": self._capacity,
                "oldest_at": iso_millis(min(observed_ms)) if observed_ms else None,
                "newest_at": iso_millis(max(observed_ms)) if observed_ms else None,
            },
            "gauges": {
                "queue_depth": self._queue_depth,
                "queue_depth_max": self._queue_depth_max,
                "inflight": self._inflight,
                "capacity": self._concurrency_capacity,
                "observed_at": (
                    iso_millis(self._gauge_observed_at_ms)
                    if self._gauge_observed_at_ms is not None
                    else None
                ),
            },
            "runs": {
                "total": total,
                "warm": warm,
                "cold": total - warm,
                "by_state": by_state,
                "by_mode": by_mode,
                "by_stage_reached": by_stage,
            },
            "reliability": {
                "success_rate_ppm": success_rate_ppm,
                "attempts_total": attempts_total,
                "retried_runs": retried,
                "by_error_class": by_error,
            },
            "latency_ms": self._latency_document(),
        }
        assert_emission_safe(document)
        return document

    def render(self, *, generated_at_ms: int) -> bytes:
        """Canonical ASCII bytes for the snapshot, with one trailing newline."""
        return canonical_json_bytes(self.snapshot(generated_at_ms=generated_at_ms))


def canonical_json_bytes(document: Any) -> bytes:
    """Sorted-key, separator-minimized, ASCII-escaped JSON plus one newline.

    Matching ``runtime_manager.oracle_policy``'s persistence discipline keeps
    metrics documents byte-comparable across hosts and Python versions.
    """
    assert_emission_safe(document)
    try:
        payload = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        _deny("emission_unsafe")
    return payload.encode("ascii") + b"\n"


def assert_emission_safe(document: Any, _depth: int = 0) -> None:
    """Fail closed unless every key and string is on the closed allowlist.

    This is the structural redaction proof.  Rather than searching emitted text
    for secret-shaped substrings — which only ever catches known shapes — it
    inverts the test: nothing may appear that was not declared in this module's
    vocabulary, plus an opaque run ID, a SHA-256 digest, or a rendered
    timestamp.  A prompt fragment, URL, home directory, account handle, or
    token matches none of those and cannot be emitted.
    """
    if _depth > _MAX_EMISSION_DEPTH:
        _deny("emission_unsafe")
    if document is None or type(document) is int:
        return
    if type(document) is bool:
        # Bools are legal only in bool-valued positions, checked by the parent.
        _deny("emission_unsafe")
    if type(document) is str:
        if document in _ALLOWED_STRINGS:
            return
        if _RUN_ID_PATTERN.fullmatch(document) is not None:
            return
        if _DIGEST_PATTERN.fullmatch(document) is not None:
            return
        if _TIMESTAMP_PATTERN.fullmatch(document) is not None:
            return
        _deny("emission_unsafe")
    if type(document) is dict:
        for key, value in document.items():
            if type(key) is not str or key not in _ALLOWED_KEYS:
                _deny("emission_unsafe")
            if type(value) is bool:
                if key not in _BOOL_KEYS:
                    _deny("emission_unsafe")
                continue
            assert_emission_safe(value, _depth + 1)
        return
    if type(document) is list:
        for item in document:
            assert_emission_safe(item, _depth + 1)
        return
    _deny("emission_unsafe")
