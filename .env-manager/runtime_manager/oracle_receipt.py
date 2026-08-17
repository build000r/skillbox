"""Bounded freshness and refresh contract for the Oracle browser receipt.

The launch receipt records which browser the Oracle lane owns: pid, CDP port,
target id, profile, and a start token that pins the process identity. The
hardened doctor then refuses to use a browser whose receipt has gone stale.

That refusal was time-based only, with a fifteen-minute ceiling, while an
admitted Deep Research run may legitimately hold the same browser for two hours
(and the policy engine will admit up to six). So a browser that was never lost —
same pid, same listener, same target, same profile the whole way — got rejected
at minute sixteen purely because the receipt had aged.

Age was standing in for the thing actually worth proving. A receipt's age says
nothing about whether the browser is still ours; what makes an old receipt
dangerous is that a pid can be recycled, so ``pid`` may name a different process
than the one we launched. This module replaces "the receipt must be young" with
"the receipt must be young **or** continuously re-proven", and keeps the
original ceiling for every receipt that offers no proof.

The contract:

* A receipt with no refresh chain is fresh for ``RECEIPT_MAX_AGE_MS`` — exactly
  the old rule, unweakened.
* A refresh observation extends validity by another ``RECEIPT_MAX_AGE_MS``, but
  only if it re-proves the SAME ownership (pid, port, target id, profile
  fingerprint, and pid start token) and every transport check passed.
* Observations must form an unbroken chain: a gap longer than
  ``RECEIPT_REFRESH_INTERVAL_MS`` ends the chain there, because we cannot vouch
  for an interval we did not watch.
* Total validity is hard-capped by ``receipt_lifetime_for_run``, derived from
  the admitted run timeout, so a receipt can never be renewed forever.
* Any ownership mismatch, or any observation whose transport checks failed, is
  ``broken`` — never renewed, and never merely "stale".

The pid start token is what makes extension safe rather than a loosening: pid
reuse is detectable at any age, so a re-proof at hour two is worth exactly as
much as one at minute two. Without a start token on both the receipt and the
observation, this module refuses to extend at all and the fifteen-minute
ceiling stands.

Pure evaluation. This module inspects no process, opens no socket, and reads no
file; the caller supplies what it observed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .oracle_broker import OracleBrokerError

ORACLE_RECEIPT_FRESHNESS_SCHEMA = "skillbox.oracle-receipt-freshness.v1"

#: Unchanged from the original doctor: a receipt with no proof behind it is
#: good for fifteen minutes and not one millisecond longer.
RECEIPT_MAX_AGE_MS = 15 * 60 * 1000

#: How often a long run must re-prove ownership. Deliberately shorter than
#: RECEIPT_MAX_AGE_MS so a refresher that misses a beat still has slack before
#: the receipt lapses.
RECEIPT_REFRESH_INTERVAL_MS = 5 * 60 * 1000

#: Slack past the admitted run so a run that ends exactly on its deadline can
#: still write its result under a valid receipt.
RECEIPT_GRACE_MS = 5 * 60 * 1000

#: The policy engine's own ceiling on an admitted run; a test pins the two
#: together so the receipt lifetime can never fall short of an admissible run.
MAX_RUN_SECONDS = 21_600

#: Hard cap on total receipt validity, however long the run claimed to be.
RECEIPT_MAX_LIFETIME_MS = MAX_RUN_SECONDS * 1000 + RECEIPT_GRACE_MS

#: Matches the JS doctor's ageIsFresh tolerance for a slightly-ahead clock.
FUTURE_SKEW_MS = 5_000

#: Bounded input: enough observations to cover the longest possible run at the
#: refresh interval, plus slack for retries.
MAX_OBSERVATIONS = RECEIPT_MAX_LIFETIME_MS // RECEIPT_REFRESH_INTERVAL_MS + 8

STATE_FRESH = "fresh"
STATE_RENEWED = "renewed"
STATE_STALE = "stale"
STATE_BROKEN = "broken"
STATES = frozenset({STATE_FRESH, STATE_RENEWED, STATE_STALE, STATE_BROKEN})

TARGET_ID_PATTERN = re.compile(r"^[A-Fa-f0-9]{16,128}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
START_TOKEN_PATTERN = re.compile(r"^[0-9a-zA-Z._:-]{1,128}$")

MIN_PORT = 1024
MAX_PORT = 65535
MAX_PID = 2**31 - 1
MAX_TIMESTAMP_MS = 4_102_444_800_000

OWNERSHIP_KEYS = frozenset(
    {"pid", "port", "target_id", "profile_fingerprint", "pid_start_token"}
)
TRANSPORT_CHECK_KEYS = (
    "single_listener",
    "loopback_only",
    "pid_matches",
    "target_matches",
)

REFUSAL_CODES = frozenset(
    {
        "receipt_invalid",
        "observation_invalid",
        "observation_out_of_order",
        "observation_overflow",
        "doctor_input_invalid",
    }
)

#: Reason codes reported on the verdict. `browser_receipt_stale` is the code the
#: JS doctor already emits; the rest are new and strictly narrower, so an
#: operator can tell "nobody refreshed" apart from "this is a different
#: browser".
REASON_CODES = frozenset(
    {
        "browser_receipt_stale",
        "browser_receipt_expired",
        "browser_identity_changed",
        "browser_ownership_unverified",
        "browser_receipt_unrefreshable",
    }
)


class OracleReceiptError(OracleBrokerError):
    """Stable, non-sensitive receipt-contract refusal."""


def _refuse(code: str) -> Any:
    raise OracleReceiptError(code)


def _bounded_int(value: Any, minimum: int, maximum: int, code: str) -> int:
    # `type(...) is int` rather than isinstance: bool is an int subclass and a
    # `true` pid must never be read as pid 1.
    if type(value) is not int or not minimum <= value <= maximum:
        _refuse(code)
    return value


def _pattern(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _refuse(code)
    return value


@dataclass(frozen=True)
class BrowserOwnership:
    """Exactly which browser the lane owns. Every field is compared on refresh."""

    pid: int
    port: int
    target_id: str
    profile_fingerprint: str
    pid_start_token: str = ""

    def __post_init__(self) -> None:
        _bounded_int(self.pid, 1, MAX_PID, "receipt_invalid")
        _bounded_int(self.port, MIN_PORT, MAX_PORT, "receipt_invalid")
        _pattern(self.target_id, TARGET_ID_PATTERN, "receipt_invalid")
        _pattern(self.profile_fingerprint, FINGERPRINT_PATTERN, "receipt_invalid")
        if self.pid_start_token:
            _pattern(self.pid_start_token, START_TOKEN_PATTERN, "receipt_invalid")

    @property
    def pins_process_identity(self) -> bool:
        """Whether this ownership can survive pid reuse, and so be extended."""

        return bool(self.pid_start_token)

    @classmethod
    def from_mapping(cls, value: Any, code: str = "receipt_invalid") -> BrowserOwnership:
        if not isinstance(value, Mapping):
            _refuse(code)
        unknown = set(value) - OWNERSHIP_KEYS
        if unknown:
            _refuse(code)
        return cls(
            pid=value.get("pid"),  # type: ignore[arg-type]
            port=value.get("port"),  # type: ignore[arg-type]
            target_id=value.get("target_id"),  # type: ignore[arg-type]
            profile_fingerprint=value.get("profile_fingerprint"),  # type: ignore[arg-type]
            pid_start_token=value.get("pid_start_token", ""),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class BrowserReceipt:
    """The launch receipt: when we took ownership, and of what."""

    observed_at_ms: int
    ownership: BrowserOwnership

    def __post_init__(self) -> None:
        _bounded_int(self.observed_at_ms, 0, MAX_TIMESTAMP_MS, "receipt_invalid")
        if not isinstance(self.ownership, BrowserOwnership):
            _refuse("receipt_invalid")


@dataclass(frozen=True)
class OwnershipObservation:
    """One re-proof that the browser we own is still the browser we own.

    ``verified`` is the conjunction of the doctor's transport checks. An
    observation that did NOT verify is evidence of loss, not a missing beat, so
    it breaks the chain instead of being skipped.
    """

    observed_at_ms: int
    ownership: BrowserOwnership
    verified: bool = True

    def __post_init__(self) -> None:
        _bounded_int(self.observed_at_ms, 0, MAX_TIMESTAMP_MS, "observation_invalid")
        if not isinstance(self.ownership, BrowserOwnership):
            _refuse("observation_invalid")
        if type(self.verified) is not bool:
            _refuse("observation_invalid")

    @classmethod
    def from_transport(
        cls,
        observed_at_ms: int,
        ownership: BrowserOwnership,
        transport: Any,
    ) -> OwnershipObservation:
        """Build an observation from the doctor's transport check booleans."""

        if not isinstance(transport, Mapping):
            _refuse("observation_invalid")
        missing = set(TRANSPORT_CHECK_KEYS) - set(transport)
        if missing:
            _refuse("observation_invalid")
        verified = True
        for key in TRANSPORT_CHECK_KEYS:
            value = transport[key]
            if type(value) is not bool:
                _refuse("observation_invalid")
            verified = verified and value
        return cls(
            observed_at_ms=observed_at_ms, ownership=ownership, verified=verified
        )


