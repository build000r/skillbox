#!/usr/bin/env python3
"""Generate docs/SBP_OUTPUT_SCHEMAS.md from the golden fixtures + payload functions.

Agents parse the JSON payloads of the ``sbp`` control plane every session, so the
shapes are a first-class contract. This generator renders ONE reference page that,
for each output surface, documents every field, marks each as CONTRACT vs
informational, and shows ONE example payload.

Why it cannot drift from reality
================================

The example payloads are not hand-written: they are produced *live* by calling the
real payload functions against the deterministic miniature estate in
``tests/fixture_fleet.py`` (the same harness the golden tests use) or, for the two
surfaces whose live payload embeds host paths/durations (``sbp doctor`` /
structure-doctor), by driving the real ``run_structure_doctor`` with canned gate
specs exactly as ``tests/test_structure_doctor.py`` does. Volatile tokens
(the per-run temp root, the runtime repo root, wall-clock durations) are normalized
to stable placeholders so the doc is byte-deterministic across machines and runs.

The committed doc is locked by ``tests/test_output_schema_docs.py``: that test runs
this generator and asserts the result equals the committed file. Editing the payload
functions therefore forces a visible, reviewable doc diff (regenerate with
``REGEN_OUTPUT_SCHEMA_DOCS=1``).

The per-field *stability notes* (CONTRACT vs informational + the one-line meaning)
live in ``FIELD_NOTES`` below; they are the curated half of the contract and were
read off the payload-producing functions
(``collect_skill_visibility`` / ``explain_skill_visibility`` / ``collect_mcp_audit``
/ ``run_structure_doctor`` / ``build_fleet_converge_plan``). Every field present in a
generated example is checked against ``FIELD_NOTES`` so a new field that ships without
a stability note also breaks the drift test.

Run::

    python3 scripts/gen_output_schemas.py            # print to stdout
    python3 scripts/gen_output_schemas.py --write     # write the doc
    REGEN_OUTPUT_SCHEMA_DOCS=1 python3 -m pytest tests/test_output_schema_docs.py
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
TESTS_DIR = ROOT_DIR / "tests"
for _path in (ENV_MANAGER_DIR, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fixture_fleet import build_fixture_fleet  # noqa: E402
from runtime_manager import fleet_converge as fc  # noqa: E402
from runtime_manager import mcp_visibility as mv  # noqa: E402
from runtime_manager import skill_visibility as sv  # noqa: E402
from runtime_manager import structure_doctor as sd  # noqa: E402


DOC_PATH = ROOT_DIR / "docs" / "SBP_OUTPUT_SCHEMAS.md"
REGEN_ENV = "REGEN_OUTPUT_SCHEMA_DOCS"

# Stable placeholders for the volatile tokens in live payloads.
FLEET_PLACEHOLDER = "<FLEET>"
RUNTIME_ROOT_PLACEHOLDER = "<RUNTIME_ROOT>"


# --------------------------------------------------------------------------- #
# Per-field stability notes (the curated half of the contract).
#
# Each surface maps a top-level field name -> (stability, note) where stability
# is "CONTRACT" (agents may depend on the field's presence + meaning) or "info"
# (advisory / human-facing / best-effort; shape may evolve). These were read off
# the payload-producing functions; a generated example field with no entry here
# fails the drift test, so the notes can never silently fall behind the payload.
# --------------------------------------------------------------------------- #

CONTRACT = "CONTRACT"
INFO = "info"

FIELD_NOTES: dict[str, dict[str, tuple[str, str]]] = {
    "skills": {
        "cwd": (CONTRACT, "Absolute resolved cwd the visibility view was computed for."),
        "active_clients": (CONTRACT, "Client overlays active for this resolution."),
        "active_profiles": (CONTRACT, "Runtime profiles active for this resolution."),
        "matched_clients": (CONTRACT, "Client overlays whose cwd_match matched this cwd (id + match)."),
        "matched_project_categories": (CONTRACT, "Policy project categories this cwd falls under (id + path)."),
        "matched_scope_rules": (CONTRACT, "skill-scope.yaml rules in force for this cwd (id + provenance)."),
        "summary": (CONTRACT, "Roll-up counters; keys are stable, add-only. Branch on these first."),
        "parity": (CONTRACT, "Claude<->Codex GLOBAL skill-surface parity (empty when --no-global)."),
        "overlay_audit": (INFO, "Declared-overlay registry audit: declared + active overlays and warnings for active overlays not in the registry (advisory, never a hard fail; only when an overlays: block is declared)."),
        "effective": (CONTRACT, "The skills actually visible at this cwd after layer resolution."),
        "issues": (CONTRACT, "Policy problems grouped by kind (broken_project, missing_for_cwd, scope_violations, ...)."),
        "beads": (CONTRACT, "Beads requirement/readiness derived from effective skills' frontmatter."),
        "recommendations": (INFO, "Ranked human-facing remediation suggestions."),
        "policy": (CONTRACT, "Which policy files + project categories drove this view."),
        "source_roots": (INFO, "Discovered skill source roots (only when --show-sources)."),
        "undefined_sources": (INFO, "Linkable sources with no policy occurrence (only when --show-sources)."),
        "next_actions": (INFO, "Ordered, copy-pasteable next commands for a human/agent."),
        # full-payload-only fields (sbp skills --full / candidates)
        "global_surfaces": (INFO, "Per-surface GLOBAL home skill report (only with global scope)."),
        "layers": (INFO, "Every resolution layer considered, ranked (full payload only)."),
        "occurrences": (CONTRACT, "Every raw skill occurrence across all layers (full payload only)."),
    },
    # `sbp candidates` == `sbp skills --show-sources --full --no-global`, i.e. the
    # FULL collect_skill_visibility payload. Its notes are derived from the `skills`
    # set (the single source of truth for these field meanings) with a few
    # candidate-specific overrides applied just below this dict.
    "mcp": {
        "cwd": (CONTRACT, "Absolute resolved cwd the audit ran against."),
        "config_root": (CONTRACT, "Repo whose Claude/Codex MCP config is audited."),
        "expected_servers": (CONTRACT, "Servers expected for this profile/scope (sorted)."),
        "declared_servers": (CONTRACT, "Servers explained by the runtime model (any profile) ∪ expected."),
        "surfaces": (CONTRACT, "Per-surface (claude/codex) config read; see the surface field table."),
        "parity": (CONTRACT, "Claude-vs-Codex set difference, split declared (intentional) vs unexpected (drift)."),
        "summary": (CONTRACT, "Counters; unexplained_drift>0 and invalid_configs>0 are the gate signals."),
        "next_actions": (INFO, "Ordered repair commands for missing/unexpected/parity drift."),
    },
    "mcp.surface": {
        "name": (CONTRACT, "Surface id: 'claude' or 'codex'."),
        "format": (CONTRACT, "Config format: 'json' (Claude) or 'toml' (Codex)."),
        "path": (CONTRACT, "Absolute path of the surface config file."),
        "present": (CONTRACT, "Whether the config file exists as a readable file."),
        "broken_symlink": (CONTRACT, "True when path is a symlink whose target is absent."),
        "symlink_target": (INFO, "Raw readlink target when path is a symlink, else null."),
        "valid": (CONTRACT, "False when the config could not be parsed (see error)."),
        "servers": (CONTRACT, "All declared server names (incl. disabled), sorted."),
        "effective_servers": (CONTRACT, "Enabled server names (servers minus disabled), sorted."),
        "disabled_servers": (CONTRACT, "Server names present but disabled."),
        "missing": (CONTRACT, "expected_servers absent from this surface — the add list."),
        "extra": (INFO, "effective_servers not in expected (declared + unexplained combined)."),
        "extra_intentional": (CONTRACT, "Extra servers that ARE declared (profile-gated; not drift)."),
        "unexpected": (CONTRACT, "Servers present but neither expected nor declared — real drift."),
        "error": (CONTRACT, "Parse error string, or null when valid."),
    },
    "recalibrate": {
        # `sbp recalibrate` is a COMPOSITE human surface; its machine-readable
        # core is the `sbp skills --issues-only --format json` view (same
        # collect_skill_visibility payload) plus the embedded `beads` block.
        "cwd": (CONTRACT, "Absolute resolved cwd being recalibrated."),
        "matched_scope_rules": (CONTRACT, "Rules in force for this cwd."),
        "matched_project_categories": (CONTRACT, "Project categories for this cwd."),
        "issues": (CONTRACT, "The drift to heal, grouped by kind (the issues-only view's payload)."),
        "beads": (CONTRACT, "required / required_skills / repo_root / initialized / br / issues."),
        "summary": (CONTRACT, "Counters incl. beads_required_skills + beads_issues."),
        "recommendations": (INFO, "Ranked remediation suggestions."),
        "next_actions": (INFO, "Ordered next commands (dry-run heal moves)."),
    },
    "recalibrate.beads": {
        "required": (CONTRACT, "True when an effective skill declares requires_beads."),
        "required_skills": (CONTRACT, "Effective skills that require beads (name + path)."),
        "repo_root": (CONTRACT, "Git repo root for the cwd, or null when none."),
        "beads_dir": (CONTRACT, "Path to the repo's .beads dir, or null when none."),
        "initialized": (CONTRACT, "Whether a .beads database exists in the repo."),
        "br": (CONTRACT, "Whether the `br` CLI is available on PATH."),
        "ok": (CONTRACT, "True when the beads requirement is satisfied (or not required)."),
        "issues": (CONTRACT, "Per-issue {message, hint} for unmet beads requirements."),
        "next_actions": (INFO, "Beads-specific next commands."),
        "repo": (INFO, "Short repo name (golden alias), advisory."),
        "activated_skill": (INFO, "Skill that triggered the requirement (golden field), advisory."),
    },
    "explain": {
        "schema_version": (CONTRACT, "Versioned tag for the explain payload shape."),
        "skill": (CONTRACT, "The skill name explained."),
        "cwd": (CONTRACT, "Absolute resolved cwd the provenance is for."),
        "visible": (CONTRACT, "True iff the skill resolves to a non-broken effective occurrence here."),
        "reason": (INFO, "Human sentence explaining the verdict."),
        "layer": (CONTRACT, "Winning layer id, or null when not visible."),
        "layer_family": (CONTRACT, "PROJECT|GLOBAL|CLIENT|DEFAULT of the winner, or null."),
        "layer_label": (INFO, "Human label for the winning layer, or null."),
        "layer_rank": (CONTRACT, "Numeric rank of the winning layer, or null."),
        "winner": (CONTRACT, "Trimmed view of the effective occurrence (won=true), or null."),
        "occurrences": (CONTRACT, "Every occurrence of this skill across layers, each with a won verdict."),
        "lost": (CONTRACT, "Non-winning occurrences with a lost_reason."),
        "scope_rules": (CONTRACT, "skill-scope.yaml rules naming this skill at this cwd."),
        "inactive_overlay_rules": (CONTRACT, "Overlay-gated rules that would apply if the overlay were active."),
        "source_options": (CONTRACT, "Discoverable source dirs that could be linked to make it visible."),
        "active_overlays": (CONTRACT, "Overlays currently active."),
        "active_clients": (CONTRACT, "Active client overlays."),
        "matched_clients": (CONTRACT, "Client overlays matching this cwd."),
        "matched_project_categories": (CONTRACT, "Project categories for this cwd."),
        "remediation": (CONTRACT, "Ranked, narrowest-first paths to visibility, each with kind + exact command."),
        "machine": (CONTRACT, "Forward-compatible machine-routing block (always present, may be partial)."),
        "registry": (CONTRACT, "Forward-compatible {skill_id, registry_ids} block (always present)."),
        "next_actions": (INFO, "Commands from remediation, or the already-visible sentinel."),
    },
    "doctor": {
        "ok": (CONTRACT, "True iff no gate is FAIL (INCO and PASS are both ok)."),
        "config_root": (CONTRACT, "Resolved skillbox-config root, or null when not found (gates go INCO)."),
        "runtime_root": (CONTRACT, "Resolved runtime repo root."),
        "cwd": (CONTRACT, "Absolute resolved cwd the gates ran against."),
        "gates": (CONTRACT, "One row per gate in declaration order; see the gate field table."),
        "summary": (CONTRACT, "Gate counters + structure budget; structure_within_budget guards the <60s promise."),
        "exit_code": (CONTRACT, "1 iff any gate FAILed, else 0 (INCO never flips it)."),
    },
    "doctor.gate": {
        "name": (CONTRACT, "Gate id (e.g. structure_invariants, mcp_parity, runtime_doctor)."),
        "kind": (CONTRACT, "'structure' or 'runtime'; only structure gates count toward the budget."),
        "status": (CONTRACT, "PASS | FAIL | INCO. Only FAIL flips exit_code; INCO is never a regression."),
        "duration_s": (INFO, "Wall-clock the gate took (non-deterministic; normalized in this example)."),
        "fix_command": (CONTRACT, "Exact command to remediate this gate when not PASS."),
        "detail": (INFO, "Human one-line outcome detail."),
    },
    "fleet_converge": {
        "kind": (CONTRACT, "Literal 'fleet-converge-plan' discriminator."),
        "dry_run": (CONTRACT, "Always true — converge is PLAN ONLY and never writes."),
        "cwd": (CONTRACT, "Absolute resolved cwd the plan was invoked from."),
        "scan_roots": (CONTRACT, "Roots walked to build the candidate fleet."),
        "max_depth": (CONTRACT, "Max repo-discovery depth under each scan root."),
        "classes": (CONTRACT, "The five triage classes in fixed order: relink, prune, sync, policy, mcp."),
        "summary": (CONTRACT, "Fleet roll-up; by_class is keyed by the five classes; actions_total is their sum."),
        "repos": (CONTRACT, "Per-repo heal plans (sorted by path); see the repo-row field table."),
        "next_actions": (INFO, "One representative bulk command per non-empty class."),
    },
    "fleet_converge.repo": {
        "path": (CONTRACT, "Absolute canonical repo path."),
        "sources": (CONTRACT, "Why this repo is in the fleet (scan_root / category / client provenance)."),
        "state": (CONTRACT, "'ok' or 'missing' (a declared path absent on this box; carries no actions)."),
        "matched_scope_rules": (CONTRACT, "Rule ids in force for the repo (absent on missing repos)."),
        "categories": (CONTRACT, "Project categories the repo falls under (absent on missing repos)."),
        "actions": (CONTRACT, "Heal actions grouped by the five classes; each action carries its exact command."),
        "counts": (CONTRACT, "Action count per class for this repo."),
        "total": (CONTRACT, "Sum of counts across classes for this repo."),
    },
}

# `sbp candidates` IS the full collect_skill_visibility payload, so its field
# meanings are exactly the `skills` set. Derive them once (single source of truth)
# and apply candidate-specific overrides that promote the source-discovery fields
# to CONTRACT (they are the load-bearing fields for the exploratory bucketing).
FIELD_NOTES["candidates"] = dict(FIELD_NOTES["skills"]) | {
    "source_roots": (CONTRACT, "Every skill source root discovered under the configured roots — the linkable universe."),
    "undefined_sources": (CONTRACT, "Linkable source skills with no policy occurrence — the candidate pool."),
}


# --------------------------------------------------------------------------- #
# Surface definitions: title, intro, the source of the example, and which
# nested field tables to render alongside the top-level one.
# --------------------------------------------------------------------------- #


def _norm(obj: Any, replacements: list[tuple[str, str]]) -> Any:
    """Replace volatile absolute-path tokens with stable placeholders, recursively."""
    if isinstance(obj, dict):
        return {key: _norm(value, replacements) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_norm(item, replacements) for item in obj]
    if isinstance(obj, str):
        out = obj
        for needle, token in replacements:
            if needle:
                out = out.replace(needle, token)
        return out
    return obj


def _fleet_example(fn: Callable[["FixtureFleetT", str], dict[str, Any]]) -> dict[str, Any]:
    """Build a fixture-fleet, run ``fn``, and normalize the temp root to <FLEET>."""
    tmp = tempfile.mkdtemp()
    try:
        fleet = build_fixture_fleet(tmp)
        with fleet._home_patched():
            payload = fn(fleet, tmp)
        return _norm(payload, [(tmp, FLEET_PLACEHOLDER)])
    finally:
        # Best-effort cleanup; the temp tree carries no operator state.
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# Typing alias kept loose to avoid importing the dataclass for annotations only.
FixtureFleetT = Any


def example_skills() -> dict[str, Any]:
    """`sbp skills` (default, compact) at the overlay repo."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        payload = sv.collect_skill_visibility(
            fleet.model(), cwd=str(fleet.repo("overlay-repo")),
            include_global=False, include_project=True, include_sources=False,
        )
        return sv.compact_skill_visibility_payload(payload)

    return _fleet_example(run)


