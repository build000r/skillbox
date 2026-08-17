"""Dual-format parity between ``scripts/self-test.sh`` and ``sbp test``.

Skillbox is the zeroth consumer of its own test runner, which creates an
obvious hazard: two rival proof formats produced by one codebase, each able to
claim the repo is healthy. This module is the thing that stops that becoming a
schism — it compares the two, run over run, and keeps a ledger of whether they
have ever actually agreed.

**The canonical gate stays authoritative.** ``scripts/self-test.sh`` remains the
release gate until :data:`MIN_CONSECUTIVE_AGREEMENTS` (5, pinned by review
repair) consecutive dual-format runs agree — on the overall verdict *and* on
every per-lane outcome. An operator may raise N; :func:`require_threshold`
refuses to lower it. Never-lie applied to the migration itself: you do not get
to declare the new format trustworthy, you have to observe it.

What counts as agreement is deliberately strict, because a lenient comparison
would manufacture the very confidence this exists to withhold:

* every lane present on one side must be present on the other — a lane missing
  from one format is a **disagreement**, not something to skip past;
* every per-lane outcome must match, not just the overall verdict, because two
  runs can agree on "red" while disagreeing about which lane was red;
* a **non-canonical** self-test run (a lane subset, a worktree overlay) never
  counts toward the ledger at all — a partial run is not evidence about a whole
  gate;
* any disagreement resets the streak to zero. Streaks do not partially survive.

Lane statuses are translated through the same three-axis vocabulary the
receipts leaf uses, so the repair holds on both sides: a lane that exits nonzero
is ``test_outcome=failed`` + ``execution_outcome=completed``, a failed test
rather than broken infrastructure.

Also here, because the golden-image decision (P4) needs measured evidence and
none exists today: :func:`timing_report` records per-lane wall time from both
formats and contrasts the gate's **serial** total with the executor's
**wall-clock** total. That number is the only honest input to "would a golden
image pay for itself".

This module runs nothing. It reads two receipts and compares them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .sbp_test_receipts import (
    EXEC_ADMISSION_UNKNOWN,
    EXEC_COMPLETED,
    EXEC_TIMEOUT,
    PROOF_COMPLETE,
    PROOF_INDETERMINATE,
    TEST_FAILED,
    TEST_NOT_RUN,
    TEST_PASSED,
    Verdict,
)

SELF_TEST_SCHEMA = "skillbox.self-test.receipt/1"
PARITY_SCHEMA = "sbp-test-parity/v1"

#: Review repair 2026-08-14: N is pinned at FIVE. Raising is allowed; lowering
#: is not, and :func:`require_threshold` enforces that rather than trusting a
#: caller to remember it.
MIN_CONSECUTIVE_AGREEMENTS = 5

AUTHORITY_SELF_TEST = "scripts/self-test.sh"
AUTHORITY_SBP_TEST = "sbp test"

#: Self-test lane id -> manifest unit id. Identity today, and deliberately so:
#: an identity mapping cannot drift the way a translation table can. It stays a
#: function so a future rename has exactly one place to live.
LANE_TO_UNIT: Mapping[str, str] = {}

REFUSAL_CODES = frozenset(
    {
        "parity_input_invalid",
        "threshold_too_low",
    }
)


class ParityRefusal(Exception):
    """A typed, fail-closed refusal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_payload(self) -> dict[str, Any]:
        return {"ok": False, "error_code": self.code, "error": self.message}


def _refuse(code: str, message: str) -> Any:
    raise ParityRefusal(code, message)


def unit_for_lane(lane_id: str) -> str:
    return LANE_TO_UNIT.get(lane_id, lane_id)


def require_threshold(n: int = MIN_CONSECUTIVE_AGREEMENTS) -> int:
    """Validate a proposed N. Raising is allowed; lowering below 5 is not."""

    if type(n) is not int or n < MIN_CONSECUTIVE_AGREEMENTS:
        _refuse(
            "threshold_too_low",
            f"the dual-run threshold is pinned at {MIN_CONSECUTIVE_AGREEMENTS}; "
            "an operator may raise it, never lower it",
        )
    return n


# --------------------------------------------------------------------------- #
# Normalizing both formats into one vocabulary
# --------------------------------------------------------------------------- #