@dataclass(frozen=True)
class ReceiptFreshness:
    """The verdict: usable or not, why, and when the next proof is due."""

    state: str
    reasons: tuple[str, ...]
    age_ms: int
    verified_through_ms: int
    expires_at_ms: int
    refresh_due_at_ms: int
    lifetime_ms: int
    refresh_count: int

    @property
    def usable(self) -> bool:
        return self.state in (STATE_FRESH, STATE_RENEWED)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": ORACLE_RECEIPT_FRESHNESS_SCHEMA,
            "state": self.state,
            "usable": self.usable,
            "reasons": list(self.reasons),
            "age_ms": self.age_ms,
            "verified_through_ms": self.verified_through_ms,
            "expires_at_ms": self.expires_at_ms,
            "refresh_due_at_ms": self.refresh_due_at_ms,
            "lifetime_ms": self.lifetime_ms,
            "refresh_count": self.refresh_count,
        }


def receipt_lifetime_for_run(timeout_seconds: Any = None) -> int:
    """Total receipt validity allowed for an admitted run, in milliseconds.

    With no admitted run there is nothing to align to, so the base window
    stands. With one, the receipt lives at least as long as the run plus a
    grace period — capped, so "long run" never means "unbounded receipt".
    """

    if timeout_seconds is None:
        return RECEIPT_MAX_AGE_MS
    seconds = _bounded_int(timeout_seconds, 1, MAX_RUN_SECONDS, "doctor_input_invalid")
    return min(
        RECEIPT_MAX_LIFETIME_MS,
        max(RECEIPT_MAX_AGE_MS, seconds * 1000 + RECEIPT_GRACE_MS),
    )


