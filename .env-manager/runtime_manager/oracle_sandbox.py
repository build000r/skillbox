"""Chrome sandbox posture for the Oracle host, and the exception that contains it.

The cookie-bearing canonical Chrome runs with ``--no-sandbox``. That flag turns
off the renderer sandbox on the one process on the estate that holds a live,
authenticated ChatGPT session, so a renderer compromise reaches the profile
directory and its cookies directly. Restoring the browser sandbox is the
preferred outcome and this module reports it as the only clean one.

Where the host genuinely cannot support it, the bead's fallback applies: the
exception is written down, it expires, and it is only accepted while four
compensating controls are *verified* — a single service uid, no shared
interactive logins, hardened unit isolation, and bounded filesystem access.

The whole point of this module is the one thing such a check usually gets
wrong. **A waived sandbox never reports green.** There is no combination of
controls, no waiver, and no evidence that makes ``--no-sandbox`` evaluate to
``enforced``; a test enumerates the entire input space to keep it that way. The
best a waived host can achieve is ``waived`` — permanently visible in every
doctor run, explicitly not a pass, and non-blocking so it does not train
operators to ignore a red gate they cannot fix today.

Fail-closed the other way too: a declaration that is present but malformed, a
waiver that has expired, a waiver approved for a different host, or a control
asserted without evidence are all ``uncontained``. Only the absence of any
declaration at all is inconclusive, because most boxes are simply not the
Oracle host.

This module runs no probe. It reads one declaration file and evaluates the
facts a host-side collector wrote into it.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .oracle_broker import OracleBrokerError

ORACLE_SANDBOX_DECLARATION_SCHEMA = "skillbox.oracle-sandbox-posture.v1"
SANDBOX_DECLARATION_REL_PATH = ("oracle", "sandbox-posture.json")
MAX_DECLARATION_BYTES = 16 * 1024

#: The four compensating controls the bead requires before an exception may be
#: accepted. All four, or the exception is not accepted.
CONTROL_SINGLE_SERVICE_UID = "single_service_uid"
CONTROL_NO_SHARED_LOGINS = "no_shared_interactive_logins"
CONTROL_HARDENED_UNIT = "hardened_unit_isolation"
CONTROL_BOUNDED_FILESYSTEM = "bounded_filesystem_access"
REQUIRED_CONTROLS = (
    CONTROL_SINGLE_SERVICE_UID,
    CONTROL_NO_SHARED_LOGINS,
    CONTROL_HARDENED_UNIT,
    CONTROL_BOUNDED_FILESYSTEM,
)

STATE_ENFORCED = "enforced"
STATE_WAIVED = "waived"
STATE_UNCONTAINED = "uncontained"
STATE_UNDECLARED = "undeclared"
STATES = frozenset({STATE_ENFORCED, STATE_WAIVED, STATE_UNCONTAINED, STATE_UNDECLARED})

#: Doctor-family verdicts. `waived` is deliberately NOT pass: the doctor cannot
#: certify a sandbox that is switched off, so it declines to say green while
#: staying non-blocking for an accepted exception.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_INCONCLUSIVE = "inconclusive"
_STATE_VERDICTS: Mapping[str, str] = {
    STATE_ENFORCED: VERDICT_PASS,
    STATE_WAIVED: VERDICT_INCONCLUSIVE,
    STATE_UNCONTAINED: VERDICT_FAIL,
    STATE_UNDECLARED: VERDICT_INCONCLUSIVE,
}

WAIVER_REASONS = frozenset(
    {
        "userns_unavailable",
        "kernel_lockdown",
        "container_runtime_restriction",
        "vendor_binary_limitation",
    }
)

#: A waiver is a dated exception, not a policy. Ninety days is long enough to
#: schedule a kernel or image change and short enough that nobody inherits it
#: silently.
MAX_WAIVER_DURATION_MS = 90 * 24 * 60 * 60 * 1000
MAX_TIMESTAMP_MS = 4_102_444_800_000

HOST_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
APPROVER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,63}$")
#: Short, non-secret evidence tokens (`systemd:ProtectSystem=strict`). Bounded
#: and pattern-checked so a doctor line can never carry a host path or a secret.
EVIDENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:=/+-]{0,95}$")

REFUSAL_CODES = frozenset(
    {
        "control_evidence_missing",
        "declaration_invalid",
        "declaration_permissions",
        "sandbox_input_invalid",
        "waiver_invalid",
    }
)

REASON_CODES = frozenset(
    {
        "no_sandbox_flag",
        "sandbox_unavailable",
        "waiver_absent",
        "waiver_expired",
        "waiver_host_mismatch",
        "control_unverified",
        "declaration_absent",
    }
)


class OracleSandboxError(OracleBrokerError):
    """Stable, non-sensitive sandbox-posture refusal."""


def _refuse(code: str) -> Any:
    raise OracleSandboxError(code)


def _bounded_int(value: Any, minimum: int, maximum: int, code: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _refuse(code)
    return value


def _pattern(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _refuse(code)
    return value


def _flag(value: Any, code: str) -> bool:
    if type(value) is not bool:
        _refuse(code)
    return value


@dataclass(frozen=True)
class ChromeSandboxEvidence:
    """What the host observed about the browser's own sandbox."""

    no_sandbox_flag: bool
    user_namespaces_available: bool
    setuid_sandbox_present: bool

    def __post_init__(self) -> None:
        for value in (
            self.no_sandbox_flag,
            self.user_namespaces_available,
            self.setuid_sandbox_present,
        ):
            _flag(value, "sandbox_input_invalid")

    @property
    def sandbox_mechanism_available(self) -> bool:
        return self.user_namespaces_available or self.setuid_sandbox_present

    @classmethod
    def from_mapping(cls, value: Any) -> ChromeSandboxEvidence:
        if not isinstance(value, Mapping):
            _refuse("declaration_invalid")
        keys = {
            "no_sandbox_flag",
            "user_namespaces_available",
            "setuid_sandbox_present",
        }
        if set(value) != keys:
            _refuse("declaration_invalid")
        try:
            return cls(
                no_sandbox_flag=value["no_sandbox_flag"],
                user_namespaces_available=value["user_namespaces_available"],
                setuid_sandbox_present=value["setuid_sandbox_present"],
            )
        except OracleSandboxError:
            _refuse("declaration_invalid")