def example_candidates() -> dict[str, Any]:
    """`sbp candidates` == `sbp skills --show-sources --full --no-global` at the cli repo."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        return sv.collect_skill_visibility(
            fleet.model(), cwd=str(fleet.repo("healthy")),
            include_global=False, include_project=True, include_sources=True,
        )

    return _fleet_example(run)


def example_mcp() -> dict[str, Any]:
    """`sbp mcp` (read-only audit) against a repo with no MCP config (clean missing-only case)."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        return mv.collect_mcp_audit(
            ROOT_DIR, fleet.model(), cwd=str(fleet.repo("healthy")), declared_servers=[],
        )

    payload = _fleet_example(run)
    return _norm(payload, [(str(ROOT_DIR), RUNTIME_ROOT_PLACEHOLDER)])


def example_recalibrate() -> dict[str, Any]:
    """`sbp recalibrate` machine core: the `--issues-only` skill-visibility view at the overlay repo.

    The wrapper composes several dry-run sub-calls; its single JSON payload is the
    issues-focused ``collect_skill_visibility`` result (the same one the wrapper's
    ``--issues-only --format json`` step parses for the beads block).
    """

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        payload = sv.collect_skill_visibility(
            fleet.model(), cwd=str(fleet.repo("overlay-repo")),
            include_global=False, include_project=True, include_sources=False,
        )
        # Mirror the wrapper's issues-only machine view: the same payload, trimmed
        # to the recalibration-relevant fields the wrapper's pipeline reads.
        return {
            "cwd": payload["cwd"],
            "matched_scope_rules": payload["matched_scope_rules"],
            "matched_project_categories": payload["matched_project_categories"],
            "issues": sv._compact_skill_visibility_issues(payload),
            "beads": payload["beads"],
            "summary": payload["summary"],
            "recommendations": payload["recommendations"],
            "next_actions": payload["next_actions"],
        }

    return _fleet_example(run)


