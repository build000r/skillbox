"""Phase-level latency diagnosis for the Oracle warm-submit SLO.

The 2026-08-06 benchmark on the Oracle host measured 20/20 successful sequential
warm submissions through one persistent Chrome PID. Nearest-rank warm
browser-to-submit p95 was 9614.34ms against a 4000ms target — but the number
that actually diagnoses it is the **minimum**: 4206.74ms. Every observed warm
run missed. There is no fast case.

That distinction is the reason this module exists. A p95 miss with a healthy p50
is a variance problem: contention, a retry, a slow tail. A miss whose *floor*
sits above target is an unconditional cost paid by every run — a fixed sleep, a
synchronous probe, a settle delay — and the two call for opposite work. Reading
a single p95 number cannot tell them apart, so :func:`diagnose_span` classifies
the shape of the miss explicitly instead of leaving an operator to eyeball it.

The second half is attribution. A span is a sum of phases from
:mod:`runtime_manager.oracle_metrics`, so given per-phase durations this module
reports each phase's p95, its share of the span's p95, and — when the operator
declares budgets — its overrun. Ranked, so the tuning scope starts at the phase
that owns the excess rather than at the first one someone suspects.

Deliberately **not** in this module: any tuning. The bead scopes tuning to a
separately authorized change, and a diagnosis that quietly edits the path it
measures is not a diagnosis.

Privacy is inherited by construction rather than by filtering. Input is
durations, counts, and tokens — no prompt, no URL, no account, no sentinel.
:func:`assert_diagnosis_safe` inverts the usual test the same way
``oracle_metrics.assert_emission_safe`` does: nothing may appear in an emitted
document that was not declared in this module's closed vocabulary. That module's
vocabulary is left untouched — this one carries its own, and the two compose.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .oracle_metrics import (
    LATENCY_PHASES,
    PHASE_ADMISSION,
    PHASE_ATTACHMENT_STAGE,
    PHASE_BROWSER_ACQUIRE,
    PHASE_QUEUE_WAIT,
    PHASE_SUBMIT,
    STATE_COMPLETED,
)

ORACLE_LATENCY_DIAGNOSIS_SCHEMA = "skillbox.oracle-latency-diagnosis.v1"

#: The two spans the SLO benchmark reports. Composed from the metrics phase
#: vocabulary so a span is always the sum of phases that module already times.
SPAN_BROWSER_TO_SUBMIT = "browser_to_submit"
SPAN_CLI_TO_SUBMIT = "cli_to_submit"
SPAN_NAMES = (SPAN_BROWSER_TO_SUBMIT, SPAN_CLI_TO_SUBMIT)

VERDICT_PASS = "within_slo"
VERDICT_FAIL = "slo_missed"
VERDICTS = (VERDICT_PASS, VERDICT_FAIL)

#: How a miss is shaped. The distinction drives what is worth tuning.
SHAPE_WITHIN_SLO = "within_slo"
SHAPE_FLOOR_EXCEEDED = "floor_exceeded"
SHAPE_MEDIAN_EXCEEDED = "median_exceeded"
SHAPE_TAIL_ONLY = "tail_only"
SHAPES = (
    SHAPE_WITHIN_SLO,
    SHAPE_FLOOR_EXCEEDED,
    SHAPE_MEDIAN_EXCEEDED,
    SHAPE_TAIL_ONLY,
)

NOTE_NO_SAMPLES = "no_samples"
NOTE_PHASES_MISSING = "phase_durations_missing"
NOTE_PHASES_PARTIAL = "phase_durations_partial"
NOTE_NO_BUDGETS = "phase_budgets_undeclared"
NOTE_SPAN_UNDERCOUNTED = "span_less_than_phase_sum"
NOTES = (
    NOTE_NO_SAMPLES,
    NOTE_PHASES_MISSING,
    NOTE_PHASES_PARTIAL,
    NOTE_NO_BUDGETS,
    NOTE_SPAN_UNDERCOUNTED,
)

P50_PPM = 500_000
P95_PPM = 950_000

MAX_SAMPLES = 100_000
MAX_DURATION_MS = 24 * 60 * 60 * 1000

REFUSAL_CODES = frozenset(
    {
        "contract_invalid",
        "diagnosis_unsafe",
        "sample_invalid",
    }
)


class OracleLatencyError(RuntimeError):
    """Stable, non-sensitive diagnosis refusal."""

    def __init__(self, code: str) -> None:
        super().__init__("oracle latency: refused")
        self.code = code


def _refuse(code: str) -> Any:
    raise OracleLatencyError(code)


def _duration(value: Any, code: str) -> float:
    """A finite, non-negative, bounded millisecond duration.

    Accepts int or float: the benchmark artifact records fractional
    milliseconds while :mod:`oracle_metrics` records integers, and both must
    diagnose identically.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(code)
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > MAX_DURATION_MS:
        _refuse(code)
    return numeric