def _validated_observations(
    observations: Any,
    receipt: BrowserReceipt,
    now_ms: int,
) -> tuple[OwnershipObservation, ...]:
    if observations is None:
        return ()
    if isinstance(observations, (str, bytes, Mapping)):
        _refuse("observation_invalid")
    if not isinstance(observations, (Sequence, Iterable)):
        _refuse("observation_invalid")
    ordered = tuple(observations)
    if len(ordered) > MAX_OBSERVATIONS:
        _refuse("observation_overflow")
    previous = receipt.observed_at_ms
    for observation in ordered:
        if not isinstance(observation, OwnershipObservation):
            _refuse("observation_invalid")
        if observation.observed_at_ms < previous:
            # A chain that goes backwards is a clock jump or a forged splice;
            # either way we cannot reason about the interval it claims.
            _refuse("observation_out_of_order")
        if observation.observed_at_ms - now_ms > FUTURE_SKEW_MS:
            _refuse("observation_invalid")
        previous = observation.observed_at_ms
    return ordered


def evaluate_receipt_freshness(
    receipt: Any,
    observations: Any = None,
    *,
    now_ms: Any,
    run_timeout_seconds: Any = None,
    max_age_ms: int = RECEIPT_MAX_AGE_MS,
    refresh_interval_ms: int = RECEIPT_REFRESH_INTERVAL_MS,
    max_lifetime_ms: int | None = None,
) -> ReceiptFreshness:
    """Decide whether a browser receipt may still be used, and say why not.

    Order matters: ownership is checked before time. A receipt whose pid,
    listener, target, profile, or start token changed is ``broken`` no matter
    how young it is, and a chain containing an unverified observation is broken
    no matter how dense it is. Only once ownership is intact does age decide
    anything.
    """

    if not isinstance(receipt, BrowserReceipt):
        _refuse("receipt_invalid")
    now = _bounded_int(now_ms, 0, MAX_TIMESTAMP_MS, "doctor_input_invalid")
    _bounded_int(max_age_ms, 1, RECEIPT_MAX_LIFETIME_MS, "doctor_input_invalid")
    _bounded_int(refresh_interval_ms, 1, RECEIPT_MAX_LIFETIME_MS, "doctor_input_invalid")
    if max_lifetime_ms is None:
        lifetime = receipt_lifetime_for_run(run_timeout_seconds)
    else:
        lifetime = _bounded_int(
            max_lifetime_ms, 1, RECEIPT_MAX_LIFETIME_MS, "doctor_input_invalid"
        )
    if receipt.observed_at_ms - now > FUTURE_SKEW_MS:
        _refuse("doctor_input_invalid")

    chain = _validated_observations(observations, receipt, now)
    age_ms = now - receipt.observed_at_ms
    hard_expiry = receipt.observed_at_ms + lifetime

    def verdict(
        state: str,
        reasons: tuple[str, ...],
        verified_through: int,
        expires_at: int,
        refresh_count: int,
    ) -> ReceiptFreshness:
        return ReceiptFreshness(
            state=state,
            reasons=reasons,
            age_ms=age_ms,
            verified_through_ms=verified_through,
            expires_at_ms=expires_at,
            refresh_due_at_ms=verified_through + refresh_interval_ms,
            lifetime_ms=lifetime,
            refresh_count=refresh_count,
        )

    # -- ownership first: a different browser is never a freshness question --
    for observation in chain:
        if observation.ownership != receipt.ownership:
            return verdict(
                STATE_BROKEN,
                ("browser_identity_changed",),
                receipt.observed_at_ms,
                receipt.observed_at_ms + max_age_ms,
                0,
            )
        if not observation.verified:
            return verdict(
                STATE_BROKEN,
                ("browser_ownership_unverified",),
                receipt.observed_at_ms,
                receipt.observed_at_ms + max_age_ms,
                0,
            )

    # -- extension is only offered to receipts that pin process identity ----
    extendable = receipt.ownership.pins_process_identity
    verified_through = receipt.observed_at_ms
    refresh_count = 0
    if extendable:
        for observation in chain:
            if observation.observed_at_ms - verified_through > refresh_interval_ms:
                # The chain lapsed here. Everything after it re-proves a browser
                # we stopped watching, so it cannot retroactively cover the gap.
                break
            verified_through = observation.observed_at_ms
            refresh_count += 1

    expires_at = min(verified_through + max_age_ms, hard_expiry)

    if now - expires_at > 0:
        if now >= hard_expiry and lifetime > max_age_ms:
            # `expired` is reserved for hitting an EXTENDED lifetime. When no
            # extension was available the two bounds coincide, and the honest
            # code is the one the JS doctor already emits.
            reason = "browser_receipt_expired"
        elif not extendable and chain:
            # Refreshes were offered but the receipt predates start-token
            # pinning, so they cannot be trusted to rule out pid reuse.
            reason = "browser_receipt_unrefreshable"
        else:
            reason = "browser_receipt_stale"
        return verdict(
            STATE_STALE, (reason,), verified_through, expires_at, refresh_count
        )

    state = STATE_RENEWED if refresh_count else STATE_FRESH
    return verdict(state, (), verified_through, expires_at, refresh_count)