@dataclass(frozen=True)
class CompensatingControl:
    """One control, and the evidence that it is actually in force.

    ``verified`` without ``evidence`` is refused rather than believed. An
    unevidenced claim is exactly how a containment check goes quietly green.
    """

    name: str
    verified: bool
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.name not in REQUIRED_CONTROLS:
            _refuse("sandbox_input_invalid")
        _flag(self.verified, "sandbox_input_invalid")
        if type(self.evidence) is not str:
            _refuse("sandbox_input_invalid")
        if self.verified:
            if not self.evidence:
                _refuse("control_evidence_missing")
            _pattern(self.evidence, EVIDENCE_PATTERN, "control_evidence_missing")
        elif self.evidence:
            _pattern(self.evidence, EVIDENCE_PATTERN, "sandbox_input_invalid")


def _validated_controls(value: Any, code: str = "sandbox_input_invalid") -> tuple[CompensatingControl, ...]:
    if isinstance(value, Mapping):
        if set(value) != set(REQUIRED_CONTROLS):
            _refuse(code)
        controls = []
        for name in REQUIRED_CONTROLS:
            entry = value[name]
            if not isinstance(entry, Mapping) or set(entry) != {"verified", "evidence"}:
                _refuse(code)
            controls.append(
                CompensatingControl(
                    name=name,
                    verified=entry["verified"],
                    evidence=entry["evidence"],
                )
            )
        return tuple(controls)
    if not isinstance(value, (list, tuple)):
        _refuse(code)
    controls = []
    for entry in value:
        if not isinstance(entry, CompensatingControl):
            _refuse(code)
        controls.append(entry)
    names = [control.name for control in controls]
    if sorted(names) != sorted(REQUIRED_CONTROLS):
        # Every control must be reported, present or not. A missing entry is
        # indistinguishable from an unverified one, so it must not be optional.
        _refuse(code)
    return tuple(controls)


@dataclass(frozen=True)
class SandboxWaiver:
    """A dated, host-scoped, operator-approved exception."""

    host: str
    reason: str
    approved_by: str
    approved_at_ms: int
    expires_at_ms: int

    def __post_init__(self) -> None:
        _pattern(self.host, HOST_PATTERN, "waiver_invalid")
        if self.reason not in WAIVER_REASONS:
            _refuse("waiver_invalid")
        _pattern(self.approved_by, APPROVER_PATTERN, "waiver_invalid")
        _bounded_int(self.approved_at_ms, 0, MAX_TIMESTAMP_MS, "waiver_invalid")
        _bounded_int(self.expires_at_ms, 0, MAX_TIMESTAMP_MS, "waiver_invalid")
        if self.expires_at_ms <= self.approved_at_ms:
            _refuse("waiver_invalid")
        if self.expires_at_ms - self.approved_at_ms > MAX_WAIVER_DURATION_MS:
            # An exception with no practical end date is a policy change
            # wearing a waiver's clothes.
            _refuse("waiver_invalid")

    def active_at(self, now_ms: int) -> bool:
        return now_ms < self.expires_at_ms

    @classmethod
    def from_mapping(cls, value: Any) -> SandboxWaiver:
        if not isinstance(value, Mapping):
            _refuse("waiver_invalid")
        keys = {"host", "reason", "approved_by", "approved_at_ms", "expires_at_ms"}
        if set(value) != keys:
            _refuse("waiver_invalid")
        return cls(
            host=value["host"],
            reason=value["reason"],
            approved_by=value["approved_by"],
            approved_at_ms=value["approved_at_ms"],
            expires_at_ms=value["expires_at_ms"],
        )


