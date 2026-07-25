"""Canonical READ-ONLY health-provider protocol for a federated doctor.

Skillbox already has three independent health surfaces, each authoritative for
its own domain and each with its own payload shape:

* ``runtime_manager.structure_doctor`` -> ``GateResult``
  ``{name, kind, status(PASS|FAIL|INCO), duration_s, fix_command, detail}``
* ``runtime_manager.evidence`` -> the runtime-evidence ``sections`` map
  (``doctor``/``status``/``pressure``/``pulse``/``skills``/``mcp``/``git``/``beads``),
  each with ``status``, its own counters, ``next_actions``, plus packet-level
  ``blocked_conditions`` and an ``overall`` traffic light.
* ``scripts/04-reconcile.py`` -> ``CheckResult``
  ``{status(pass|warn|fail), code, message, details, fix_command}``

This module defines ONE typed vocabulary those three can all be projected into
WITHOUT losing provider-specific evidence, so a future ``doctor --all`` can
federate them instead of growing a fourth bespoke shape. It is deliberately
inert: no provider imports, no collection, no I/O, no ``subprocess``, no
``--fix``. It is types + a deterministic prioritizer, nothing else.

Field mapping (the fidelity contract this module is designed against)::

    structure_doctor.GateResult   health_protocol
    ---------------------------   ------------------------------------------
    name                          check_id
    kind (structure|runtime)      scope.kind
    status PASS                   STATUS_PASS   (+ severity advisory when the
                                                 detail reports advisory warns)
    status FAIL                   STATUS_FAIL
    status INCO / cap exceeded    STATUS_TIMED_OUT   + cause + timeout_s
    status INCO / dep unreachable STATUS_UNAVAILABLE + cause
    duration_s                    duration_s
    fix_command                   next_action.fix_command (DISPLAY ONLY)
    detail                        detail
    _GateSpec.cap_s               timeout_s

    runtime evidence section      health_protocol
    ---------------------------   ------------------------------------------
    section key (e.g. "pulse")    check_id
    section status pass|warn|fail STATUS_PASS|STATUS_WARN|STATUS_FAIL
    pulse state "unreadable"      STATUS_UNAVAILABLE + cause=<read error>
    scope.root_dir / cwd          scope.target / scope.labels
    section next_actions[]        next_action + related_actions (NONE dropped)
    packet blocked_conditions     blocked_conditions
    pulse last_tick_age_s         observed_at / max_age_s / age_s()
    packet overall green|yellow|red  overall_light(fold_status(results))

    reconcile CheckResult         health_protocol
    ---------------------------   ------------------------------------------
    code                          check_id
    status pass|warn|fail         STATUS_PASS|STATUS_WARN|STATUS_FAIL
    message                       summary
    details (free-form dict)      details (preserved verbatim)
    fix_command (may be None)     next_action (ACTION_NONE when absent)
    (no timing field)             duration_s=None ("not measured", never 0.0)

Five deliberate EXTENSIONS exist because a narrower field set would have thrown
away real provider evidence:

1. ``severity`` is independent of ``status`` because structure_doctor folds
   advisory warnings into PASS ("N advisory warning(s); no failures").
2. ``duration_s`` is OPTIONAL — reconcile's ``CheckResult`` has no timing field
   and reporting ``0.0`` would fabricate a measurement that was never taken.
3. ``related_actions`` exists because an evidence section carries a LIST of
   ``next_actions``; a single ``next_action`` slot would silently discard them.
4. ``details`` is a free-form mapping because reconcile's ``CheckResult.details``
   is provider-defined and must survive federation intact.
5. ``blocked_conditions`` exists because the evidence packet reports gray/blocked
   conditions that are neither a pass nor a fail of any single check.

SAFETY CONTRACT: ``NextAction.fix_command`` is a DISPLAY string. It is text an
operator may read and choose to run themselves. Nothing in this module executes,
schedules, or hands off a command; ``NextAction.executable`` is a hard ``False``
and this module imports no execution primitive (no ``subprocess``, ``os``,
``shutil``, ``eval``, ``exec``). A federated doctor that later grows remediation
must add separately justified, typed, allowlisted actions — not shell out to
these strings.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

# --------------------------------------------------------------------------- #
# Status vocabulary
# --------------------------------------------------------------------------- #

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
# UNKNOWN states. These are NOT pass and NOT fail: the check did not produce a
# verdict. Collapsing them into either direction is the failure mode this
# protocol exists to prevent (a missing dependency reading as a regression, or a
# timeout reading as health). Both REQUIRE a cause and carry full provenance.
STATUS_UNAVAILABLE = "unavailable"
STATUS_TIMED_OUT = "timed_out"

HEALTH_STATUSES = (
    STATUS_PASS,
    STATUS_WARN,
    STATUS_FAIL,
    STATUS_UNAVAILABLE,
    STATUS_TIMED_OUT,
)

# Statuses that did not yield a verdict; they must preserve cause + provenance.
UNKNOWN_STATUSES = frozenset({STATUS_UNAVAILABLE, STATUS_TIMED_OUT})

# Statuses a next action may be proposed for. ``pass`` never is.
ACTIONABLE_STATUSES = frozenset(
    {STATUS_FAIL, STATUS_TIMED_OUT, STATUS_UNAVAILABLE, STATUS_WARN}
)

# Lower rank wins when the prioritizer picks the single primary action. A real
# FAIL outranks an unknown; an unknown outranks an advisory WARN; PASS never
# competes.
_STATUS_RANK = {
    STATUS_FAIL: 0,
    STATUS_TIMED_OUT: 1,
    STATUS_UNAVAILABLE: 2,
    STATUS_WARN: 3,
    STATUS_PASS: 4,
}

# --------------------------------------------------------------------------- #
# Severity vocabulary (independent of status — see EXTENSION 1)
# --------------------------------------------------------------------------- #

SEVERITY_NONE = "none"
SEVERITY_INFO = "info"
SEVERITY_ADVISORY = "advisory"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
# The check could not be evaluated, so its true severity is unknown. Distinct
# from "none" so an unavailable provider is never rendered as healthy.
SEVERITY_UNKNOWN = "unknown"

SEVERITIES = (
    SEVERITY_NONE,
    SEVERITY_INFO,
    SEVERITY_ADVISORY,
    SEVERITY_WARNING,
    SEVERITY_CRITICAL,
    SEVERITY_UNKNOWN,
)

_SEVERITY_RANK = {
    SEVERITY_CRITICAL: 0,
    SEVERITY_WARNING: 1,
    SEVERITY_UNKNOWN: 2,
    SEVERITY_ADVISORY: 3,
    SEVERITY_INFO: 4,
    SEVERITY_NONE: 5,
}

# --------------------------------------------------------------------------- #
# Scope vocabulary
# --------------------------------------------------------------------------- #

SCOPE_STRUCTURE = "structure"
SCOPE_RUNTIME = "runtime"
SCOPE_REPO = "repo"
SCOPE_HOST = "host"
SCOPE_FLEET = "fleet"

SCOPE_KINDS = (SCOPE_STRUCTURE, SCOPE_RUNTIME, SCOPE_REPO, SCOPE_HOST, SCOPE_FLEET)

# --------------------------------------------------------------------------- #
# Next-action vocabulary (typed metadata; NEVER an execution handle)
# --------------------------------------------------------------------------- #

ACTION_NONE = "none"
ACTION_INSPECT = "inspect"
ACTION_REPAIR = "repair"
ACTION_RETRY = "retry"
ACTION_INSTALL_DEPENDENCY = "install_dependency"
ACTION_ESCALATE = "escalate"

ACTION_KINDS = (
    ACTION_NONE,
    ACTION_INSPECT,
    ACTION_REPAIR,
    ACTION_RETRY,
    ACTION_INSTALL_DEPENDENCY,
    ACTION_ESCALATE,
)

# --------------------------------------------------------------------------- #
# Overall traffic light (mirrors the runtime-evidence packet's `overall`)
# --------------------------------------------------------------------------- #

OVERALL_GREEN = "green"
OVERALL_YELLOW = "yellow"
OVERALL_RED = "red"

_OVERALL_BY_STATUS = {
    STATUS_FAIL: OVERALL_RED,
    STATUS_TIMED_OUT: OVERALL_YELLOW,
    STATUS_UNAVAILABLE: OVERALL_YELLOW,
    STATUS_WARN: OVERALL_YELLOW,
    STATUS_PASS: OVERALL_GREEN,
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _frozen_strings(values: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(_clean(v) for v in (values or ()) if _clean(v))


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Provenance:
    """Where a result came from. Required on EVERY result, unknown or not.

    ``collector`` is a DISPLAY string naming the read-only surface that produced
    the evidence (e.g. ``"python3 .env-manager/manage.py doctor --format json"``).
    Like ``NextAction.fix_command`` it is never executed by this module.
    """

    provider_id: str
    source: str
    collector: str = ""
    evidence_ref: str = ""

    def __post_init__(self) -> None:
        if not _clean(self.provider_id):
            raise ValueError("provenance requires a provider_id")
        if not _clean(self.source):
            raise ValueError(f"provenance for {self.provider_id!r} requires a source")

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "source": self.source,
            "collector": self.collector,
            "evidence_ref": self.evidence_ref,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Provenance":
        return cls(
            provider_id=_clean(payload.get("provider_id")),
            source=_clean(payload.get("source")),
            collector=_clean(payload.get("collector")),
            evidence_ref=_clean(payload.get("evidence_ref")),
        )


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckScope:
    """What the check speaks for: a kind, a concrete target, and free labels."""

    kind: str
    target: str = ""
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in SCOPE_KINDS:
            raise ValueError(
                f"unknown scope kind {self.kind!r}; expected one of {list(SCOPE_KINDS)}"
            )
        object.__setattr__(self, "labels", _frozen_strings(self.labels))

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "labels": list(self.labels)}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CheckScope":
        return cls(
            kind=_clean(payload.get("kind")),
            target=_clean(payload.get("target")),
            labels=_frozen_strings(payload.get("labels")),
        )


# --------------------------------------------------------------------------- #
# Next action
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NextAction:
    """Typed next-action METADATA. Display-only; never an execution handle.

    ``fix_command`` is text for a human to read and decide about. ``executable``
    is a hard ``False``: there is no code path in this module (or intended in any
    consumer of it) that runs the string. A real remediation lane must be a
    separately justified, typed, allowlisted action.
    """

    action_id: str
    kind: str
    summary: str = ""
    fix_command: str = ""
    target: str = ""
    requires_human: bool = True

    def __post_init__(self) -> None:
        if not _clean(self.action_id):
            raise ValueError("next action requires a stable action_id")
        if self.kind not in ACTION_KINDS:
            raise ValueError(
                f"unknown action kind {self.kind!r}; expected one of {list(ACTION_KINDS)}"
            )
        if self.kind == ACTION_NONE and _clean(self.fix_command):
            raise ValueError(
                f"action {self.action_id!r} is ACTION_NONE but carries a fix_command"
            )
        if not self.requires_human:
            raise ValueError(
                f"action {self.action_id!r} set requires_human=False; every action in "
                "this protocol is operator-decided (there is no automated fix lane)"
            )

    @property
    def executable(self) -> bool:
        """Always ``False``. ``fix_command`` is display text, not an action."""
        return False

    @property
    def is_actionable(self) -> bool:
        return self.kind != ACTION_NONE

    def to_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "summary": self.summary,
            "fix_command": self.fix_command,
            "target": self.target,
            "requires_human": self.requires_human,
            "executable": False,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "NextAction":
        return cls(
            action_id=_clean(payload.get("action_id")),
            kind=_clean(payload.get("kind")),
            summary=_clean(payload.get("summary")),
            fix_command=_clean(payload.get("fix_command")),
            target=_clean(payload.get("target")),
            requires_human=bool(payload.get("requires_human", True)),
        )


NO_ACTION = NextAction(
    action_id="none",
    kind=ACTION_NONE,
    summary="no action required",
)


# --------------------------------------------------------------------------- #
# The result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HealthCheckResult:
    """One federated health observation from one provider.

    ``check_id`` + ``provider_id`` form the stable identity a consumer may pin,
    dedupe, and suppress on; they must be stable across runs of the same check.
    """

    check_id: str
    provider_id: str
    scope: CheckScope
    status: str
    severity: str
    observed_at: float
    provenance: Provenance
    summary: str = ""
    detail: str = ""
    # None means "this provider does not measure duration" (see EXTENSION 2).
    duration_s: float | None = None
    # None means "this result does not expire".
    max_age_s: float | None = None
    # Wall-clock cap that produced a STATUS_TIMED_OUT, when the provider has one.
    timeout_s: float | None = None
    # REQUIRED for unavailable / timed_out: why no verdict was produced.
    cause: str = ""
    next_action: NextAction = NO_ACTION
    related_actions: tuple[NextAction, ...] = ()
    blocked_conditions: tuple[str, ...] = ()
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not _clean(self.check_id):
            raise ValueError("health check requires a stable check_id")
        if not _clean(self.provider_id):
            raise ValueError(f"check {self.check_id!r} requires a provider_id")
        if self.status not in HEALTH_STATUSES:
            raise ValueError(
                f"check {self.check_id!r} has unknown status {self.status!r}; "
                f"expected one of {list(HEALTH_STATUSES)}"
            )
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"check {self.check_id!r} has unknown severity {self.severity!r}; "
                f"expected one of {list(SEVERITIES)}"
            )
        if not isinstance(self.scope, CheckScope):
            raise TypeError(f"check {self.check_id!r} requires a CheckScope")
        if not isinstance(self.provenance, Provenance):
            raise TypeError(f"check {self.check_id!r} requires a Provenance")
        if self.provenance.provider_id != self.provider_id:
            raise ValueError(
                f"check {self.check_id!r} provenance provider_id "
                f"{self.provenance.provider_id!r} != {self.provider_id!r}"
            )
        if self.status in UNKNOWN_STATUSES and not _clean(self.cause):
            raise ValueError(
                f"check {self.check_id!r} is {self.status!r} but carries no cause; "
                "unavailable/timed_out MUST preserve why no verdict was produced"
            )
        if self.status == STATUS_PASS and _clean(self.cause):
            raise ValueError(
                f"check {self.check_id!r} passed but carries a cause; cause explains "
                "the absence of a verdict, not a healthy one"
            )
        if self.duration_s is not None and self.duration_s < 0:
            raise ValueError(f"check {self.check_id!r} has negative duration_s")
        object.__setattr__(self, "observed_at", float(self.observed_at))
        object.__setattr__(self, "related_actions", tuple(self.related_actions or ()))
        object.__setattr__(
            self, "blocked_conditions", _frozen_strings(self.blocked_conditions)
        )
        object.__setattr__(
            self, "details", dict(self.details) if self.details else {}
        )

    # -- freshness ---------------------------------------------------------- #

    def age_s(self, now: float | None = None) -> float:
        """Seconds since the observation. Never negative (clock skew clamps to 0)."""
        reference = time.time() if now is None else float(now)
        return max(0.0, round(reference - self.observed_at, 3))

    def is_stale(self, now: float | None = None) -> bool:
        """True when the result outlived ``max_age_s``. No max_age -> never stale."""
        if self.max_age_s is None:
            return False
        return self.age_s(now) > float(self.max_age_s)

    def freshness(self, now: float | None = None) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "age_s": self.age_s(now),
            "max_age_s": self.max_age_s,
            "stale": self.is_stale(now),
        }

    # -- classification ----------------------------------------------------- #

    @property
    def is_unknown(self) -> bool:
        """True for unavailable/timed_out — no verdict, neither pass nor fail."""
        return self.status in UNKNOWN_STATUSES

    @property
    def is_actionable(self) -> bool:
        return self.status in ACTIONABLE_STATUSES and self.next_action.is_actionable

    @property
    def all_actions(self) -> tuple[NextAction, ...]:
        """Primary action first, then the provider's related actions."""
        actions = [self.next_action] if self.next_action.is_actionable else []
        actions.extend(a for a in self.related_actions if a.is_actionable)
        return tuple(actions)

    # -- payload ------------------------------------------------------------ #

    def to_payload(self, now: float | None = None) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "provider_id": self.provider_id,
            "scope": self.scope.to_payload(),
            "status": self.status,
            "severity": self.severity,
            "freshness": self.freshness(now),
            "duration_s": self.duration_s,
            "timeout_s": self.timeout_s,
            "cause": self.cause,
            "summary": self.summary,
            "detail": self.detail,
            "provenance": self.provenance.to_payload(),
            "next_action": self.next_action.to_payload(),
            "related_actions": [a.to_payload() for a in self.related_actions],
            "blocked_conditions": list(self.blocked_conditions),
            "details": dict(self.details or {}),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "HealthCheckResult":
        freshness = payload.get("freshness") or {}
        return cls(
            check_id=_clean(payload.get("check_id")),
            provider_id=_clean(payload.get("provider_id")),
            scope=CheckScope.from_payload(payload.get("scope") or {}),
            status=_clean(payload.get("status")),
            severity=_clean(payload.get("severity")),
            observed_at=float(freshness.get("observed_at") or 0.0),
            provenance=Provenance.from_payload(payload.get("provenance") or {}),
            summary=_clean(payload.get("summary")),
            detail=_clean(payload.get("detail")),
            duration_s=payload.get("duration_s"),
            max_age_s=freshness.get("max_age_s"),
            timeout_s=payload.get("timeout_s"),
            cause=_clean(payload.get("cause")),
            next_action=NextAction.from_payload(payload.get("next_action") or {}),
            related_actions=tuple(
                NextAction.from_payload(a) for a in payload.get("related_actions") or []
            ),
            blocked_conditions=_frozen_strings(payload.get("blocked_conditions")),
            details=payload.get("details") or {},
        )


