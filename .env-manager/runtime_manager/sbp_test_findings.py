"""``suite-readiness/v1``: finding codes, evidence states, and the readiness report.

This module is the *contract layer* of the repo-readiness lane
(``skillbox-sbp-test-finding-registry-yxm7``). It holds no adapters and reads no
repository: it defines what may be said about a suite, with what evidence, and
what any of it is allowed to gate. Adapters (``make -qp``, ``package.json``,
pytest config, compose files) and the refactoring skill are separate slices that
both bind to the names frozen here.

The merged duel contract (CC's finding codes x COD's evidence ladder):

* **Stable finding codes are the API.** Every code has exactly one skill recipe
  and every recipe maps back to exactly one code; drift in either direction is a
  test failure (:func:`validate_recipe_catalog`).
* **Codes bind invariants, not one repository's naming.** The code is
  ``EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING`` because the invariant is "somebody
  other than the suite owns serialization" -- not ``LOCK_SEAM_MISSING`` matching
  a literal ``X`` / ``X-unlocked`` target pair, which is one repo's spelling of
  it. Same reason for ``CROSS_MACHINE_PARTITION_MISSING`` over
  ``SHARD_VOCAB_MISSING`` (xdist already shards *within* a machine, so the
  missing capability had to be named precisely). Codes renamed from their duel
  originals keep :attr:`CodeSpec.duel_origin` so the lineage stays traceable.
* **Evidence states, not a magic percentage:** ``proven`` / ``likely`` /
  ``unknown`` / ``blocked`` / ``not_applicable``.
* **Unknown never becomes a pass.** Structurally, not by convention: a pass
  (:class:`Cleared`) is unrepresentable without evidence, ``proven`` and
  ``likely`` are unrepresentable without evidence, and a v1 code that a report
  simply *omits* is filled in as ``unknown`` rather than silently counting as
  clean. Omission is the most likely way a scorer would lie.
* **Only named, evidenced hard blockers gate.** A gate decision names the
  ``proven`` findings whose :attr:`CodeSpec.blocks` denies the intent being
  requested. ``likely`` never gates; ``unknown`` never gates *and* never passes,
  so admission for a lesser intent stays possible while the readiness class
  stays honest (a repo with unknown parallel safety and a valid serial oracle is
  still admissible at concurrency one).
* **The rollup is advisory.** "The scorer is the gate / convergence is a number"
  was withdrawn by its originator during the duel: Goodhart pressure on agents
  is real, and a number an agent can optimize becomes the thing it optimizes.
  :func:`build_report` still emits a deterministic score because humans want one
  -- flagged ``advisory``, with authority recorded as the blocker list.

**The ten-axis model is the long-term shape; v1 covers seven of it.** :data:`AXES`
records all ten dimensions from the duel. The v1 code set reaches seven of them;
``resource_declaration``, ``source_fidelity`` and ``failure_cleanup`` have no v1
code and are reported as ``not_covered_in_v1`` rather than as clean. A report
therefore cannot be read as a whole-model verdict -- which is why the readiness
class is named :data:`V1_READINESS_KEY` and the coverage block ships in every
report.

Standard library only. No CLI surface, no MCP mirror, no manifest coupling.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_ID = "suite-readiness/v1"
SCHEMA_VERSION = 1

V1_READINESS_KEY = "v1_readiness_class"


# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #

#: Evidence states, from the duel's ladder. Order is the ladder order.
STATUSES: tuple[str, ...] = ("proven", "likely", "unknown", "blocked", "not_applicable")

#: States that assert a violation and therefore require evidence.
ASSERTING_STATUSES: frozenset[str] = frozenset({"proven", "likely"})

#: States that are not a claim about the world and therefore require a reason.
NON_ASSERTING_STATUSES: frozenset[str] = frozenset({"unknown", "blocked", "not_applicable"})

#: Severity is operator triage ordering. It is deliberately NOT the gate --
#: gating is `status == "proven"` plus `blocks`, so nobody can gate a suite by
#: relabelling a finding "high".
SEVERITIES: tuple[str, ...] = ("high", "medium", "low")

#: What a violation denies. "optimization-only" exists so a finding can be real,
#: proven, and still not gate anything -- an `&&` chain can be intentionally
#: serial and correct.
BLOCKS_VALUES: tuple[str, ...] = (
    "any-execution",
    "remote",
    "parallel",
    "caching",
    "optimization-only",
)

#: Capability intents a consumer can ask admission for.
INTENTS: tuple[str, ...] = ("local", "remote", "parallel", "caching")

#: Which intents each `blocks` value denies. Deliberately not a total order:
#: "cannot leave this machine" says nothing about local concurrency, and
#: "cannot be cached" says nothing about either.
DENIED_INTENTS: Mapping[str, tuple[str, ...]] = {
    "any-execution": ("local", "remote", "parallel", "caching"),
    "remote": ("remote",),
    "parallel": ("parallel",),
    "caching": ("caching",),
    "optimization-only": (),
}

#: How a claim was established. `absent` is how "there is no such target
#: anywhere" is evidenced -- a missing thing has no file:line.
EVIDENCE_KINDS: tuple[str, ...] = ("file", "parsed_target", "probe", "absent")

_FILE_EVIDENCE = re.compile(r"^[^/][^:]*:[0-9]+$")

#: Readiness classes over the v1 subset only.
READINESS_CLASSES: tuple[str, ...] = ("blocked", "bounded", "ready")

#: Per-axis states. `not_covered_in_v1` is a fourth state on purpose: an axis
#: nothing looked at must not render like an axis that came back clean.
AXIS_STATES: tuple[str, ...] = ("blocked", "bounded", "ready", "not_covered_in_v1")

_UNEVALUATED_REASON = "no v1 adapter evaluated this code in this report"


# --------------------------------------------------------------------------- #
# The ten-axis model (long-term); v1 covers the subset that has codes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Axis:
    """One readiness dimension. The ten are the long-term model, frozen here."""

    id: str
    ordinal: int
    title: str
    question: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ordinal": self.ordinal,
            "title": self.title,
            "question": self.question,
            "v1_covered": self.id in V1_AXIS_IDS,
            "codes": sorted(spec.code for spec in CODES.values() if spec.axis == self.id),
        }


AXES: tuple[Axis, ...] = (
    Axis(
        "entrypoint_clarity",
        1,
        "Entrypoint clarity",
        "Are there named, non-interactive commands with stable exit semantics?",
    ),
    Axis(
        "selection_completeness",
        2,
        "Selection completeness",
        "Do the declared units cover the serial oracle, with explicit exclusions?",
    ),
    Axis(
        "workspace_isolation",
        3,
        "Workspace isolation",
        "Are temp/output/cache paths and repo-root assumptions per-run and portable?",
    ),
    Axis(
        "service_isolation",
        4,
        "Service isolation",
        "Are databases, caches and containers namespaced, owned, health-checked and torn down?",
    ),
    Axis(
        "concurrency_safety",
        5,
        "Concurrency safety",
        "Do shared files, singleton locks, global ports or order dependence break concurrent runs?",
    ),
    Axis(
        "determinism",
        6,
        "Determinism",
        "Do repeated runs agree, with fixed seeds and no unpinned time/network/image inputs?",
    ),
    Axis(
        "resource_declaration",
        7,
        "Resource declaration",
        "Are OS, architecture, tooling, memory/disk, secrets and network posture declared?",
    ),
    Axis(
        "observability",
        8,
        "Observability",
        "Are exit codes, logs, reports and artifact paths machine-readable and bounded?",
    ),
    Axis(
        "source_fidelity",
        9,
        "Source fidelity",
        "Does the suite behave on a dirty tree, with generated prerequisites and pinned toolchains?",
    ),
    Axis(
        "failure_cleanup",
        10,
        "Failure cleanup",
        "On timeout or failure, are child processes, services and workspaces actually cleaned up?",
    ),
)

AXES_BY_ID: Mapping[str, Axis] = {axis.id: axis for axis in AXES}


# --------------------------------------------------------------------------- #
# The v1 finding-code registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CodeSpec:
    """One stable finding code. ``code`` and ``recipe_id`` are the frozen API."""

    code: str
    axis: str
    invariant: str
    detects: str
    default_severity: str
    blocks: str
    recipe_id: str
    #: Whether proposing a `.skillbox/test.yaml` fragment for this finding can
    #: ever be safe. False where the fix must land in the repo first -- a
    #: fragment declaring a lane that does not exist yet is a fabrication.
    fragment_safe: bool = False
    #: The duel's original name, when this code was renamed to bind the
    #: invariant instead of one repository's vocabulary.
    duel_origin: str | None = None

    def __post_init__(self) -> None:
        if self.axis not in AXES_BY_ID:
            raise ValueError(f"{self.code}: unknown axis {self.axis!r}")
        if self.default_severity not in SEVERITIES:
            raise ValueError(f"{self.code}: unknown severity {self.default_severity!r}")
        if self.blocks not in BLOCKS_VALUES:
            raise ValueError(f"{self.code}: unknown blocks value {self.blocks!r}")

    @property
    def denied_intents(self) -> tuple[str, ...]:
        return DENIED_INTENTS[self.blocks]

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "axis": self.axis,
            "invariant": self.invariant,
            "detects": self.detects,
            "default_severity": self.default_severity,
            "blocks": self.blocks,
            "denied_intents": list(self.denied_intents),
            "recipe_id": self.recipe_id,
            "fragment_safe": self.fragment_safe,
            "duel_origin": self.duel_origin,
        }


_CODE_SPECS: tuple[CodeSpec, ...] = (
    CodeSpec(
        code="SERVICE_FREE_LANE_MISSING",
        axis="service_isolation",
        invariant="at least one declared lane runs to completion with no external service",
        detects="every declared lane needs a database, cache or container to start",
        default_severity="high",
        blocks="parallel",
        recipe_id="suite-refactor/service-free-lane",
        duel_origin="UNIT_DB_FREE_MISSING",
    ),
    CodeSpec(
        code="SERVICE_REQUIREMENT_UNDERIVED",
        axis="selection_completeness",
        invariant="a test's service requirement is derived from what it requests, not hand-labelled",
        detects="service markers are maintained by hand, so the service-free lane drifts silently",
        default_severity="medium",
        blocks="parallel",
        recipe_id="suite-refactor/derive-service-requirements",
        duel_origin="MARKERS_HAND_ANNOTATED",
    ),
    CodeSpec(
        code="SERVICE_ENDPOINT_STATIC",
        axis="concurrency_safety",
        invariant="service endpoints are allocated per run and injected by environment",
        detects="a fixed port or fixed socket path makes two concurrent runs collide",
        default_severity="high",
        blocks="parallel",
        recipe_id="suite-refactor/dynamic-service-endpoints",
        duel_origin="SERVICES_STATIC_PORT",
    ),
    CodeSpec(
        code="SERVICE_IMAGES_UNPINNED",
        axis="determinism",
        invariant="every external service image is pinned by digest",
        detects="a floating tag means two runs of the same tree can test different software",
        default_severity="medium",
        blocks="caching",
        recipe_id="suite-refactor/pin-service-images",
    ),
    CodeSpec(
        code="EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING",
        axis="concurrency_safety",
        invariant="the suite exposes a way to run its work without taking its own global lock",
        detects="the only entrypoint serializes itself, so an external scheduler cannot own ordering",
        default_severity="high",
        blocks="parallel",
        recipe_id="suite-refactor/external-scheduler-seam",
        duel_origin="LOCK_SEAM_MISSING",
    ),
    CodeSpec(
        code="TARGET_MONOLITHIC",
        axis="entrypoint_clarity",
        invariant="the declared gate is separately invocable lanes, not one chain of phases",
        detects="the gate is an && chain, so a phase cannot be placed or retried on its own",
        default_severity="medium",
        blocks="optimization-only",
        recipe_id="suite-refactor/split-monolithic-target",
    ),
    CodeSpec(
        code="CROSS_MACHINE_PARTITION_MISSING",
        axis="selection_completeness",
        invariant="the suite can be partitioned into disjoint subsets addressable from the CLI",
        detects="the big suite has no cross-machine partition vocabulary (in-process sharding is not one)",
        default_severity="high",
        blocks="optimization-only",
        recipe_id="suite-refactor/cross-machine-partition",
        duel_origin="SHARD_VOCAB_MISSING",
    ),
    CodeSpec(
        code="RECEIPT_NOT_COMPOSABLE",
        axis="observability",
        invariant="proof of a run composes from per-unit evidence",
        detects="proof exists only whole-tree, so a fanned-out run cannot be aggregated honestly",
        default_severity="high",
        blocks="parallel",
        recipe_id="suite-refactor/composable-receipts",
        fragment_safe=True,
    ),
    CodeSpec(
        code="PATH_FRAGILE",
        axis="workspace_isolation",
        invariant="no target depends on an absolute path or a path that escapes the repo root",
        detects="../.. or absolute paths threaded through targets break the moment the tree moves",
        default_severity="high",
        blocks="remote",
        recipe_id="suite-refactor/repo-relative-paths",
        fragment_safe=True,
    ),
    CodeSpec(
        code="PACKAGE_LANES_UNENUMERATED",
        axis="selection_completeness",
        invariant="every test-bearing package is reachable from a declared aggregate entrypoint",
        detects="test packages exist that no declared entrypoint runs, so the unit union is not the suite",
        default_severity="medium",
        blocks="parallel",
        recipe_id="suite-refactor/enumerate-package-lanes",
        fragment_safe=True,
        duel_origin="JS_AGGREGATOR_MISSING",
    ),
)

CODES: Mapping[str, CodeSpec] = {spec.code: spec for spec in _CODE_SPECS}

#: The recipe namespace the refactoring skill must implement, one per code.
RECIPE_IDS: frozenset[str] = frozenset(spec.recipe_id for spec in _CODE_SPECS)

#: Axes reachable by a v1 code. The other three are the honest remainder.
V1_AXIS_IDS: frozenset[str] = frozenset(spec.axis for spec in _CODE_SPECS)

#: Axes in the long-term model that v1 does not evaluate at all.
UNCOVERED_AXIS_IDS: frozenset[str] = frozenset(axis.id for axis in AXES) - V1_AXIS_IDS


def _assert_registry_is_bijective() -> None:
    """Import-time guard: a duplicate code or recipe id is a broken contract."""
    if len(CODES) != len(_CODE_SPECS):
        raise ValueError("duplicate finding code in the registry")
    if len(RECIPE_IDS) != len(_CODE_SPECS):
        raise ValueError("two codes share one recipe id; the mapping must be one-to-one")


_assert_registry_is_bijective()


# --------------------------------------------------------------------------- #
# Evidence and findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Evidence:
    """Deterministic evidence: a repo-relative file:line, a parsed target, or a probe receipt."""

    kind: str
    locator: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence kind {self.kind!r}")
        if not self.locator.strip():
            raise ValueError("evidence needs a locator")
        if self.kind == "file" and not _FILE_EVIDENCE.match(self.locator):
            raise ValueError(
                f"file evidence must be a repo-relative 'path:line', got {self.locator!r}"
            )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "locator": self.locator}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class Finding:
    """One evaluated finding code with its evidence state.

    Construction is the contract check: an unregistered code, an asserting
    status without evidence, a non-asserting status without a reason, and a
    manifest fragment attached to a code that can never safely carry one are all
    unrepresentable.
    """

    finding_code: str
    status: str
    evidence: tuple[Evidence, ...] = ()
    affected_units: tuple[str, ...] = ()
    reason: str | None = None
    severity: str | None = None
    proposed_fragment: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.finding_code not in CODES:
            raise ValueError(
                f"unknown finding code {self.finding_code!r}; "
                "every emitted code must be registered (and therefore have a recipe)"
            )
        if self.status not in STATUSES:
            raise ValueError(f"unknown evidence state {self.status!r}")
        if self.status in ASSERTING_STATUSES and not self.evidence:
            raise ValueError(
                f"{self.finding_code}: status {self.status!r} asserts a violation and "
                "cannot be stated without evidence"
            )
        if self.status in NON_ASSERTING_STATUSES and not (self.reason or "").strip():
            raise ValueError(
                f"{self.finding_code}: status {self.status!r} needs a reason; "
                "an unexplained unknown is indistinguishable from an unasked question"
            )
        if self.severity is None:
            object.__setattr__(self, "severity", self.spec.default_severity)
        elif self.severity not in SEVERITIES:
            raise ValueError(f"{self.finding_code}: unknown severity {self.severity!r}")
        if self.proposed_fragment is not None:
            if not self.spec.fragment_safe:
                raise ValueError(
                    f"{self.finding_code}: proposing a manifest fragment for this code is "
                    "never safe; the repo change lands first"
                )
            if self.status not in ASSERTING_STATUSES:
                raise ValueError(
                    f"{self.finding_code}: a fragment proposed from a {self.status!r} finding "
                    "would be a guess"
                )
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "affected_units", tuple(self.affected_units))

    @property
    def spec(self) -> CodeSpec:
        return CODES[self.finding_code]

    @property
    def axis(self) -> str:
        return self.spec.axis

    @property
    def blocks(self) -> str:
        return self.spec.blocks

    @property
    def recipe_id(self) -> str:
        return self.spec.recipe_id

    def gates(self, intent: str) -> bool:
        """Only a proven violation whose `blocks` denies this intent is a gate."""
        if intent not in INTENTS:
            raise ValueError(f"unknown intent {intent!r}")
        return self.status == "proven" and intent in self.spec.denied_intents

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "finding_code": self.finding_code,
            "status": self.status,
            "severity": self.severity,
            "axis": self.axis,
            "blocks": self.blocks,
            "recipe_id": self.recipe_id,
            "evidence": [item.to_payload() for item in self.evidence],
            "affected_units": list(self.affected_units),
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.proposed_fragment is not None:
            payload["proposed_fragment"] = dict(self.proposed_fragment)
        return payload


@dataclass(frozen=True)
class Cleared:
    """A code positively established as satisfied. Evidence is mandatory.

    This is the structural half of "unknown never becomes a pass": there is no
    way to express a clean code without saying what made it clean.
    """

    finding_code: str
    evidence: tuple[Evidence, ...]
    note: str | None = None

    def __post_init__(self) -> None:
        if self.finding_code not in CODES:
            raise ValueError(f"unknown finding code {self.finding_code!r}")
        if not self.evidence:
            raise ValueError(f"{self.finding_code}: a pass must carry evidence")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def spec(self) -> CodeSpec:
        return CODES[self.finding_code]

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "finding_code": self.finding_code,
            "axis": self.spec.axis,
            "evidence": [item.to_payload() for item in self.evidence],
        }
        if self.note is not None:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class Subject:
    """What the report is about. ``capsule_digest`` binds a report to bytes."""

    label: str
    capsule_digest: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"label": self.label, "capsule_digest": self.capsule_digest}


# --------------------------------------------------------------------------- #
# Registry payload (the frozen v1 artifact)
# --------------------------------------------------------------------------- #


def registry_payload() -> dict[str, Any]:
    """The whole frozen contract, as data. Pinned by a fixture."""
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "axis_model": {
            "axes": [axis.to_payload() for axis in AXES],
            "axes_total": len(AXES),
            "v1_covered_axis_ids": sorted(V1_AXIS_IDS),
            "not_covered_in_v1_axis_ids": sorted(UNCOVERED_AXIS_IDS),
        },
        "codes": [CODES[code].to_payload() for code in sorted(CODES)],
        "recipe_ids": sorted(RECIPE_IDS),
        "vocabularies": {
            "statuses": list(STATUSES),
            "severities": list(SEVERITIES),
            "blocks": list(BLOCKS_VALUES),
            "intents": list(INTENTS),
            "denied_intents": {key: list(value) for key, value in sorted(DENIED_INTENTS.items())},
            "evidence_kinds": list(EVIDENCE_KINDS),
            "readiness_classes": list(READINESS_CLASSES),
            "axis_states": list(AXIS_STATES),
        },
        "authority": {
            "gating": "only a proven finding whose blocks denies the requested intent",
            "likely_gates": False,
            "unknown_gates": False,
            "unknown_passes": False,
            "omission_is_unknown": True,
            "rollup": "advisory",
            "rollup_authority_note": (
                "the numeric rollup never gates; 'the scorer is the gate' was withdrawn "
                "post-reveal because a number an agent can optimize becomes the target"
            ),
            "severity_gates": False,
            "readiness_scope": "the v1 code subset only, never the ten-axis model",
        },
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _sort_key(code: str) -> tuple[int, str]:
    return (AXES_BY_ID[CODES[code].axis].ordinal, code)


def _fill_unevaluated(
    findings: Sequence[Finding], cleared: Sequence[Cleared]
) -> tuple[Finding, ...]:
    """Any registered code the report did not evaluate becomes an explicit unknown.

    Silence is the cheapest lie a scorer can tell, so it is not available.
    """
    seen = {item.finding_code for item in findings} | {item.finding_code for item in cleared}
    filled = list(findings)
    for code in sorted(set(CODES) - seen, key=_sort_key):
        filled.append(Finding(code, "unknown", reason=_UNEVALUATED_REASON))
    return tuple(filled)


def _axis_state(axis_id: str, findings: Sequence[Finding]) -> str:
    if axis_id not in V1_AXIS_IDS:
        return "not_covered_in_v1"
    axis_findings = [f for f in findings if f.axis == axis_id]
    if any(f.status == "proven" and f.spec.denied_intents for f in axis_findings):
        return "blocked"
    if any(f.status != "not_applicable" for f in axis_findings):
        return "bounded"
    return "ready"


def _rollup_score(findings: Sequence[Finding], cleared: Sequence[Cleared]) -> dict[str, Any]:
    """Deterministic, integral, and advisory.

    ``not_applicable`` leaves the denominator instead of scoring: a repo should
    not gain points for having fewer things that can go wrong. ``likely``
    earns partial credit because it is real evidence that is not proof.
    """
    not_applicable = sum(1 for f in findings if f.status == "not_applicable")
    applicable = len(CODES) - not_applicable
    earned = 100 * len(cleared) + 40 * sum(1 for f in findings if f.status == "likely")
    score = 1000 if applicable == 0 else (earned * 1000) // (applicable * 100)
    return {
        "score": score,
        "max": 1000,
        "applicable_codes": applicable,
        "advisory": True,
        "authority": "named_evidenced_blockers",
    }


def _next_actions(findings: Sequence[Finding]) -> list[str]:
    ordered = sorted(findings, key=lambda f: _sort_key(f.finding_code))
    blocking = [f for f in ordered if f.status == "proven" and f.spec.denied_intents]
    unresolved = [f for f in ordered if f.status in ("unknown", "blocked")]
    likely = [f for f in ordered if f.status == "likely"]
    optional = [f for f in ordered if f.status == "proven" and not f.spec.denied_intents]
    actions = [
        f"apply {f.recipe_id} to clear {f.finding_code} (blocks {f.blocks})" for f in blocking
    ]
    actions += [
        f"resolve {f.finding_code} ({f.status}): {f.reason}" for f in unresolved
    ]
    actions += [
        f"confirm {f.finding_code} with deterministic evidence, then apply {f.recipe_id}"
        for f in likely
    ]
    actions += [
        f"optional: apply {f.recipe_id} to clear {f.finding_code} (optimization only)"
        for f in optional
    ]
    return actions


def build_report(
    subject: Subject,
    findings: Iterable[Finding] = (),
    cleared: Iterable[Cleared] = (),
) -> dict[str, Any]:
    """Assemble a deterministic ``suite-readiness/v1`` report.

    Deterministic means: no timestamps, no absolute paths, no host state, and a
    total order on every list -- the same inputs render the same bytes, which is
    what makes before/after comparison meaningful.
    """
    findings = tuple(findings)
    cleared = tuple(cleared)

    counted: dict[str, int] = {}
    for item in list(findings) + list(cleared):
        counted[item.finding_code] = counted.get(item.finding_code, 0) + 1
    duplicated = sorted(code for code, count in counted.items() if count > 1)
    if duplicated:
        raise ValueError(
            "a code is evaluated exactly once per report; duplicated: " + ", ".join(duplicated)
        )

    findings = _fill_unevaluated(findings, cleared)
    ordered = sorted(findings, key=lambda f: _sort_key(f.finding_code))
    ordered_cleared = sorted(cleared, key=lambda c: _sort_key(c.finding_code))

    gates: dict[str, Any] = {}
    for intent in INTENTS:
        blocked_by = [f.finding_code for f in ordered if f.gates(intent)]
        unproven = [
            f.finding_code
            for f in ordered
            if f.status in ("unknown", "blocked") and intent in f.spec.denied_intents
        ]
        gates[intent] = {
            "admitted": not blocked_by,
            "blocked_by": blocked_by,
            "unproven_for_intent": unproven,
        }

    if any(f.status == "proven" and f.spec.denied_intents for f in ordered):
        readiness = "blocked"
    elif all(f.status == "not_applicable" for f in ordered):
        readiness = "ready"
    else:
        readiness = "bounded"

    counts = {status: sum(1 for f in ordered if f.status == status) for status in STATUSES}
    counts["cleared"] = len(ordered_cleared)

    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "subject": subject.to_payload(),
        "coverage": {
            "axes_total": len(AXES),
            "v1_covered": sorted(V1_AXIS_IDS),
            "not_covered_in_v1": sorted(UNCOVERED_AXIS_IDS),
            "note": "a v1 report is a claim about the covered axes only",
        },
        "axes": {
            axis.id: {
                "ordinal": axis.ordinal,
                "title": axis.title,
                "state": _axis_state(axis.id, ordered),
                "codes": sorted(
                    spec.code for spec in CODES.values() if spec.axis == axis.id
                ),
            }
            for axis in AXES
        },
        "findings": [f.to_payload() for f in ordered],
        "cleared": [c.to_payload() for c in ordered_cleared],
        "counts": counts,
        "rollup": _rollup_score(ordered, ordered_cleared),
        V1_READINESS_KEY: readiness,
        "gates": gates,
        "next_actions": _next_actions(ordered),
    }


def report_json(report: Mapping[str, Any]) -> str:
    """Byte-stable rendering. Same inputs, same bytes, on every host."""
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def is_comparable(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Two reports may be compared only when they are bound to the same bytes.

    Without a capsule digest an apparent improvement can come from scanning a
    different tree, so an unbound report is never comparable -- not even to
    itself.
    """
    digest = before.get("subject", {}).get("capsule_digest")
    return bool(digest) and digest == after.get("subject", {}).get("capsule_digest")


# --------------------------------------------------------------------------- #
# Code <-> recipe contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CatalogDrift:
    """One direction of code/recipe drift. Consumers treat any drift as failure."""

    kind: str
    subject: str
    message: str

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "message": self.message}


def validate_recipe_catalog(recipe_ids: Iterable[str]) -> list[CatalogDrift]:
    """Check a skill's recipe catalog against the registry, both directions.

    Every emitted code must have a recipe and every recipe must map back to a
    known code. One-directional checking is how a skill accumulates recipes for
    codes that no longer exist while a new code ships with no recipe at all.
    """
    catalog = set(recipe_ids)
    drift: list[CatalogDrift] = []
    for code in sorted(CODES, key=_sort_key):
        recipe = CODES[code].recipe_id
        if recipe not in catalog:
            drift.append(
                CatalogDrift(
                    "recipe_missing_for_code",
                    code,
                    f"{code} can be emitted but the catalog has no {recipe}",
                )
            )
    for recipe in sorted(catalog - RECIPE_IDS):
        drift.append(
            CatalogDrift(
                "recipe_without_code",
                recipe,
                f"{recipe} maps to no registered finding code",
            )
        )
    return drift