def contract_payload() -> dict[str, Any]:
    """The constants the JS doctor must agree with, for cross-language pinning."""

    return {
        "schema": ORACLE_RECEIPT_FRESHNESS_SCHEMA,
        "receipt_max_age_ms": RECEIPT_MAX_AGE_MS,
        "receipt_refresh_interval_ms": RECEIPT_REFRESH_INTERVAL_MS,
        "receipt_grace_ms": RECEIPT_GRACE_MS,
        "receipt_max_lifetime_ms": RECEIPT_MAX_LIFETIME_MS,
        "max_run_seconds": MAX_RUN_SECONDS,
        "future_skew_ms": FUTURE_SKEW_MS,
        "states": sorted(STATES),
        "reason_codes": sorted(REASON_CODES),
        "transport_checks": list(TRANSPORT_CHECK_KEYS),
    }


__all__ = [
    "FUTURE_SKEW_MS",
    "MAX_OBSERVATIONS",
    "MAX_RUN_SECONDS",
    "ORACLE_RECEIPT_FRESHNESS_SCHEMA",
    "OWNERSHIP_KEYS",
    "REASON_CODES",
    "RECEIPT_GRACE_MS",
    "RECEIPT_MAX_AGE_MS",
    "RECEIPT_MAX_LIFETIME_MS",
    "RECEIPT_REFRESH_INTERVAL_MS",
    "REFUSAL_CODES",
    "STATES",
    "STATE_BROKEN",
    "STATE_FRESH",
    "STATE_RENEWED",
    "STATE_STALE",
    "TRANSPORT_CHECK_KEYS",
    "BrowserOwnership",
    "BrowserReceipt",
    "OracleReceiptError",
    "OwnershipObservation",
    "ReceiptFreshness",
    "contract_payload",
    "evaluate_receipt_freshness",
    "receipt_lifetime_for_run",
]