# --------------------------------------------------------------------------- #
# Provider interface (contract only — this module integrates no provider)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderDescriptor:
    """Static self-description a provider offers before it is ever run."""

    provider_id: str
    title: str
    scope_kinds: tuple[str, ...] = ()
    default_max_age_s: float | None = None
    # A provider that mutates anything is out of contract for this federation.
    read_only: bool = True

    def __post_init__(self) -> None:
        if not _clean(self.provider_id):
            raise ValueError("provider descriptor requires a provider_id")
        object.__setattr__(self, "scope_kinds", _frozen_strings(self.scope_kinds))
        unknown = [k for k in self.scope_kinds if k not in SCOPE_KINDS]
        if unknown:
            raise ValueError(f"provider {self.provider_id!r} declares unknown scopes {unknown}")
        if not self.read_only:
            raise ValueError(
                f"provider {self.provider_id!r} declared read_only=False; the health "
                "federation admits read-only providers only"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "title": self.title,
            "scope_kinds": list(self.scope_kinds),
            "default_max_age_s": self.default_max_age_s,
            "read_only": True,
        }


@runtime_checkable
class HealthProvider(Protocol):
    """What a federated provider must offer. Read-only by construction."""

    def describe(self) -> ProviderDescriptor:
        """Static description; cheap, side-effect free, safe to call always."""
        ...

    def collect(self) -> Sequence[HealthCheckResult]:
        """Observe and return results. MUST NOT mutate anything it inspects."""
        ...


