"""``sbp test`` front door (skillbox-sbp-test-front-door-1y29).

`sbp test` normalizes the *infrastructure*, not the tests: a repo keeps its own
`make`/`pytest`/`cargo` commands and declares them once in a version-controlled
`.skillbox/test.yaml`, which this module reads.

Slice 1 deliberately ships only the **front door**:

* ``plan`` and ``lint`` are real and read-only. They parse the manifest and
  report what would run, so an agent can inspect a repo's test contract without
  side effects.
* ``run`` and ``dispatch`` are declared but not implemented. They return a typed
  ``not_implemented`` envelope rather than silently doing nothing. They are
  declared *now*, and classified as gated in the DCG policy, so agent hooks fail
  closed on day one instead of inheriting an allow when the executor lands.

The generic ``sbp-<cmd>`` PATH resolver is explicitly NOT used: that lookup seam
does not exist today, and an explicit verb avoids command-shadowing and
path-trust policy in this slice.

Schema, parsing and validation live in :mod:`runtime_manager.sbp_test_manifest`
(skillbox-sbp-test-manifest-v1-23t3); this module owns only the verb envelopes
and the exit-code mapping.
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from . import sbp_test_manifest as manifest_schema
from .sbp_test_manifest import (  # re-exported for callers/tests
    DEFAULT_GROUP,
    MANIFEST_RELPATH,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
)

SBP_TEST_SCHEMA_VERSION = "2026-08-16+sbp_test_front_door"

#: Verbs that only read the repo. Safe for an agent to call unprompted.
#: `score` inspects the repo itself rather than the manifest, and works with or
#: without one (skillbox-sbp-test-scorer-adapters-jyg2).
#:
#: `score` stays read-only here even though `--probe` executes, and that is a
#: recorded judgement rather than an oversight. DCG classifies *verbs*, and the
#: bare verb is genuinely read-only; the probe path's gate is its own fail-closed
#: authority set (:data:`PROBE_AUTHORITIES`), every element of which must be
#: explicit before anything runs. Reclassifying the verb would gate the static
#: score -- the safe, default, unattended path -- on the strength of a flag
#: almost nobody passes.
READ_ONLY_VERBS: tuple[str, ...] = ("plan", "lint", "score")

#: The five authorities `--probe` requires. Named here so the front door, the
#: CLI and the tests all agree on what "explicit" means.
PROBE_AUTHORITIES: tuple[str, ...] = (
    "capsule_workspace",
    "wall_clock_budget",
    "max_parallelism",
    "service_permission",
    "network_permission",
)
#: Verbs that write locally but execute nothing. `capsule` admits an archive
#: into the local content-addressed store; it never runs a test or leaves the
#: machine, so it is deliberately NOT in the DCG-gated set.
WRITE_VERBS: tuple[str, ...] = ("capsule",)
#: Verbs that will execute or fan out work. Gated in DCG from day one, even
#: though slice 1 does not implement them yet.
GATED_VERBS: tuple[str, ...] = ("run", "dispatch")
VERBS: tuple[str, ...] = READ_ONLY_VERBS + WRITE_VERBS + GATED_VERBS

__all__ = [
    "DEFAULT_GROUP",
    "GATED_VERBS",
    "MANIFEST_RELPATH",
    "PROBE_AUTHORITIES",
    "READ_ONLY_VERBS",
    "SBP_TEST_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "VERBS",
    "WRITE_VERBS",
    "capsule_payload",
    "deferred_payload",
    "lint_payload",
    "manifest_path",
    "plan_payload",
    "score_exit_class",
    "score_payload",
    "status_payload",
    "suggest_verb",
    "unknown_verb_payload",
]

#: Refusal codes that mean "the repo did not give us enough", as opposed to
#: "your input was wrong" or "we broke". Mapped to the exit ladder by the CLI.
#:
#: Probe-authority refusals live here too, and the reason is the contract: being
#: unable to run a probe is a *needs-input* refusal, never a test failure. The
#: caller is missing an authority it can supply; nothing about the suite was
#: learned. A probe that ran and found something is the opposite case and exits
#: ok, because a finding is data.
SCORE_NEEDS_INPUT_CODES: frozenset[str] = frozenset({"no_test_surface"})


def _probe_mode() -> Any:
    """Lazy import: the static score must not pay for the probe module."""
    from . import sbp_test_probe as probe_mode  # noqa: PLC0415

    return probe_mode


def suggest_verb(verb: str) -> str:
    """Closest known verb, or "" when nothing is close enough."""
    matches = difflib.get_close_matches(verb, sorted(VERBS), n=1, cutoff=0.55)
    return matches[0] if matches else ""


def manifest_path(cwd: Path) -> Path:
    return Path(cwd) / MANIFEST_RELPATH


def _envelope(verb: str, cwd: Path, *, ok: bool = True) -> dict[str, Any]:
    return {
        "ok": ok,
        "schema_version": SBP_TEST_SCHEMA_VERSION,
        "manifest_schema_version": SCHEMA_VERSION,
        "command": "test",
        "verb": verb,
        "cwd": str(Path(cwd)),
        "manifest_path": str(manifest_path(cwd)),
    }


def unknown_verb_payload(verb: str, cwd: Path) -> dict[str, Any]:
    """Typed did-you-mean payload for an unrecognized subcommand."""
    suggestion = suggest_verb(verb)
    payload = _envelope(verb, cwd, ok=False)
    payload["error"] = f"unknown `test` subcommand: {verb!r}"
    payload["error_code"] = "unknown_subcommand"
    payload["known_verbs"] = list(VERBS)
    payload["suggestion"] = suggestion or None
    next_actions = []
    if suggestion:
        next_actions.append(f"sbp test {suggestion} --format json")
    next_actions.append("sbp test --format json")
    payload["next_actions"] = next_actions
    return payload


def lint_payload(cwd: Path) -> dict[str, Any]:
    """Validate `.skillbox/test.yaml`. Read-only.

    Schema findings and drift findings are reported separately and never merged:
    a broken contract and a well-formed manifest that disagrees with this host
    are different problems with different exit codes.
    """
    cwd = Path(cwd)
    payload = _envelope("lint", cwd)
    manifest, issues = manifest_schema.load_manifest(cwd)
    payload["manifest_present"] = manifest_path(cwd).is_file()
    payload["issues"] = manifest_schema.findings_payload(issues)

    if manifest is None:
        payload["ok"] = False
        payload["drift"] = []
        payload["unit_count"] = 0
        payload["group_count"] = 0
        payload["next_actions"] = [f"create {MANIFEST_RELPATH}"]
        return payload

    drift = manifest_schema.detect_drift(manifest, cwd)
    payload["drift"] = manifest_schema.findings_payload(drift)
    payload["unit_count"] = len(manifest.units)
    payload["group_count"] = len(manifest.groups)
    payload["groups"] = sorted(manifest.groups)
    payload["ok"] = not issues and not drift
    if issues:
        payload["next_actions"] = [f"fix {MANIFEST_RELPATH}"]
    elif drift:
        payload["next_actions"] = ["install the missing commands, or fix the manifest"]
    else:
        payload["next_actions"] = ["sbp test plan --format json"]
    return payload


def plan_payload(cwd: Path, *, group: str | None = None) -> dict[str, Any]:
    """Compile the manifest into the ordered unit list a run would execute.

    Read-only: this resolves and reports, it never executes or schedules.
    """
    cwd = Path(cwd)
    group_name = group or DEFAULT_GROUP
    payload = _envelope("plan", cwd)
    payload["group"] = group_name
    manifest, issues = manifest_schema.load_manifest(cwd)
    if manifest is None:
        payload["ok"] = False
        payload["issues"] = manifest_schema.findings_payload(issues)
        payload["units"] = []
        payload["next_actions"] = [f"create {MANIFEST_RELPATH}"]
        return payload

    ordered, plan_issues = manifest_schema.compile_plan(manifest, group_name)
    all_issues = [*issues, *plan_issues]
    payload["issues"] = manifest_schema.findings_payload(all_issues)
    payload["known_groups"] = sorted(manifest.groups)
    payload["units"] = [unit.to_payload() for unit in ordered]
    payload["unit_count"] = len(ordered)
    payload["ok"] = not all_issues
    payload["next_actions"] = ["sbp test lint --format json"]
    if all_issues:
        return payload

    # The compiled plan is the execution authority; the unit list above is a
    # convenience view of the same manifest. Source digests are computed on the
    # side-effect-free path: plan mode must not admit a capsule or create the
    # store (skillbox-sbp-test-plan-compiler-er74).
    from . import sbp_test_capsule as capsule_builder  # noqa: PLC0415
    from . import sbp_test_plan as plan_compiler  # noqa: PLC0415

    source: dict[str, str] = {}
    try:
        source = capsule_builder.compute_digests(cwd)
    except capsule_builder.CapsuleRefusal as refusal:
        # Best effort, deliberately. `plan` is a read-only inspection verb and
        # must stay usable where a capsule is not available or is refused (a
        # non-git tree, a secret-shaped path). The plan is still valid; it simply
        # is not source-bound, and says so rather than pretending. Placement is
        # what requires real digests.
        payload["source_unavailable"] = refusal.to_payload()

    try:
        compiled = plan_compiler.compile_plan(cwd, group=group_name, source_digests=source)
    except plan_compiler.PlanRefusal as refusal:
        payload["ok"] = False
        payload["plan"] = None
        payload.update(refusal.to_payload())
        return payload

    payload["plan"] = compiled.to_payload()
    payload["plan_digest"] = compiled.digest
    return payload


def score_payload(
    cwd: Path,
    *,
    probe: Any = None,
    probe_runner: Any = None,
    verify_archive: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Score this repo's suite readiness. Static and read-only by default.

    Unlike plan/lint this does not require a manifest: it reads the repo's own
    Make/package/pytest/compose surfaces. The result is *analysis*, never a
    generated manifest -- the payload says so, and an unreadable construct
    becomes an explicit unknown rather than a guess.

    ``probe`` is the opt-in half (skillbox-sbp-test-probe-mode-sz4d). Passing a
    :class:`~runtime_manager.sbp_test_probe.ProbeAuthority` asks for bounded
    execution inside an admitted disposable capsule. It changes nothing about the
    default: with ``probe=None`` this function executes exactly as before, and
    ``probed`` is stamped ``False`` either way so no reader can mistake a static
    score for one that ran probes.
    """
    from . import sbp_test_scorer as scorer  # noqa: PLC0415

    cwd = Path(cwd)
    payload = _envelope("score", cwd)
    try:
        report = scorer.score_report(cwd)
    except scorer.ScorerRefusal as refusal:
        payload.update(refusal.to_payload())
        payload["probed"] = False
        return payload
    except Exception as exc:  # noqa: BLE001 - a scorer bug must not print a traceback
        payload["ok"] = False
        payload["error"] = f"scorer failed: {type(exc).__name__}"
        payload["error_code"] = "internal_error"
        payload["probed"] = False
        payload["next_actions"] = ["sbp test lint --format json"]
        return payload

    payload["report"] = report
    payload["manifest_present"] = report["provenance"]["manifest_present"]
    payload["analysis_only"] = True
    payload["probed"] = False
    payload["next_actions"] = report["next_actions"]
    if probe is not None:
        _attach_probe(
            payload,
            cwd,
            probe=probe,
            probe_runner=probe_runner,
            verify_archive=verify_archive,
            clock=clock,
        )
    return payload