def lane_verdict(lane: Mapping[str, Any]) -> Verdict:
    """Translate one ``skillbox.self-test.receipt/1`` lane row.

    The vocabulary repair applies on this side too: a lane that ran and exited
    nonzero is a failed test with a completed execution, not an infrastructure
    fault.
    """

    if not isinstance(lane, Mapping):
        _refuse("parity_input_invalid", "self-test lane must be a mapping")
    status = str(lane.get("status") or "")
    if status == "pass":
        return Verdict(TEST_PASSED, EXEC_COMPLETED, PROOF_COMPLETE)
    if status == "fail":
        return Verdict(TEST_FAILED, EXEC_COMPLETED, PROOF_COMPLETE)
    if status in {"timeout", "timed_out"}:
        return Verdict(TEST_NOT_RUN, EXEC_TIMEOUT, PROOF_INDETERMINATE)
    # A status this module does not recognise is an unknown, never a pass.
    return Verdict(TEST_NOT_RUN, EXEC_ADMISSION_UNKNOWN, PROOF_INDETERMINATE)


def normalize_self_test(receipt: Mapping[str, Any]) -> dict[str, Verdict]:
    """``{unit_id: Verdict}`` from a self-test receipt."""

    if not isinstance(receipt, Mapping):
        _refuse("parity_input_invalid", "self-test receipt must be a mapping")
    if receipt.get("schema") != SELF_TEST_SCHEMA:
        _refuse(
            "parity_input_invalid",
            f"self-test receipt schema must be {SELF_TEST_SCHEMA}",
        )
    lanes = receipt.get("lanes")
    if not isinstance(lanes, list):
        _refuse("parity_input_invalid", "self-test receipt carries no lanes")
    return {
        unit_for_lane(str(lane.get("id") or "")): lane_verdict(lane) for lane in lanes
    }


def normalize_sbp_receipt(receipt: Mapping[str, Any]) -> dict[str, Verdict]:
    """``{unit_id: Verdict}`` from a ``test-receipt/v1`` payload."""

    if not isinstance(receipt, Mapping):
        _refuse("parity_input_invalid", "sbp receipt must be a mapping")
    units = receipt.get("units")
    if not isinstance(units, Mapping):
        _refuse("parity_input_invalid", "sbp receipt carries no units")
    return {str(uid): Verdict.from_mapping(payload) for uid, payload in units.items()}


def self_test_is_canonical(receipt: Mapping[str, Any]) -> bool:
    """A lane subset or worktree overlay is not evidence about the whole gate."""

    return bool(receipt.get("canonical"))


# --------------------------------------------------------------------------- #
# One dual-format observation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LaneComparison:
    unit_id: str
    self_test: Verdict | None
    sbp_test: Verdict | None

    @property
    def agrees(self) -> bool:
        # A lane missing from either side is a disagreement, never a skip: the
        # formats do not agree about what the gate even consists of.
        if self.self_test is None or self.sbp_test is None:
            return False
        return (
            self.self_test.test_outcome == self.sbp_test.test_outcome
            and self.self_test.execution_outcome == self.sbp_test.execution_outcome
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "agrees": self.agrees,
            "self_test": self.self_test.to_payload() if self.self_test else None,
            "sbp_test": self.sbp_test.to_payload() if self.sbp_test else None,
        }


@dataclass(frozen=True)
class ParityObservation:
    """One commit observed through both formats."""

    commit: str
    canonical: bool
    lanes: tuple[LaneComparison, ...]
    self_test_green: bool
    sbp_test_green: bool

    @property
    def verdict_agrees(self) -> bool:
        return self.self_test_green == self.sbp_test_green

    @property
    def disagreements(self) -> tuple[str, ...]:
        return tuple(sorted(lane.unit_id for lane in self.lanes if not lane.agrees))

    @property
    def counts(self) -> bool:
        """Does this observation advance the ledger at all?

        Only a canonical run does. A lane-subset or worktree run may agree
        perfectly and still say nothing about the gate as a whole.
        """

        return self.canonical

    @property
    def agrees(self) -> bool:
        return self.counts and self.verdict_agrees and not self.disagreements

    def to_payload(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "canonical": self.canonical,
            "counts": self.counts,
            "agrees": self.agrees,
            "verdict_agrees": self.verdict_agrees,
            "self_test_green": self.self_test_green,
            "sbp_test_green": self.sbp_test_green,
            "disagreements": list(self.disagreements),
            "lanes": [lane.to_payload() for lane in self.lanes],
        }


def compare(
    self_test_receipt: Mapping[str, Any], sbp_receipt: Mapping[str, Any]
) -> ParityObservation:
    """Compare one commit's two receipts, strictly."""

    left = normalize_self_test(self_test_receipt)
    right = normalize_sbp_receipt(sbp_receipt)
    lanes = tuple(
        LaneComparison(unit_id=uid, self_test=left.get(uid), sbp_test=right.get(uid))
        for uid in sorted(set(left) | set(right))
    )
    return ParityObservation(
        commit=str(self_test_receipt.get("commit") or ""),
        canonical=self_test_is_canonical(self_test_receipt),
        lanes=lanes,
        self_test_green=str(self_test_receipt.get("status") or "") == "pass",
        sbp_test_green=bool(sbp_receipt.get("green")),
    )


