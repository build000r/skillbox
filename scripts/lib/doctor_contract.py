"""ONE JSON envelope, ONE status vocabulary, and ONE fix contract for the doctor family.

Why this module exists
======================

The pass-2 ergonomics audit (``agent_ergonomics_audit/audit/partial/pass2/
doctor_family.jsonl``, finding ``F-doc-02``) measured **four** incompatible JSON
envelopes and **three** status vocabularies behind one word, ``doctor``:

* outer ``scripts/04-reconcile.py doctor`` — a bare LIST of check dicts
* inner ``.env-manager/manage.py doctor`` — ``{checks, next_actions}``, no ``fix_command``
* ``sbp doctor`` (structure-doctor) — ``{ok, exit_code, gates}`` with UPPERCASE ``PASS/FAIL/INCO``
* ``sbp cass doctor`` — ``{checks: [{name, ok}]}`` booleans

so "is anything failing?" needed a different ``jq`` per doctor. This module is
the single definition every doctor now renders through, so the answer is always::

    <doctor> --format json | jq '.checks[] | select(.status == "fail")'

Who imports it
==============

``scripts/lib`` is the ONE package both halves of the tree can already reach:
``runtime_manager/_shared/shared.py`` puts ``scripts/`` on ``sys.path`` and
imports ``lib.runtime_model`` / ``lib.redaction``, and ``scripts/04-reconcile.py``
imports ``lib.runtime_model`` directly. The outer reconcile script famously
*cannot* import ``runtime_manager`` (see ``tests/test_reconcile.py``
``RuntimeDoctorExitVocabularyTests``), which is exactly why the shared contract
lives here and not under ``.env-manager/``. This module imports nothing outside
the standard library so neither half gains a dependency on the other.

The contract
============

**Status vocabulary** (lowercase, closed set)::

    pass | warn | fail | inco

``warn`` is advisory and never fails a run. ``inco`` is *inconclusive* — a check
that could not reach its dependency or blew its time cap. An inconclusive check
is NEVER a failure; conflating "I could not look" with "I looked and it is
broken" is how a slow box gets reported as a regression. The vocabulary is
lowercase because two of the three doctors already spoke lowercase and because
``select(.status == "fail")`` is what an agent types first.

**Exit ladder** — mirrors ``runtime_manager._shared.errors`` (pinned by tests)::

    0  ok            every check passed (or only warned / was inconclusive)
    1  error         the doctor could not produce a verdict at all
    2  usage         RESERVED for argparse; no doctor ever returns it deliberately
    3  needs input   a mutation was requested and confirmation is missing
    4  drift         the doctor RAN and found failing checks

Exit 4 is the load-bearing one: "ran fine, found a difference" is a
success-with-verdict, not an error, and it must be distinguishable from "the
doctor itself blew up" (1) and from "your invocation was wrong" (2).

**Envelope**::

    {
      "ok":             bool,          # no failing checks
      "exit_code":      int,           # the code the process will return
      "schema_version": str,
      "tool":           str,           # which doctor produced this
      "checks":         [ finding, ... ],
      "summary":        {"total","pass","warn","fail","inco", ...},
      "next_actions":   [str, ...],
      "coverage":       {...},         # which sibling doctors did NOT run
      "fix":            {...}          # the --fix contract for THIS doctor
    }

**Finding**::

    {"code", "status", "message", "details", "fix_command", "fixable"}

``fix_command`` is present on every finding — including passing ones — because
the field is how an agent learns the remediation vocabulary before it needs it.
``fixable`` states whether ``--fix`` can execute that command unattended; see
:mod:`doctor_fix` semantics in :func:`fix_contract`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------- #
# Schema + vocabulary
# --------------------------------------------------------------------------- #

#: Bumped whenever the envelope gains/renames/drops a top-level or finding key.
DOCTOR_SCHEMA_VERSION = "2026-08-14+doctor-family.v1"

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_INCO = "inco"

#: The closed vocabulary, in escalation order. Anything outside it is a bug.
STATUSES: tuple[str, ...] = (STATUS_PASS, STATUS_WARN, STATUS_INCO, STATUS_FAIL)

#: Only these two vocabularies ever shipped; both fold into the family one.
_STATUS_ALIASES: dict[str, str] = {
    "pass": STATUS_PASS,
    "ok": STATUS_PASS,
    "passed": STATUS_PASS,
    "warn": STATUS_WARN,
    "warning": STATUS_WARN,
    "fail": STATUS_FAIL,
    "failed": STATUS_FAIL,
    "error": STATUS_FAIL,
    "inco": STATUS_INCO,
    "inconclusive": STATUS_INCO,
    "skip": STATUS_INCO,
    "skipped": STATUS_INCO,
}

# --------------------------------------------------------------------------- #
# Exit ladder — a LITERAL MIRROR of runtime_manager._shared.errors
# --------------------------------------------------------------------------- #
#
# scripts/04-reconcile.py cannot import runtime_manager, so the ladder has to be
# restated somewhere both halves can read. tests/test_doctor_contract.py pins
# every constant here against the real one so the two cannot drift.

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NEEDS_INPUT = 3
EXIT_DRIFT = 4


def normalize_status(status: Any) -> str:
    """Fold any historical status spelling into the family vocabulary.

    Accepts the uppercase ``PASS/FAIL/INCO`` the structure doctor shipped, the
    lowercase ``pass/warn/fail`` the runtime doctors shipped, and plain booleans
    (the cass doctor's ``{"ok": true}`` shape). An unrecognized value is
    ``inco`` — "I cannot read this verdict" is inconclusive, never a silent pass.
    """
    if isinstance(status, bool):
        return STATUS_PASS if status else STATUS_FAIL
    text = str(status or "").strip().lower()
    return _STATUS_ALIASES.get(text, STATUS_INCO)


def display_status(status: str) -> str:
    """Uppercase spelling for HUMAN text output only. JSON stays lowercase."""
    return normalize_status(status).upper()


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    """One doctor finding in the family shape.

    ``code`` is the stable id an agent alerts on. ``fix_command`` is the exact
    copy-pasteable remediation (present even on ``pass`` findings, so the
    remediation vocabulary is discoverable before it is needed). ``fixable``
    says whether ``--fix`` may execute that command unattended; ``fix_reason``
    explains a ``False`` so "no autofix" is never mysterious.
    """

    code: str
    status: str
    message: str
    details: dict[str, Any] | None = None
    fix_command: str | None = None
    fixable: bool = False
    fix_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "status": normalize_status(self.status),
            "message": self.message,
            "details": self.details,
            "fix_command": self.fix_command,
            "fixable": bool(self.fixable),
        }
        if self.fix_reason:
            payload["fix_reason"] = self.fix_reason
        payload.update(self.extra)
        return payload


def finding_from_obj(obj: Any) -> Finding:
    """Adapt any dataclass/mapping with ``status``/``code`` into a :class:`Finding`.

    Both ``CheckResult`` variants (``_shared/errors.CheckResult`` and the outer
    reconcile script's own) satisfy this without either importing the other.
    """
    if isinstance(obj, Mapping):
        get = obj.get
    else:
        def get(name: str, default: Any = None) -> Any:
            return getattr(obj, name, default)
    return Finding(
        code=str(get("code", "") or get("name", "") or "unknown"),
        status=normalize_status(get("status")),
        message=str(get("message", "") or get("detail", "") or ""),
        details=get("details"),
        fix_command=get("fix_command"),
        fixable=bool(get("fixable", False)),
        fix_reason=str(get("fix_reason", "") or ""),
    )


def summarize(findings: Iterable[Finding]) -> dict[str, int]:
    """Counts per status plus ``total``. Always carries all four keys."""
    items = list(findings)
    counts = {status: 0 for status in STATUSES}
    for item in items:
        counts[normalize_status(item.status)] += 1
    return {"total": len(items), **counts}


def exit_code_for(findings: Iterable[Finding]) -> int:
    """``EXIT_DRIFT`` if anything failed, else ``EXIT_OK``.

    ``warn`` and ``inco`` deliberately exit 0: an advisory and an unreachable
    dependency are not regressions, and a doctor that redlines on either
    trains agents to ignore its exit code.
    """
    return EXIT_DRIFT if any(normalize_status(f.status) == STATUS_FAIL for f in findings) else EXIT_OK


# --------------------------------------------------------------------------- #
# Family routing — one table, rendered by every doctor's `coverage` field
# --------------------------------------------------------------------------- #

#: Symptom -> doctor. Pass-2 finding F-doc-05: routing knowledge was smeared
#: across four docs and three capabilities surfaces and no command answered
#: "which doctor for this symptom". Every doctor now ships this table.
FAMILY: tuple[dict[str, str], ...] = (
    {"doctor": "sbp doctor", "symptom": "structural gates: policy/lock/MCP parity, skill drift, git hygiene (FRONT DOOR — superset)"},
    {"doctor": "make doctor", "symptom": "outer manifest, compose wiring, skill-repo sync drift"},
    {"doctor": "python3 .env-manager/manage.py doctor", "symptom": "runtime graph, service/env/artifact readiness, installed-skill integrity"},
    {"doctor": "sbp registry doctor", "symptom": "registry/repos.yaml vs the on-disk git estate"},
    {"doctor": "sbp cass doctor", "symptom": "remote Cass index health"},
    {"doctor": "sbp send-later doctor", "symptom": "scheduler tick/queue health"},
    {"doctor": "sbp beads status", "symptom": "beads db/jsonl health"},
    {"doctor": "make self-test", "symptom": "canonical CI gate against an exact SHA"},
)

FRONT_DOOR = "sbp doctor"


def coverage_block(
    *,
    tool: str,
    includes: Iterable[str],
    siblings_not_run: Iterable[str] | None = None,
) -> dict[str, Any]:
    """The ``coverage`` field: what THIS run covered, and where else to look.

    ``siblings_not_run`` names the doctors by command; the symptom text is
    looked up from :data:`FAMILY` so every doctor prints the same routing words.
    """
    names = list(siblings_not_run) if siblings_not_run is not None else [
        entry["doctor"] for entry in FAMILY if entry["doctor"] != tool
    ]
    by_name = {entry["doctor"]: entry for entry in FAMILY}
    return {
        "front_door": FRONT_DOOR,
        "tool": tool,
        "includes": list(includes),
        "siblings_not_run": [
            by_name.get(name, {"doctor": name, "symptom": ""}) for name in names
        ],
    }


# --------------------------------------------------------------------------- #
# The --fix contract block
# --------------------------------------------------------------------------- #


def fix_contract(
    *,
    supported: bool,
    reason: str = "",
    fix_flag: str = "--fix",
    confirm_flag: str = "--yes",
    undo_flag: str = "--undo",
    artifact_dir: str | None = None,
    fixable_codes: Iterable[str] = (),
) -> dict[str, Any]:
    """Describe THIS doctor's ``--fix`` capability, in or out.

    A doctor with nothing mechanically fixable still ships this block with
    ``supported: false`` and a ``reason`` — the audit's complaint was that
    "no ``--fix``" was invisible, not that every doctor must grow one. An
    honest, machine-readable "this doctor diagnoses only, because <reason>" is
    a contract; silence is not.
    """
    payload: dict[str, Any] = {
        "supported": bool(supported),
        "dry_run_by_default": True,
        "confirmation_required": True,
    }
    if supported:
        payload.update(
            {
                "preview": f"{fix_flag} (no {confirm_flag}) prints the plan, writes a run artifact, and exits {EXIT_NEEDS_INPUT}",
                "apply": f"{fix_flag} {confirm_flag} takes a backup, executes each fixable finding's fix_command, and records the result",
                "undo": (
                    f"{undo_flag} <run-artifact.json> plans the restore and exits {EXIT_NEEDS_INPUT}; "
                    f"{undo_flag} <run-artifact.json> {confirm_flag} restores the backups and removes "
                    "what the fix created, refusing any path that has changed since"
                ),
                "fixable_codes": sorted(set(fixable_codes)),
            }
        )
        if artifact_dir:
            payload["run_artifact_dir"] = artifact_dir
    else:
        payload["reason"] = reason or "no finding in this doctor has a mechanically executable remediation"
        payload["dry_run_by_default"] = True
    return payload


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #


def doctor_envelope(
    *,
    tool: str,
    findings: Iterable[Finding],
    next_actions: Iterable[str] = (),
    coverage: Mapping[str, Any] | None = None,
    fix: Mapping[str, Any] | None = None,
    summary_extra: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    exit_code: int | None = None,
) -> dict[str, Any]:
    """Render the ONE family envelope.

    ``exit_code`` is computed from the findings unless the caller overrides it
    (``--fix`` without confirmation overrides it to ``EXIT_NEEDS_INPUT``).
    """
    items = [f if isinstance(f, Finding) else finding_from_obj(f) for f in findings]
    summary = summarize(items)
    if summary_extra:
        summary.update(dict(summary_extra))
    code = exit_code_for(items) if exit_code is None else int(exit_code)
    payload: dict[str, Any] = {
        "ok": summary[STATUS_FAIL] == 0,
        "exit_code": code,
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "tool": tool,
        "checks": [item.to_payload() for item in items],
        "summary": summary,
        "next_actions": list(next_actions),
    }
    if coverage is not None:
        payload["coverage"] = dict(coverage)
    if fix is not None:
        payload["fix"] = dict(fix)
    if extra:
        payload.update(dict(extra))
    return payload


# --------------------------------------------------------------------------- #
# Human text — the envelope's counterpart, deliberately NOT JSON-shaped
# --------------------------------------------------------------------------- #


def summary_line(summary: Mapping[str, Any]) -> str:
    """The one-line verdict every doctor prints last."""
    return (
        f"summary: {summary.get(STATUS_PASS, 0)} passed, "
        f"{summary.get(STATUS_WARN, 0)} warnings, "
        f"{summary.get(STATUS_INCO, 0)} inconclusive, "
        f"{summary.get(STATUS_FAIL, 0)} failed"
    )


def routing_line(tool: str) -> str:
    """The 'not covered here' pointer (pass-2 finding F-doc-04).

    Printed BEFORE the summary so the summary stays the last meaningful line —
    the structure doctor's ``runtime_doctor`` gate reports exactly that line.
    """
    if tool == FRONT_DOOR:
        return "front door: this run is the doctor-family superset; satellites listed under coverage.siblings_not_run"
    return f"structural gates not checked here — front door: {FRONT_DOOR} --format json"


__all__ = [
    "DOCTOR_SCHEMA_VERSION",
    "STATUS_PASS",
    "STATUS_WARN",
    "STATUS_FAIL",
    "STATUS_INCO",
    "STATUSES",
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_USAGE",
    "EXIT_NEEDS_INPUT",
    "EXIT_DRIFT",
    "FAMILY",
    "FRONT_DOOR",
    "Finding",
    "coverage_block",
    "display_status",
    "doctor_envelope",
    "exit_code_for",
    "finding_from_obj",
    "fix_contract",
    "normalize_status",
    "routing_line",
    "summarize",
    "summary_line",
]