def _probe_units(cwd: Path, group: str | None = None) -> list[Any]:
    """The manifest's compiled units, as probe units.

    Probes execute the repo's *declared* contract, never a guess at it: a probe
    that invented its own commands would be testing the scorer's imagination.
    """
    probe_mode = _probe_mode()
    manifest, _issues = manifest_schema.load_manifest(cwd)
    if manifest is None:
        return []
    ordered, plan_issues = manifest_schema.compile_plan(manifest, group or DEFAULT_GROUP)
    if plan_issues:
        return []
    return [
        probe_mode.ProbeUnit(
            id=unit.id,
            argv=tuple(unit.command),
            # Carried, never inferred: `services` is what the service-permission
            # gate refuses against, and `artifacts` is what "did this lane emit
            # per-unit evidence" is measured against.
            services=tuple(unit.services),
            artifacts=tuple(unit.artifacts),
        )
        for unit in ordered
    ]


def _attach_probe(
    payload: dict[str, Any],
    cwd: Path,
    *,
    probe: Any,
    probe_runner: Any,
    verify_archive: Any,
    clock: Any,
) -> None:
    """Run the probes and fold their receipt into the payload, or refuse.

    A refusal leaves the static report exactly where it was and stamps
    ``probed=False``: the analysis is still worth reading, and pretending
    otherwise would punish a caller for asking a harder question.
    """
    probe_mode = _probe_mode()
    payload["probe_schema"] = probe_mode.PROBE_SCHEMA
    try:
        # The default runner is the repo's existing wave-concurrent local
        # executor. Probe mode reuses it rather than growing a second, weaker
        # scheduler that would drift from the one real runs use. Callers (and
        # tests) may inject a bounded fake instead; `run_probes` itself still
        # refuses a `None` runner, so the default lives here and not there.
        runner = probe_runner
        if runner is None:
            runner = probe_mode.local_executor_runner()
        kwargs: dict[str, Any] = {
            "consumer_root": cwd,
            "authority": probe,
            "runner": runner,
            "verify_archive": verify_archive,
        }
        if clock is not None:
            kwargs["clock"] = clock
        receipt = probe_mode.run_probes(_probe_units(cwd), **kwargs)
    except probe_mode.ProbeRefusal as refusal:
        payload["ok"] = False
        payload.update(refusal.to_payload())
        payload["probed"] = False
        payload["next_actions"] = [
            *refusal.next_actions,
            "sbp test score --format json  # the static score is unaffected",
        ]
        return

    from . import sbp_test_scorer as scorer  # noqa: PLC0415

    digest = probe_mode.receipt_digest(receipt)
    upgraded, upgrades = probe_mode.upgrade_report(payload["report"], receipt)
    if "provenance" in upgraded:
        upgraded["provenance"] = scorer.probed_provenance(
            upgraded["provenance"], receipt, digest
        )
    payload["report"] = upgraded
    payload["probed"] = True
    payload["probe_receipt"] = receipt
    payload["probe_receipt_digest"] = digest
    payload["probe_upgrades"] = upgrades
    payload["probe_counts"] = receipt["counts"]
    payload["analysis_only"] = False
    payload["next_actions"] = upgraded["next_actions"]