@dataclass(frozen=True)
class SandboxPosture:
    """The verdict, the reasons, and what an operator should do next."""

    state: str
    verdict: str
    reasons: tuple[str, ...]
    unverified_controls: tuple[str, ...]
    waiver_active: bool
    waiver_expires_at_ms: int | None

    @property
    def green(self) -> bool:
        """True only for a genuinely enforced sandbox. Never for a waiver."""

        return self.state == STATE_ENFORCED

    def detail(self) -> str:
        if self.state == STATE_ENFORCED:
            return "chrome sandbox enforced"
        if self.state == STATE_UNDECLARED:
            return "no oracle sandbox declaration on this box (not the oracle host)"
        if self.state == STATE_WAIVED:
            return (
                "chrome sandbox DISABLED under an accepted, expiring exception; "
                f"all {len(REQUIRED_CONTROLS)} compensating controls verified; "
                f"waiver expires at {self.waiver_expires_at_ms}"
            )
        return "chrome sandbox DISABLED and NOT contained: " + ", ".join(self.reasons)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": ORACLE_SANDBOX_DECLARATION_SCHEMA,
            "state": self.state,
            "verdict": self.verdict,
            "green": self.green,
            "reasons": list(self.reasons),
            "unverified_controls": list(self.unverified_controls),
            "waiver_active": self.waiver_active,
            "waiver_expires_at_ms": self.waiver_expires_at_ms,
            "detail": self.detail(),
        }


def evaluate_sandbox_posture(
    evidence: Any,
    controls: Any,
    waiver: Any = None,
    *,
    now_ms: Any,
    host: Any,
) -> SandboxPosture:
    """Classify the host's Chrome sandbox posture. ``--no-sandbox`` is never green.

    Order: the browser's own sandbox decides first. Only once it is known to be
    off does the exception machinery run, and it can at best downgrade the
    failure to a visible, expiring, fully-compensated waiver.
    """

    if not isinstance(evidence, ChromeSandboxEvidence):
        _refuse("sandbox_input_invalid")
    now = _bounded_int(now_ms, 0, MAX_TIMESTAMP_MS, "sandbox_input_invalid")
    host_label = _pattern(host, HOST_PATTERN, "sandbox_input_invalid")
    verified_controls = _validated_controls(controls)
    unverified = tuple(
        control.name for control in verified_controls if not control.verified
    )

    if not evidence.no_sandbox_flag:
        if not evidence.sandbox_mechanism_available:
            # Chrome was launched expecting a sandbox the kernel cannot give it.
            # That is a broken host, not a clean one.
            return SandboxPosture(
                state=STATE_UNCONTAINED,
                verdict=_STATE_VERDICTS[STATE_UNCONTAINED],
                reasons=("sandbox_unavailable",),
                unverified_controls=unverified,
                waiver_active=False,
                waiver_expires_at_ms=None,
            )
        return SandboxPosture(
            state=STATE_ENFORCED,
            verdict=_STATE_VERDICTS[STATE_ENFORCED],
            reasons=(),
            unverified_controls=unverified,
            waiver_active=False,
            waiver_expires_at_ms=None,
        )

    # --- the sandbox is off; from here nothing can produce a pass ----------
    reasons: list[str] = ["no_sandbox_flag"]
    waiver_active = False
    expires_at: int | None = None

    if waiver is None:
        reasons.append("waiver_absent")
    else:
        if not isinstance(waiver, SandboxWaiver):
            _refuse("waiver_invalid")
        expires_at = waiver.expires_at_ms
        if waiver.host != host_label:
            reasons.append("waiver_host_mismatch")
        elif not waiver.active_at(now):
            reasons.append("waiver_expired")
        else:
            waiver_active = True

    if unverified:
        reasons.append("control_unverified")

    if waiver_active and not unverified:
        return SandboxPosture(
            state=STATE_WAIVED,
            verdict=_STATE_VERDICTS[STATE_WAIVED],
            reasons=("no_sandbox_flag",),
            unverified_controls=(),
            waiver_active=True,
            waiver_expires_at_ms=expires_at,
        )

    return SandboxPosture(
        state=STATE_UNCONTAINED,
        verdict=_STATE_VERDICTS[STATE_UNCONTAINED],
        reasons=tuple(reasons),
        unverified_controls=unverified,
        waiver_active=waiver_active,
        waiver_expires_at_ms=expires_at,
    )