# --------------------------------------------------------------------------- #
# Deterministic prioritization
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ActionRef:
    """A SECONDARY reference. Deliberately carries no ``fix_command``.

    Exactly one command is ever presented for copy/paste (the primary). Secondary
    findings are referenced by identity so a rendered surface cannot turn into a
    menu of shell commands.
    """

    check_id: str
    provider_id: str
    status: str
    severity: str
    action_id: str
    action_kind: str
    summary: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "provider_id": self.provider_id,
            "status": self.status,
            "severity": self.severity,
            "action_id": self.action_id,
            "action_kind": self.action_kind,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class PrimaryAction:
    """The single next action, with the finding and the reason it was chosen."""

    check_id: str
    provider_id: str
    status: str
    severity: str
    stale: bool
    action: NextAction
    rationale: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "provider_id": self.provider_id,
            "status": self.status,
            "severity": self.severity,
            "stale": self.stale,
            "action": self.action.to_payload(),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class Prioritization:
    """At most ONE primary action plus references to everything else."""

    primary: PrimaryAction | None
    secondary: tuple[ActionRef, ...]
    considered: int
    status_counts: Mapping[str, int]
    overall: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "primary": self.primary.to_payload() if self.primary else None,
            "secondary": [ref.to_payload() for ref in self.secondary],
            "considered": self.considered,
            "status_counts": dict(self.status_counts),
            "overall": self.overall,
        }