def example_explain() -> dict[str, Any]:
    """`sbp explain <skill>` — the invisible-but-activatable case (richest remediation)."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        # `needs-beads` has a source but is not linked at the healthy repo -> the
        # activatable case, which exercises source_options + ranked remediation.
        return sv.explain_skill_visibility(
            fleet.model(), "needs-beads", cwd=str(fleet.repo("healthy")),
        )

    return _fleet_example(run)


def example_doctor() -> dict[str, Any]:
    """`sbp doctor` (structure-doctor) — driven with canned gate specs for determinism.

    The live payload embeds host paths + wall-clock durations, so we drive the real
    ``run_structure_doctor`` with canned gate specs (one of each status + both kinds)
    exactly as ``tests/test_structure_doctor.py`` does, then normalize durations + the
    runtime root. This keeps the EXAMPLE shape identical to production while staying
    byte-deterministic.
    """

    def fake_specs() -> tuple[Any, ...]:
        rows = [
            ("structure_invariants", sd.KIND_STRUCTURE, sd.STATUS_PASS, "12 invariant(s) passed"),
            ("mcp_parity", sd.KIND_STRUCTURE, sd.STATUS_FAIL, "claude/codex MCP drift: foo only in claude"),
            ("skill_drift", sd.KIND_STRUCTURE, sd.STATUS_INCO, "skillbox-config repo not found on this box"),
            ("runtime_doctor", sd.KIND_RUNTIME, sd.STATUS_PASS, "make doctor: all checks pass"),
        ]
        specs = []
        for name, kind, status, detail in rows:
            specs.append(
                sd._GateSpec(
                    name=name,
                    kind=kind,
                    cap_s=5.0,
                    fix_command=f"<fix for {name}>",
                    runner=(lambda s=status, d=detail: (lambda ctx: (s, d)))(),
                )
            )
        return tuple(specs)

    ctx = sd.DoctorContext(runtime_root=ROOT_DIR, config_root=None, cwd=ROOT_DIR / "sample-repo")
    ctx._model = {"skills": [], "repos": [], "clients": []}
    with mock.patch.object(sd, "_gate_specs", fake_specs), mock.patch.object(
        sd, "build_context", lambda **_kw: ctx
    ):
        payload = sd.run_structure_doctor()
    # Normalize the volatile bits: durations (wall-clock) and roots.
    for gate in payload["gates"]:
        gate["duration_s"] = 0.0
    payload["summary"]["structure_duration_s"] = 0.0
    payload["summary"]["runtime_duration_s"] = 0.0
    payload["runtime_root"] = RUNTIME_ROOT_PLACEHOLDER
    payload["cwd"] = f"{RUNTIME_ROOT_PLACEHOLDER}/sample-repo"
    return payload


def example_fleet_converge() -> dict[str, Any]:
    """`fleet converge` plan over the fixture fleet (skill drift only, include_mcp off)."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        return fc.build_fleet_converge_plan(
            fleet.model(), cwd=str(fleet.repo("overlay-repo")),
            scan_roots=[str(fleet.aliased_root)], include_clean=False,
            include_mcp=False, max_workers=1,
        )

    return _fleet_example(run)