def undeclared_posture() -> SandboxPosture:
    """The verdict for a box that is simply not the Oracle host."""

    return SandboxPosture(
        state=STATE_UNDECLARED,
        verdict=_STATE_VERDICTS[STATE_UNDECLARED],
        reasons=("declaration_absent",),
        unverified_controls=(),
        waiver_active=False,
        waiver_expires_at_ms=None,
    )


def declaration_path(state_root: Any) -> Path:
    """Where the host-side collector writes its sandbox declaration."""

    if isinstance(state_root, Path):
        root = state_root
    elif isinstance(state_root, str) and state_root:
        root = Path(state_root)
    else:
        _refuse("sandbox_input_invalid")
    return root.joinpath(*SANDBOX_DECLARATION_REL_PATH)


def read_declaration(state_root: Any, *, uid: int | None = None) -> dict[str, Any]:
    """Read the sandbox declaration, or refuse. Absence raises FileNotFoundError.

    Absence is the caller's to interpret — most boxes are not the Oracle host —
    but a declaration that exists and is wrong is always a refusal, never a
    shrug.
    """

    getuid = getattr(os, "getuid", None)
    resolved_uid = (getuid() if getuid else 0) if uid is None else uid
    path = declaration_path(state_root)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _refuse("declaration_permissions")
        if metadata.st_uid != resolved_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
            _refuse("declaration_permissions")
        if metadata.st_size > MAX_DECLARATION_BYTES:
            _refuse("declaration_invalid")
        raw = os.read(descriptor, MAX_DECLARATION_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_DECLARATION_BYTES:
        _refuse("declaration_invalid")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _refuse("declaration_invalid")
    if not isinstance(document, dict):
        _refuse("declaration_invalid")
    if document.get("schema") != ORACLE_SANDBOX_DECLARATION_SCHEMA:
        _refuse("declaration_invalid")
    return document


def posture_from_declaration(
    state_root: Any,
    *,
    now_ms: int,
    uid: int | None = None,
) -> SandboxPosture:
    """Evaluate the host declaration if present; undeclared if absent.

    A missing file is the only inconclusive input. A present file that cannot be
    parsed, or whose fields do not validate, refuses — a broken declaration on
    the Oracle host is a finding, not a missing one.
    """

    try:
        document = read_declaration(state_root, uid=uid)
    except FileNotFoundError:
        return undeclared_posture()
    except OSError:
        _refuse("declaration_permissions")

    keys = {"schema", "host", "evidence", "controls"}
    optional = {"waiver"}
    present = set(document)
    if not keys <= present or not present <= (keys | optional):
        _refuse("declaration_invalid")

    evidence = ChromeSandboxEvidence.from_mapping(document["evidence"])
    controls = _validated_controls(document["controls"], "declaration_invalid")
    raw_waiver = document.get("waiver")
    waiver = None if raw_waiver is None else SandboxWaiver.from_mapping(raw_waiver)
    return evaluate_sandbox_posture(
        evidence,
        controls,
        waiver,
        now_ms=now_ms,
        host=document["host"],
    )


__all__ = [
    "CONTROL_BOUNDED_FILESYSTEM",
    "CONTROL_HARDENED_UNIT",
    "CONTROL_NO_SHARED_LOGINS",
    "CONTROL_SINGLE_SERVICE_UID",
    "MAX_WAIVER_DURATION_MS",
    "ORACLE_SANDBOX_DECLARATION_SCHEMA",
    "REASON_CODES",
    "REFUSAL_CODES",
    "REQUIRED_CONTROLS",
    "SANDBOX_DECLARATION_REL_PATH",
    "STATES",
    "STATE_ENFORCED",
    "STATE_UNCONTAINED",
    "STATE_UNDECLARED",
    "STATE_WAIVED",
    "VERDICT_FAIL",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_PASS",
    "WAIVER_REASONS",
    "ChromeSandboxEvidence",
    "CompensatingControl",
    "OracleSandboxError",
    "SandboxPosture",
    "SandboxWaiver",
    "declaration_path",
    "evaluate_sandbox_posture",
    "posture_from_declaration",
    "read_declaration",
    "undeclared_posture",
]