def priority_key(result: HealthCheckResult, now: float | None = None) -> tuple[Any, ...]:
    """Total order used to pick the primary action. Lower sorts first.

    Deterministic and independent of input order:
    ``(status rank, severity rank, stale, provider_id, check_id, action_id)``.
    Status leads so a real FAIL outranks an unknown, and an unknown outranks an
    advisory WARN. Freshness breaks severity ties (a fresh finding beats a stale
    one). The trailing identity fields make the order total.
    """
    return (
        _STATUS_RANK.get(result.status, len(_STATUS_RANK)),
        _SEVERITY_RANK.get(result.severity, len(_SEVERITY_RANK)),
        1 if result.is_stale(now) else 0,
        result.provider_id,
        result.check_id,
        result.next_action.action_id,
    )


def status_counts(results: Iterable[HealthCheckResult]) -> dict[str, int]:
    counts = {status: 0 for status in HEALTH_STATUSES}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def fold_status(results: Iterable[HealthCheckResult]) -> str:
    """Worst status across results. Empty -> ``pass`` (nothing observed failing)."""
    worst = STATUS_PASS
    worst_rank = _STATUS_RANK[STATUS_PASS]
    for result in results:
        rank = _STATUS_RANK.get(result.status, len(_STATUS_RANK))
        if rank < worst_rank:
            worst, worst_rank = result.status, rank
    return worst