# --------------------------------------------------------------------------- #
# The ledger that governs the switch
# --------------------------------------------------------------------------- #


@dataclass
class ParityLedger:
    """Consecutive agreeing dual-format runs. Resets hard on any disagreement."""

    threshold: int = MIN_CONSECUTIVE_AGREEMENTS
    observations: list[ParityObservation] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.threshold = require_threshold(self.threshold)

    def record(self, observation: ParityObservation) -> None:
        if not isinstance(observation, ParityObservation):
            _refuse("parity_input_invalid", "ledger records ParityObservations")
        self.observations.append(observation)

    def consecutive_agreements(self) -> int:
        """The current streak, counting back from the most recent run.

        Non-counting (non-canonical) observations are transparent: they neither
        extend nor break a streak, because they are not evidence either way.
        """

        streak = 0
        for observation in reversed(self.observations):
            if not observation.counts:
                continue
            if not observation.agrees:
                break
            streak += 1
        return streak

    def may_switch(self) -> bool:
        return self.consecutive_agreements() >= self.threshold

    def authority(self) -> str:
        """Who decides a release right now."""

        return AUTHORITY_SBP_TEST if self.may_switch() else AUTHORITY_SELF_TEST

    def to_payload(self) -> dict[str, Any]:
        streak = self.consecutive_agreements()
        return {
            "schema": PARITY_SCHEMA,
            "threshold": self.threshold,
            "consecutive_agreements": streak,
            "remaining": max(0, self.threshold - streak),
            "may_switch": self.may_switch(),
            "authority": self.authority(),
            "observations": [item.to_payload() for item in self.observations],
        }


# --------------------------------------------------------------------------- #
# Timing telemetry — the measured evidence P4 needs
# --------------------------------------------------------------------------- #


def timing_report(
    self_test_receipt: Mapping[str, Any],
    unit_durations: Mapping[str, float],
    *,
    sbp_wall_clock_s: float | None = None,
) -> dict[str, Any]:
    """Per-lane wall time from both formats, plus serial vs wall-clock.

    ``self-test.sh`` runs lanes serially, so its total IS the sum of its lanes.
    The executor runs a wave concurrently, so its wall clock can be much less.
    The difference is the only honest input to "would a golden image pay for
    itself", and today no such number exists anywhere.
    """

    if not isinstance(self_test_receipt, Mapping):
        _refuse("parity_input_invalid", "self-test receipt must be a mapping")
    lanes = self_test_receipt.get("lanes") or []
    gate_lane_s = {
        unit_for_lane(str(lane.get("id") or "")): float(lane.get("duration_s") or 0)
        for lane in lanes
        if isinstance(lane, Mapping)
    }
    serial_total = sum(gate_lane_s.values())
    wall_clock = (
        float(sbp_wall_clock_s)
        if sbp_wall_clock_s is not None
        else sum(float(value) for value in unit_durations.values())
    )
    rows = []
    for unit_id in sorted(set(gate_lane_s) | set(unit_durations)):
        rows.append(
            {
                "unit_id": unit_id,
                "self_test_s": gate_lane_s.get(unit_id),
                "sbp_test_s": (
                    round(float(unit_durations[unit_id]), 3)
                    if unit_id in unit_durations
                    else None
                ),
            }
        )
    return {
        "schema": PARITY_SCHEMA,
        "self_test_serial_total_s": round(serial_total, 3),
        "sbp_test_wall_clock_s": round(wall_clock, 3),
        "speedup": (
            round(serial_total / wall_clock, 3) if wall_clock > 0 else None
        ),
        "lanes": rows,
    }


def load_receipt(path: str | Path) -> dict[str, Any]:
    """Read a receipt from disk. Read-only; refuses rather than guessing."""

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _refuse("parity_input_invalid", f"receipt is unreadable: {Path(path).name}")


def parity_payload(
    ledger: ParityLedger, timing: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    payload = ledger.to_payload()
    if timing is not None:
        payload["timing"] = dict(timing)
    return payload


__all__ = [
    "AUTHORITY_SBP_TEST",
    "AUTHORITY_SELF_TEST",
    "LANE_TO_UNIT",
    "MIN_CONSECUTIVE_AGREEMENTS",
    "PARITY_SCHEMA",
    "REFUSAL_CODES",
    "SELF_TEST_SCHEMA",
    "LaneComparison",
    "ParityLedger",
    "ParityObservation",
    "ParityRefusal",
    "compare",
    "lane_verdict",
    "load_receipt",
    "normalize_sbp_receipt",
    "normalize_self_test",
    "parity_payload",
    "require_threshold",
    "self_test_is_canonical",
    "timing_report",
    "unit_for_lane",
]