# (key, command, title, intro, example fn, nested tables [(notes_key, label, picker)])
SURFACES: list[dict[str, Any]] = [
    {
        "key": "skills",
        "command": "sbp skills",
        "long": "`sbp skills [--full] [--no-global] [--show-sources] --format json`",
        "fn": "collect_skill_visibility (compact via compact_skill_visibility_payload)",
        "intro": (
            "The conflict-aware skill availability view for the current cwd. `sbp skills` "
            "emits the COMPACT payload (below); `sbp skills --full` adds `global_surfaces`, "
            "`layers`, and `occurrences`. Branch on `summary` counters and `issues` groups; "
            "`effective` is the authoritative list of what is visible here."
        ),
        "example": example_skills,
        "nested": [],
    },
    {
        "key": "candidates",
        "command": "sbp candidates",
        "long": "`sbp candidates --json` (== `sbp skills --show-sources --full --no-global --format json`)",
        "fn": "collect_skill_visibility (full, include_sources=True)",
        "intro": (
            "The exploratory source-inventory surface. Same payload as `sbp skills --full` "
            "with sources enabled; the load-bearing fields for bucketing candidates are "
            "`undefined_sources` + `source_roots` (the linkable universe) against `effective` "
            "(already present), `issues.missing_for_cwd` (definitely), and the matched policy."
        ),
        "example": example_candidates,
        "nested": [],
    },
    {
        "key": "mcp",
        "command": "sbp mcp",
        "long": "`sbp mcp [--cwd <repo>] --format json` (bare `sbp mcp` runs the read-only audit)",
        "fn": "collect_mcp_audit",
        "intro": (
            "Claude (`.mcp.json`) vs Codex (`.codex/config.toml`) MCP-server reconciliation. "
            "`expected_servers` is the per-scope baseline, `declared_servers` adds model-declared "
            "servers (any profile). Gate on `summary.unexplained_drift` and `summary.invalid_configs`; "
            "per-surface detail is in `surfaces.claude` / `surfaces.codex`."
        ),
        "example": example_mcp,
        "nested": [("mcp.surface", "`surfaces.claude` / `surfaces.codex` (one MCP surface)", None)],
    },
    {
        "key": "recalibrate",
        "command": "sbp recalibrate",
        "long": "`sbp recalibrate [--cwd <repo>]` (composite; machine core shown below)",
        "fn": "collect_skill_visibility (issues-only view) + embedded beads block",
        "intro": (
            "A COMPOSITE human surface that stitches together several dry-run sub-calls "
            "(`sbp skills --issues-only`, `sbp skill sync --dry-run`, `sbp skill prune --dry-run`, "
            "the beads graph, and `sbp mcp`). Its single machine-readable core is the issues-focused "
            "`collect_skill_visibility` payload below — same shape as `sbp skills --issues-only "
            "--format json`, whose `beads` block the wrapper parses directly."
        ),
        "example": example_recalibrate,
        "nested": [("recalibrate.beads", "`beads` (beads requirement/readiness)", None)],
    },
    {
        "key": "explain",
        "command": "sbp explain",
        "long": "`sbp explain <skill> [--cwd <repo>] --format json`",
        "fn": "explain_skill_visibility",
        "intro": (
            "Full provenance for ONE skill at ONE cwd: is it visible, via which layer, which "
            "occurrences lost and why, and — when invisible — the ranked, narrowest path to "
            "visibility with the EXACT command for each option. `machine` and `registry` are "
            "forward-compatible blocks: always present (possibly partial) so routing/registry "
            "consumers can grow without a schema break."
        ),
        "example": example_explain,
        "nested": [],
    },
    {
        "key": "doctor",
        "command": "sbp doctor",
        "long": "`sbp doctor [--cwd <repo>] --format json` (a.k.a. structure-doctor)",
        "fn": "run_structure_doctor",
        "intro": (
            "The structural verification front door. Runs every gate read-only and returns "
            "`{ok, gates, summary, exit_code}`. FAIL is the only status that flips `exit_code`; "
            "INCO (e.g. a dependency unreachable) and PASS both exit 0 — INCO is never a "
            "regression. The example below uses canned gate outcomes (one of each status) for "
            "determinism; `duration_s` is real wall-clock in production (normalized to 0.0 here)."
        ),
        "example": example_doctor,
        "nested": [("doctor.gate", "`gates[]` (one gate outcome)", None)],
    },
    {
        "key": "fleet_converge",
        "command": "fleet converge",
        "long": "`sbp fleet converge [--cwd <repo>] [--all] [--no-mcp] --format json`",
        "fn": "build_fleet_converge_plan",
        "intro": (
            "ONE diffable, PLAN-ONLY heal plan across the deduped canonical fleet (the same "
            "candidate set `collect_skill_audit` reports). Each repo's drift is grouped into the "
            "five fixed triage classes (`relink`, `prune`, `sync`, `policy`, `mcp`); every action "
            "carries its exact single-repo command. `dry_run` is always true — converge never writes."
        ),
        "example": example_fleet_converge,
        "nested": [("fleet_converge.repo", "`repos[]` (one per-repo heal plan)", None)],
    },
]