def score_exit_class(payload: dict[str, Any]) -> str:
    """"ok" / "needs_input" / "error" -- the CLI owns the numeric ladder.

    Findings are data, not failure: a report full of proven blockers still exits
    ok, because an inspection verb that exits nonzero on what it found trains
    agents to stop reading it. A *failed probe* is the same kind of thing --
    evidence -- so it also exits ok. Only the inability to run a probe is
    nonzero, and it lands on needs-input because the caller can fix it.
    """
    if payload.get("ok"):
        return "ok"
    code = payload.get("error_code")
    if code in SCORE_NEEDS_INPUT_CODES:
        return "needs_input"
    if code in _probe_mode().NEEDS_INPUT_CODES:
        return "needs_input"
    return "error"


def status_payload(cwd: Path) -> dict[str, Any]:
    """Bare `sbp test`: report the repo's test contract without running anything."""
    cwd = Path(cwd)
    payload = _envelope("status", cwd)
    manifest, issues = manifest_schema.load_manifest(cwd)
    payload["manifest_present"] = manifest_path(cwd).is_file()
    payload["read_only_verbs"] = list(READ_ONLY_VERBS)
    payload["write_verbs"] = list(WRITE_VERBS)
    payload["gated_verbs"] = list(GATED_VERBS)
    payload["implemented_verbs"] = list(READ_ONLY_VERBS + WRITE_VERBS)
    payload["supported_manifest_versions"] = list(SUPPORTED_SCHEMA_VERSIONS)
    if manifest is None:
        payload["ok"] = False
        payload["issues"] = manifest_schema.findings_payload(issues)
        payload["unit_count"] = 0
        payload["group_count"] = 0
        payload["next_actions"] = [f"create {MANIFEST_RELPATH}"]
        return payload
    payload["issues"] = manifest_schema.findings_payload(issues)
    payload["unit_count"] = len(manifest.units)
    payload["group_count"] = len(manifest.groups)
    payload["groups"] = sorted(manifest.groups)
    payload["ok"] = not issues
    payload["next_actions"] = ["sbp test plan --format json", "sbp test lint --format json"]
    return payload