def nearest_rank(values: Sequence[float], ppm: int) -> float:
    """Nearest-rank percentile — no interpolation, so the result is a real run.

    Identical arithmetic to ``oracle_metrics._percentile``, kept here so a
    rerun of the host benchmark and this diagnosis cannot disagree about which
    observation p95 names. For n=20, p95 is the 19th smallest.
    """

    if not values:
        _refuse("sample_invalid")
    ordered = sorted(values)
    count = len(ordered)
    rank = -((-ppm * count) // 1_000_000)
    if rank < 1:
        rank = 1
    if rank > count:
        rank = count
    return ordered[rank - 1]


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpanContract:
    """A measured span, its phase composition, and its target.

    ``phase_budgets_ms`` is optional on purpose. Share-based attribution needs
    no budget and is the output that actually points at a culprit; inventing a
    per-phase split to fill the table would be fake precision. Declare budgets
    when the operator has real ones.
    """

    name: str
    phases: tuple[str, ...]
    target_ms: float
    warm: bool
    phase_budgets_ms: Mapping[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.name not in SPAN_NAMES:
            _refuse("contract_invalid")
        if not isinstance(self.phases, tuple) or not self.phases:
            _refuse("contract_invalid")
        seen: set[str] = set()
        for phase in self.phases:
            if phase not in LATENCY_PHASES or phase in seen:
                _refuse("contract_invalid")
            seen.add(phase)
        if _duration(self.target_ms, "contract_invalid") <= 0:
            _refuse("contract_invalid")
        if type(self.warm) is not bool:
            _refuse("contract_invalid")
        budgets = self.phase_budgets_ms
        if budgets is None:
            object.__setattr__(self, "phase_budgets_ms", {})
            return
        if not isinstance(budgets, Mapping):
            _refuse("contract_invalid")
        normalized: dict[str, float] = {}
        for phase, budget in budgets.items():
            if phase not in seen:
                _refuse("contract_invalid")
            normalized[phase] = _duration(budget, "contract_invalid")
        if normalized and sum(normalized.values()) > self.target_ms:
            # Budgets that cannot fit inside the target would make every run
            # look compliant against a target it can never meet.
            _refuse("contract_invalid")
        object.__setattr__(self, "phase_budgets_ms", normalized)


#: Warm browser-to-submit: the span the 2026-08-06 benchmark failed.
WARM_BROWSER_TO_SUBMIT = SpanContract(
    name=SPAN_BROWSER_TO_SUBMIT,
    phases=(PHASE_BROWSER_ACQUIRE, PHASE_ATTACHMENT_STAGE, PHASE_SUBMIT),
    target_ms=4000.0,
    warm=True,
)

#: Cold CLI-to-submit: the span that passed at 7273.89ms <= 12000ms.
COLD_CLI_TO_SUBMIT = SpanContract(
    name=SPAN_CLI_TO_SUBMIT,
    phases=(
        PHASE_QUEUE_WAIT,
        PHASE_ADMISSION,
        PHASE_BROWSER_ACQUIRE,
        PHASE_ATTACHMENT_STAGE,
        PHASE_SUBMIT,
    ),
    target_ms=12000.0,
    warm=False,
)


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpanObservation:
    """One run's span total, and its phase breakdown when instrumented."""

    span_ms: float
    warm: bool
    state: str = STATE_COMPLETED
    phase_ms: Mapping[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        _duration(self.span_ms, "sample_invalid")
        if type(self.warm) is not bool:
            _refuse("sample_invalid")
        if type(self.state) is not str or not self.state:
            _refuse("sample_invalid")
        phases = self.phase_ms
        if phases is None:
            object.__setattr__(self, "phase_ms", {})
            return
        if not isinstance(phases, Mapping):
            _refuse("sample_invalid")
        normalized: dict[str, float] = {}
        for phase, value in phases.items():
            if phase not in LATENCY_PHASES:
                _refuse("sample_invalid")
            normalized[phase] = _duration(value, "sample_invalid")
        object.__setattr__(self, "phase_ms", normalized)


def observations_from_samples(
    samples: Iterable[Any],
    contract: SpanContract,
) -> tuple[SpanObservation, ...]:
    """Project ``oracle_metrics.OracleRunSample`` objects onto a span.

    Duck-typed on purpose: the registry's sample dataclass and a decoded
    benchmark row both satisfy it, and neither needs to know about this module.
    """

    if not isinstance(contract, SpanContract):
        _refuse("contract_invalid")
    projected: list[SpanObservation] = []
    for sample in samples:
        durations = getattr(sample, "durations_ms", None)
        if not isinstance(durations, Mapping):
            _refuse("sample_invalid")
        phase_ms = {
            phase: _duration(durations[phase], "sample_invalid")
            for phase in contract.phases
            if phase in durations
        }
        if len(phase_ms) != len(contract.phases):
            # A partial breakdown cannot be summed into a span total without
            # silently under-reporting it.
            _refuse("sample_invalid")
        projected.append(
            SpanObservation(
                span_ms=sum(phase_ms.values()),
                warm=bool(getattr(sample, "warm", False)),
                state=str(getattr(sample, "state", STATE_COMPLETED)),
                phase_ms=phase_ms,
            )
        )
    return tuple(projected)


# --------------------------------------------------------------------------- #
# Diagnosis
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PhaseFinding:
    """One phase's contribution to the span's p95."""

    phase: str
    count: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    share_ppm: int
    budget_ms: float | None
    overrun_ms: float | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "count": self.count,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "max_ms": self.max_ms,
            "share_ppm": self.share_ppm,
            "budget_ms": self.budget_ms,
            "overrun_ms": self.overrun_ms,
        }


@dataclass(frozen=True)
class SpanDiagnosis:
    """The verdict, the shape of the miss, and where the time went."""

    span: str
    verdict: str
    shape: str
    count: int
    target_ms: float
    min_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    excess_at_p95_ms: float
    phases: tuple[PhaseFinding, ...]
    notes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.verdict == VERDICT_PASS

    @property
    def every_run_missed(self) -> bool:
        """True when even the fastest observed run exceeded target."""

        return self.shape == SHAPE_FLOOR_EXCEEDED

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema": ORACLE_LATENCY_DIAGNOSIS_SCHEMA,
            "span": self.span,
            "verdict": self.verdict,
            "shape": self.shape,
            "passed": self.passed,
            "count": self.count,
            "target_ms": self.target_ms,
            "min_ms": self.min_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "max_ms": self.max_ms,
            "excess_at_p95_ms": self.excess_at_p95_ms,
            "phases": [finding.to_payload() for finding in self.phases],
            "notes": list(self.notes),
        }
        assert_diagnosis_safe(payload)
        return payload


def _shape(min_ms: float, p50_ms: float, p95_ms: float, target_ms: float) -> str:
    if p95_ms <= target_ms:
        return SHAPE_WITHIN_SLO
    if min_ms > target_ms:
        # No fast case exists: the cost is unconditional, so the thing to find
        # is fixed per-run work, not contention.
        return SHAPE_FLOOR_EXCEEDED
    if p50_ms > target_ms:
        return SHAPE_MEDIAN_EXCEEDED
    return SHAPE_TAIL_ONLY


def diagnose_span(
    observations: Iterable[Any],
    contract: SpanContract,
) -> SpanDiagnosis:
    """Diagnose one span against its contract. Measures only; tunes nothing."""

    if not isinstance(contract, SpanContract):
        _refuse("contract_invalid")
    rows: list[SpanObservation] = []
    for observation in observations:
        if not isinstance(observation, SpanObservation):
            _refuse("sample_invalid")
        rows.append(observation)
    if len(rows) > MAX_SAMPLES:
        _refuse("sample_invalid")

    # The SLO is stated for a temperature and for successful runs; a failed run
    # that aborted early would flatter the numbers.
    selected = [
        row for row in rows if row.warm == contract.warm and row.state == STATE_COMPLETED
    ]
    notes: list[str] = []
    if not selected:
        notes.append(NOTE_NO_SAMPLES)
        return SpanDiagnosis(
            span=contract.name,
            verdict=VERDICT_FAIL,
            shape=SHAPE_WITHIN_SLO,
            count=0,
            target_ms=contract.target_ms,
            min_ms=0.0,
            p50_ms=0.0,
            p95_ms=0.0,
            max_ms=0.0,
            excess_at_p95_ms=0.0,
            phases=(),
            notes=tuple(notes),
        )

    spans = [row.span_ms for row in selected]
    p95 = nearest_rank(spans, P95_PPM)
    p50 = nearest_rank(spans, P50_PPM)
    minimum = min(spans)
    maximum = max(spans)
    verdict = VERDICT_PASS if p95 <= contract.target_ms else VERDICT_FAIL
    shape = _shape(minimum, p50, p95, contract.target_ms)
    excess = max(0.0, p95 - contract.target_ms)

    instrumented = [row for row in selected if row.phase_ms]
    if not instrumented:
        notes.append(NOTE_PHASES_MISSING)
    elif len(instrumented) != len(selected):
        notes.append(NOTE_PHASES_PARTIAL)
    if not contract.phase_budgets_ms:
        notes.append(NOTE_NO_BUDGETS)

    findings: list[PhaseFinding] = []
    for phase in contract.phases:
        values = [
            row.phase_ms[phase] for row in instrumented if phase in row.phase_ms
        ]
        if not values:
            continue
        phase_p95 = nearest_rank(values, P95_PPM)
        budget = contract.phase_budgets_ms.get(phase)
        findings.append(
            PhaseFinding(
                phase=phase,
                count=len(values),
                p50_ms=nearest_rank(values, P50_PPM),
                p95_ms=phase_p95,
                max_ms=max(values),
                # Share of the span's p95, so the table adds up to roughly
                # 1_000_000 and the biggest row is the place to start.
                share_ppm=int(phase_p95 * 1_000_000 / p95) if p95 > 0 else 0,
                budget_ms=budget,
                overrun_ms=None if budget is None else max(0.0, phase_p95 - budget),
            )
        )
    if findings and sum(finding.p95_ms for finding in findings) > p95 * 1.5:
        # Phase p95s are per-phase order statistics; they do not have to sum to
        # the span p95. Flag a wild divergence rather than implying they do.
        notes.append(NOTE_SPAN_UNDERCOUNTED)

    findings.sort(key=lambda finding: finding.p95_ms, reverse=True)
    return SpanDiagnosis(
        span=contract.name,
        verdict=verdict,
        shape=shape,
        count=len(selected),
        target_ms=contract.target_ms,
        min_ms=minimum,
        p50_ms=p50,
        p95_ms=p95,
        max_ms=maximum,
        excess_at_p95_ms=excess,
        phases=tuple(findings),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------- #
# Emission safety — same inversion as oracle_metrics, own vocabulary
# --------------------------------------------------------------------------- #

_STRUCTURAL_KEYS = frozenset(
    {
        "schema",
        "span",
        "verdict",
        "shape",
        "passed",
        "count",
        "target_ms",
        "min_ms",
        "p50_ms",
        "p95_ms",
        "max_ms",
        "excess_at_p95_ms",
        "phases",
        "phase",
        "share_ppm",
        "budget_ms",
        "overrun_ms",
        "notes",
    }
)
_VOCABULARY = frozenset(SPAN_NAMES + VERDICTS + SHAPES + NOTES + LATENCY_PHASES)
_ALLOWED_KEYS = _STRUCTURAL_KEYS | _VOCABULARY
_ALLOWED_STRINGS = _VOCABULARY | {ORACLE_LATENCY_DIAGNOSIS_SCHEMA}
_BOOL_KEYS = frozenset({"passed"})
_MAX_DEPTH = 6


def assert_diagnosis_safe(document: Any, _depth: int = 0) -> None:
    """Fail closed unless every key and string was declared in this module.

    The inverted test, as in ``oracle_metrics.assert_emission_safe``: rather
    than hunting emitted text for secret-shaped substrings, nothing may appear
    that is not on the allowlist. A prompt fragment, URL, account handle, or
    sentinel matches nothing here and cannot be emitted. Numbers pass because a
    bounded finite duration carries no content.
    """

    if _depth > _MAX_DEPTH:
        _refuse("diagnosis_unsafe")
    if document is None:
        return
    if type(document) is bool:
        _refuse("diagnosis_unsafe")
    if type(document) is int:
        return
    if type(document) is float:
        if not math.isfinite(document):
            _refuse("diagnosis_unsafe")
        return
    if type(document) is str:
        if document in _ALLOWED_STRINGS:
            return
        _refuse("diagnosis_unsafe")
    if type(document) is dict:
        for key, value in document.items():
            if type(key) is not str or key not in _ALLOWED_KEYS:
                _refuse("diagnosis_unsafe")
            if type(value) is bool:
                if key not in _BOOL_KEYS:
                    _refuse("diagnosis_unsafe")
                continue
            assert_diagnosis_safe(value, _depth + 1)
        return
    if type(document) is list:
        for item in document:
            assert_diagnosis_safe(item, _depth + 1)
        return
    _refuse("diagnosis_unsafe")


__all__ = [
    "COLD_CLI_TO_SUBMIT",
    "MAX_SAMPLES",
    "NOTES",
    "NOTE_NO_BUDGETS",
    "NOTE_NO_SAMPLES",
    "NOTE_PHASES_MISSING",
    "NOTE_PHASES_PARTIAL",
    "NOTE_SPAN_UNDERCOUNTED",
    "ORACLE_LATENCY_DIAGNOSIS_SCHEMA",
    "P50_PPM",
    "P95_PPM",
    "REFUSAL_CODES",
    "SHAPES",
    "SHAPE_FLOOR_EXCEEDED",
    "SHAPE_MEDIAN_EXCEEDED",
    "SHAPE_TAIL_ONLY",
    "SHAPE_WITHIN_SLO",
    "SPAN_BROWSER_TO_SUBMIT",
    "SPAN_CLI_TO_SUBMIT",
    "SPAN_NAMES",
    "VERDICTS",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "WARM_BROWSER_TO_SUBMIT",
    "OracleLatencyError",
    "PhaseFinding",
    "SpanContract",
    "SpanDiagnosis",
    "SpanObservation",
    "assert_diagnosis_safe",
    "diagnose_span",
    "nearest_rank",
    "observations_from_samples",
]