def _present_fields(example: Any) -> list[str]:
    """Top-level field names in a payload example (dict only)."""
    if isinstance(example, dict):
        return list(example.keys())
    return []


def _first_nested(example: dict[str, Any], notes_key: str) -> dict[str, Any] | None:
    """Pull one representative nested object for a nested field table.

    The notes_key encodes the path: 'mcp.surface' -> surfaces.claude,
    'doctor.gate' -> gates[0], 'recalibrate.beads' -> beads,
    'fleet_converge.repo' -> repos[0].
    """
    leaf = notes_key.split(".")[-1]
    if leaf == "surface":
        return ((example.get("surfaces") or {}).get("claude")) or None
    if leaf == "gate":
        gates = example.get("gates") or []
        return gates[0] if gates else None
    if leaf == "beads":
        return example.get("beads") or None
    if leaf == "repo":
        repos = example.get("repos") or []
        return repos[0] if repos else None
    return None


def _field_table(notes_key: str, fields: list[str]) -> list[str]:
    """Render a field | stability | meaning table for the given fields.

    Every field MUST have an entry in FIELD_NOTES[notes_key]; an unknown field
    is rendered with a loud TODO marker so the drift test fails until the note
    is added (the doc can never silently fall behind the payload).
    """
    notes = FIELD_NOTES.get(notes_key, {})
    lines = [
        "| Field | Stability | Meaning |",
        "|-------|-----------|---------|",
    ]
    for field in fields:
        stability, note = notes.get(field, ("???", "MISSING STABILITY NOTE — add to FIELD_NOTES"))
        lines.append(f"| `{field}` | {stability} | {note} |")
    return lines