def capsule_payload(cwd: Path, *, admit: bool = True) -> dict[str, Any]:
    """Build a source capsule and stamp all three identifiers.

    This is what makes a receipt able to say *which bytes ran*. All three
    identifiers are emitted together from day one -- they answer different
    questions and are never substituted for one another.

    Unlike plan/lint this is not read-only: it admits an archive into the local
    content-addressed store. It still executes nothing and sends nothing.
    """
    from . import sbp_test_capsule as capsule_builder  # noqa: PLC0415

    cwd = Path(cwd)
    payload = _envelope("capsule", cwd)
    try:
        capsule = capsule_builder.build_capsule(cwd, admit_to_store=admit)
    except capsule_builder.CapsuleRefusal as refusal:
        payload.update(refusal.to_payload())
        payload["next_actions"] = ["remove or rename the refused paths, then retry"]
        return payload
    payload["capsule"] = capsule.to_payload()
    payload.update(capsule.identifiers())
    payload["next_actions"] = ["sbp test plan --format json"]
    return payload


def deferred_payload(verb: str, cwd: Path) -> dict[str, Any]:
    """`run`/`dispatch`: declared and gated, not implemented in slice 1.

    A typed refusal, never a silent no-op: an agent must be able to tell
    "this does nothing yet" from "this ran and found nothing".
    """
    payload = _envelope(verb, Path(cwd), ok=False)
    payload["error"] = f"`test {verb}` is not implemented in this slice"
    payload["error_code"] = "not_implemented"
    payload["implemented_verbs"] = list(READ_ONLY_VERBS)
    payload["next_actions"] = ["sbp test plan --format json"]
    return payload