def overall_light(status: str) -> str:
    """Map a folded status onto the runtime-evidence traffic light."""
    return _OVERALL_BY_STATUS.get(status, OVERALL_YELLOW)


def prioritize(
    results: Iterable[HealthCheckResult], now: float | None = None
) -> Prioritization:
    """Pick at most one primary next action; reference the rest as secondary.

    Pure and deterministic: the same set of results yields the same output
    regardless of iteration order. ``pass`` results and results whose action is
    ``ACTION_NONE`` never compete.
    """
    materialized = list(results)
    candidates = sorted(
        (r for r in materialized if r.is_actionable),
        key=lambda r: priority_key(r, now),
    )
    counts = status_counts(materialized)
    overall = overall_light(fold_status(materialized))

    if not candidates:
        return Prioritization(
            primary=None,
            secondary=(),
            considered=0,
            status_counts=counts,
            overall=overall,
        )

    head = candidates[0]
    tail = candidates[1:]
    rationale = (
        f"status={head.status} severity={head.severity} "
        f"{'stale' if head.is_stale(now) else 'fresh'}; "
        f"outranks {len(tail)} other actionable finding(s) under "
        "(status, severity, freshness, provider_id, check_id)"
    )
    primary = PrimaryAction(
        check_id=head.check_id,
        provider_id=head.provider_id,
        status=head.status,
        severity=head.severity,
        stale=head.is_stale(now),
        action=head.next_action,
        rationale=rationale,
    )
    secondary: list[ActionRef] = []
    for result in tail:
        for action in result.all_actions:
            secondary.append(
                ActionRef(
                    check_id=result.check_id,
                    provider_id=result.provider_id,
                    status=result.status,
                    severity=result.severity,
                    action_id=action.action_id,
                    action_kind=action.kind,
                    summary=action.summary,
                )
            )
    # The head's own extra actions are secondary too — only ONE primary exists.
    for action in head.related_actions:
        if action.is_actionable:
            secondary.append(
                ActionRef(
                    check_id=head.check_id,
                    provider_id=head.provider_id,
                    status=head.status,
                    severity=head.severity,
                    action_id=action.action_id,
                    action_kind=action.kind,
                    summary=action.summary,
                )
            )
    return Prioritization(
        primary=primary,
        secondary=tuple(secondary),
        considered=len(candidates),
        status_counts=counts,
        overall=overall,
    )