def render_doc() -> str:
    lines: list[str] = []
    lines.append("<!-- GENERATED FILE — do not hand-edit. -->")
    lines.append(
        "<!-- Regenerate: python3 scripts/gen_output_schemas.py --write "
        "(or REGEN_OUTPUT_SCHEMA_DOCS=1 python3 -m pytest tests/test_output_schema_docs.py). -->"
    )
    lines.append(
        "<!-- Generated from: scripts/gen_output_schemas.py + tests/fixture_fleet.py goldens. -->")
    lines.append("")
    lines.append("# sbp Output Schemas")
    lines.append("")
    lines.append(
        "Documented JSON shapes for every `sbp` output surface agents parse. For each "
        "surface: the producing function, a field-by-field table marking each field "
        "**CONTRACT** (depend on it) vs *info* (advisory; shape may evolve), and ONE "
        "example payload."
    )
    lines.append("")
    lines.append(
        "This page is **generated** from `scripts/gen_output_schemas.py`. The example "
        "payloads are produced live by calling the real payload functions against the "
        "deterministic `tests/fixture_fleet.py` estate (the same harness the golden tests "
        "use), so they cannot drift from the runtime. `tests/test_output_schema_docs.py` "
        "locks the committed file to the generator output."
    )
    lines.append("")
    lines.append("## Reading the stability column")
    lines.append("")
    lines.append(
        "- **CONTRACT** — the field's presence and meaning are part of the agent-facing "
        "contract; parse and branch on it. The `summary` / `by_class` counter *sets* are "
        "add-only (new keys may appear; existing keys keep their meaning)."
    )
    lines.append(
        "- *info* — advisory, human-facing, or best-effort (e.g. `reason`, `recommendations`, "
        "`next_actions`, `duration_s`). Useful to surface, but its exact shape/wording may evolve."
    )
    lines.append("")
    lines.append("## Surfaces")
    lines.append("")
    for surface in SURFACES:
        lines.append(f"- [`{surface['command']}`](#{_anchor(surface['command'])})")
    lines.append("")

    for surface in SURFACES:
        example = surface["example"]()
        lines.append("---")
        lines.append("")
        lines.append(f"## `{surface['command']}`")
        lines.append("")
        lines.append(f"**Invocation:** {surface['long']}  ")
        lines.append(f"**Produced by:** `{surface['fn']}`")
        lines.append("")
        lines.append(surface["intro"])
        lines.append("")
        lines.append("### Fields")
        lines.append("")
        lines.extend(_field_table(surface["key"], _present_fields(example)))
        lines.append("")
        for notes_key, label, _picker in surface["nested"]:
            nested = _first_nested(example, notes_key)
            if nested is None:
                continue
            lines.append(f"#### {label}")
            lines.append("")
            lines.extend(_field_table(notes_key, _present_fields(nested)))
            lines.append("")
        lines.append("### Example payload")
        lines.append("")
        lines.append(
            "<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to "
            f"`{FLEET_PLACEHOLDER}` / `{RUNTIME_ROOT_PLACEHOLDER}`.</sub>")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(example, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _anchor(text: str) -> str:
    """GitHub-style markdown header anchor for a `code` command heading."""
    # Heading is rendered as `## `<command>``; GitHub strips backticks and the
    # surrounding code formatting, lowercases, and joins spaces with hyphens.
    slug = text.lower().strip()
    out = []
    for ch in slug:
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("-")
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Write the doc to disk instead of printing it.")
    args = parser.parse_args(argv)
    doc = render_doc()
    if args.write:
        DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOC_PATH.write_text(doc, encoding="utf-8")
        print(f"wrote {DOC_PATH}")
    else:
        sys.stdout.write(doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