def federation_payload(
    results: Iterable[HealthCheckResult], now: float | None = None
) -> dict[str, Any]:
    """The full read-only federation payload: checks + one prioritized action."""
    materialized = list(results)
    prioritization = prioritize(materialized, now)
    ordered = sorted(materialized, key=lambda r: priority_key(r, now))
    return {
        "kind": "health-federation",
        "checks": [r.to_payload(now) for r in ordered],
        "prioritization": prioritization.to_payload(),
        "summary": {
            "total": len(materialized),
            "counts": prioritization.status_counts,
            "unknown": sum(1 for r in materialized if r.is_unknown),
            "stale": sum(1 for r in materialized if r.is_stale(now)),
            "overall": prioritization.overall,
        },
    }


__all__ = [
    "ACTIONABLE_STATUSES",
    "ACTION_ESCALATE",
    "ACTION_INSPECT",
    "ACTION_INSTALL_DEPENDENCY",
    "ACTION_KINDS",
    "ACTION_NONE",
    "ACTION_REPAIR",
    "ACTION_RETRY",
    "ActionRef",
    "CheckScope",
    "HEALTH_STATUSES",
    "HealthCheckResult",
    "HealthProvider",
    "NO_ACTION",
    "NextAction",
    "OVERALL_GREEN",
    "OVERALL_RED",
    "OVERALL_YELLOW",
    "PrimaryAction",
    "Prioritization",
    "Provenance",
    "ProviderDescriptor",
    "SCOPE_FLEET",
    "SCOPE_HOST",
    "SCOPE_KINDS",
    "SCOPE_REPO",
    "SCOPE_RUNTIME",
    "SCOPE_STRUCTURE",
    "SEVERITIES",
    "SEVERITY_ADVISORY",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_NONE",
    "SEVERITY_UNKNOWN",
    "SEVERITY_WARNING",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_TIMED_OUT",
    "STATUS_UNAVAILABLE",
    "STATUS_WARN",
    "UNKNOWN_STATUSES",
    "federation_payload",
    "fold_status",
    "overall_light",
    "priority_key",
    "prioritize",
    "status_counts",
]
