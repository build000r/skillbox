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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
TESTS_DIR = ROOT_DIR / "tests"
for _path in (ENV_MANAGER_DIR, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from argparse import Namespace

from runtime_manager import cli as runtime_cli  # noqa: E402
from runtime_manager import fleet_converge as fc  # noqa: E402
from runtime_manager import lifecycle as lc  # noqa: E402
from runtime_manager.machines import MachineProfile, MachinesConfig  # noqa: E402
from runtime_manager import mcp_visibility as mv  # noqa: E402
from runtime_manager.shared import build_runtime_model  # noqa: E402
from runtime_manager import skill_visibility as sv  # noqa: E402
from runtime_manager import structure_doctor as sd  # noqa: E402


DOC_PATH = ROOT_DIR / "docs" / "SBP_OUTPUT_SCHEMAS.md"
REGEN_ENV = "REGEN_OUTPUT_SCHEMA_DOCS"

# Stable placeholders for the volatile tokens in live payloads.
FLEET_PLACEHOLDER = "<FLEET>"
RUNTIME_ROOT_PLACEHOLDER = "<RUNTIME_ROOT>"
BR_BIN_PLACEHOLDER = "<BR_BIN>"
REMOTE_ROOT_PLACEHOLDER = "<REMOTE_ROOT>"


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
    "capabilities": {
        "ok": (CONTRACT, "True when the wrapper emitted a complete capabilities payload."),
        "tool": (CONTRACT, "Tool identity, e.g. skillbox-sbp."),
        "contract_version": (CONTRACT, "Version tag for this wrapper capabilities contract."),
        "entrypoint": (CONTRACT, "Wrapper entrypoint path relative to the skillbox repo."),
        "cwd": (CONTRACT, "Invocation cwd used by the wrapper."),
        "aliases": (CONTRACT, "Sibling wrapper aliases for this entrypoint."),
        "commands": (CONTRACT, "Agent-facing command inventory with safe_first_try examples."),
        "agent_surfaces": (CONTRACT, "Canonical discovery commands for agents."),
        "skill_verbs": (CONTRACT, "Machine-readable skill verb decision map; every dispatched skill subcommand has an entry."),
        "stdout_stderr_contract": (CONTRACT, "Where JSON and diagnostics are emitted."),
        "safety": (CONTRACT, "Dry-run and confirmation guidance for mutating commands."),
        "next_actions": (INFO, "Common first follow-up commands."),
    },
    "capabilities.skill_verb": {
        "purpose": (CONTRACT, "One-line meaning of the verb."),
        "mutates": (CONTRACT, "Stable mutation class: none, cwd-ephemeral, disk-links, or repo-state+disk-links."),
        "links_disk": (CONTRACT, "True when the verb may create/remove skill links on disk."),
        "returns_packet": (CONTRACT, "True when success includes an activation_packet for immediate session use."),
        "scope": (CONTRACT, "Scope the verb operates on."),
        "survives_recalibrate": (CONTRACT, "True when the verb writes durable repo state that recalibrate/prune should preserve."),
        "when_to_use": (INFO, "Human/agent guidance for choosing this verb."),
        "do_NOT": (INFO, "Important anti-pattern for this verb."),
    },
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
        "visibility_decisions": (CONTRACT, "One winning resolution row per skill name, including disabled/broken winners; use effective for visible skills."),
        "effective": (CONTRACT, "Visible skills at this cwd after layer resolution; excludes disabled/broken winners."),
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
        # `sbp recalibrate --json` emits the issues-only visibility core plus
        # machine-actionable ``fixes[]`` rows (one per actionable issue).
        "cwd": (CONTRACT, "Absolute resolved cwd being recalibrated."),
        "matched_scope_rules": (CONTRACT, "Rules in force for this cwd."),
        "matched_project_categories": (CONTRACT, "Project categories for this cwd."),
        "issues": (CONTRACT, "The drift to heal, grouped by kind (the issues-only view's payload)."),
        "beads": (CONTRACT, "required / required_skills / repo_root / initialized / br / issues."),
        "summary": (CONTRACT, "Counters incl. beads_required_skills + beads_issues."),
        "fixes": (CONTRACT, "Machine-actionable remediation rows; one per actionable issue."),
        "recommendations": (INFO, "Ranked remediation suggestions."),
        "next_actions": (INFO, "Ordered next commands (dry-run heal moves)."),
    },
    "recalibrate.fix": {
        "problem": (CONTRACT, "Issue kind (e.g. missing_for_cwd, scope_violations)."),
        "skill": (CONTRACT, "Skill name this fix targets."),
        "command": (CONTRACT, "Exact copy-pasteable command that resolves the issue."),
        "links": (CONTRACT, "Link actions the dry-run would apply (lifecycle link rows)."),
        "dry_run_preview": (CONTRACT, "Trimmed skill lifecycle dry-run payload for the fix."),
        "packet_on_apply": (INFO, "activation_packet from the dry-run when the fix links a skill; null otherwise."),
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
        "layer": (CONTRACT, "Resolution winner layer id, including disabled/broken winners, or null when none."),
        "winning_layer": (CONTRACT, "Canonical winning_layer copied from the same visibility decision that drives the effective set."),
        "layer_family": (CONTRACT, "PROJECT|GLOBAL|CLIENT|DEFAULT|OVERRIDE of the resolution winner, or null."),
        "layer_label": (INFO, "Human label for the resolution winner layer, or null."),
        "layer_rank": (CONTRACT, "Numeric rank of the resolution winner layer, or null."),
        "winner": (CONTRACT, "Trimmed view of the resolution winner (won=true), or null."),
        "layers": (CONTRACT, "Ordered provenance trace for this skill; exactly one row has wins=true when a winning layer exists."),
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

# `sbp skill why` routes to the same explain payload as `sbp explain`.
FIELD_NOTES["skill_why"] = dict(FIELD_NOTES["explain"])

_SKILL_TOGGLE_NOTES: dict[str, tuple[str, str]] = {
    "action": (CONTRACT, "Verb executed: 'on' or 'off'."),
    "skill": (CONTRACT, "Skill name toggled."),
    "cwd": (CONTRACT, "Absolute resolved repo cwd the toggle ran against."),
    "requested_to": (CONTRACT, "Scope the caller requested (project-only today)."),
    "resolved_to": (CONTRACT, "Scope the plan resolved to."),
    "categories": (CONTRACT, "Project categories targeted when --to category (often empty)."),
    "from_scope": (CONTRACT, "Installed scope considered for off/unlink (project for skill off)."),
    "source_options": (CONTRACT, "Resolvable source directories for on/activate."),
    "selected_source": (CONTRACT, "Chosen source for on/activate, or null for off."),
    "activation_packet": (CONTRACT, "Immediate-use SKILL.md packet on on; null on off."),
    "warnings": (INFO, "Non-fatal plan warnings."),
    "actions": (CONTRACT, "Link/unlink rows the toggle would apply; see the action field table."),
    "skipped": (CONTRACT, "Skills skipped by the plan (e.g. prune firewall pinned rows)."),
    "summary": (CONTRACT, "Roll-up counters for planned/applied link+unlink actions."),
    "dry_run": (CONTRACT, "True when the payload previews without writing."),
    "override": (CONTRACT, "Repo override-file mutation preview/result; see the override field table."),
    "changed": (CONTRACT, "True when disk and/or override state changed (apply mode)."),
    "noop": (CONTRACT, "True when neither override nor link actions would change state."),
    "verification": (INFO, "Optional post-on verify block when --verify is set; null otherwise."),
}
FIELD_NOTES["skill_on"] = dict(_SKILL_TOGGLE_NOTES)
FIELD_NOTES["skill_off"] = dict(_SKILL_TOGGLE_NOTES)

FIELD_NOTES["skill_on.override"] = {
    "changed": (CONTRACT, "True when the override file was written (apply mode)."),
    "pin": (CONTRACT, "Override list touched: pin_on or pin_off."),
    "policy_path": (CONTRACT, "Absolute path of .skillbox/skill-overrides.yaml."),
    "would_change": (CONTRACT, "True when a dry-run would mutate the override file."),
}
FIELD_NOTES["skill_off.override"] = dict(FIELD_NOTES["skill_on.override"])

_FIELD_NOTES_SKILL_ACTIVATION_PACKET = {
    "name": (CONTRACT, "Skill name in the activation packet."),
    "source": (CONTRACT, "Resolved source directory backing the skill."),
    "source_bucket": (CONTRACT, "Source bucket id (external/private/etc.)."),
    "skill_md_path": (CONTRACT, "Absolute path to SKILL.md used for the packet."),
    "skill_md_sha256": (CONTRACT, "SHA-256 of SKILL.md for verify consumers."),
    "skill_md": (CONTRACT, "Full SKILL.md body for immediate session use."),
    "surface_targets": (CONTRACT, "Per-surface link destinations the packet covers."),
    "instructions": (INFO, "Human guidance for using the packet in-session."),
}
FIELD_NOTES["skill_on.activation_packet"] = dict(_FIELD_NOTES_SKILL_ACTIVATION_PACKET)

_FIELD_NOTES_SKILL_LIFECYCLE_ACTION = {
    "op": (CONTRACT, "Lifecycle op: link or unlink."),
    "skill": (CONTRACT, "Skill name for this action row."),
    "source": (CONTRACT, "Source path for link rows; prior target for unlink rows."),
    "destination": (CONTRACT, "Installed symlink path affected."),
    "scope": (CONTRACT, "project or global scope of the action."),
    "surface": (CONTRACT, "claude or codex surface."),
    "status": (CONTRACT, "Dry-run/applied status (would_link, would_unlink, linked, ...)."),
    "blocked_reason": (CONTRACT, "Empty when allowed; otherwise why the row is blocked."),
    "reason": (INFO, "Human reason for unlink (e.g. pin_off, prune)."),
    "repo_path": (CONTRACT, "Repo root owning the destination."),
    "root": (CONTRACT, "Skills root directory under the repo for link rows."),
    "existing": (CONTRACT, "Prior install state at the destination."),
    "category": (INFO, "Project category when scoped by category."),
    "source_bucket": (INFO, "Source bucket for link rows."),
    "layer": (INFO, "Layer id for unlink rows derived from visibility."),
}
FIELD_NOTES["skill_on.action"] = dict(_FIELD_NOTES_SKILL_LIFECYCLE_ACTION)
FIELD_NOTES["skill_off.action"] = dict(_FIELD_NOTES_SKILL_LIFECYCLE_ACTION)

FIELD_NOTES["skill_on.summary"] = {
    "actions": (CONTRACT, "Total planned/applied action rows."),
    "link": (CONTRACT, "Link action count."),
    "unlink": (CONTRACT, "Unlink action count."),
    "blocked": (CONTRACT, "Blocked action count."),
    "skipped": (CONTRACT, "Skipped action count."),
    "applied": (CONTRACT, "Applied action count (apply mode)."),
    "unchanged": (CONTRACT, "Actions that left destination unchanged."),
}
FIELD_NOTES["skill_off.summary"] = dict(FIELD_NOTES["skill_on.summary"])

FIELD_NOTES["skill_togglable"] = {
    "cwd": (CONTRACT, "Absolute resolved cwd the switchboard was computed for."),
    "items": (CONTRACT, "Every flippable skill at this cwd; see the item field table."),
}
FIELD_NOTES["skill_togglable.item"] = {
    "skill": (CONTRACT, "Skill name."),
    "state": (CONTRACT, "on | off | missing_for_cwd | pinned_on | pinned_off."),
    "source": (CONTRACT, "Installed path when on; null when absent."),
    "pinned_by": (CONTRACT, "override when repo override lists drive state; else policy."),
    "command_to_flip": (CONTRACT, "Literal sbp skill on/off command to transition state."),
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
    from fixture_fleet import build_fixture_fleet

    tmp = tempfile.mkdtemp()
    try:
        fleet = build_fixture_fleet(tmp)
        source_roots = (str(fleet.skills_root), str(fleet.skills_private_root))
        machines_config = MachinesConfig(
            machines={
                "devbox-like": MachineProfile(
                    machine_id="devbox-like",
                    repo_roots=(str(fleet.aliased_root),),
                ),
                "mac-like": MachineProfile(
                    machine_id="mac-like",
                    repo_roots=("/fake-mac-root/repos",),
                ),
            },
            source_path=str(fleet.machines_path),
        )
        registry_doctor = SimpleNamespace(
            DEFAULT_REGISTRY=str(fleet.registry_path),
            load_registry=lambda _path: {"repos": []},
        )
        with fleet._home_patched(), mock.patch.object(
            sv, "DEFAULT_SKILL_SOURCE_ROOT_PATTERNS", source_roots
        ), mock.patch.object(
            sv,
            "_machines_classifier_override",
            lambda: (machines_config, "devbox-like"),
            create=True,
        ), mock.patch.object(
            sv,
            "_registry_doctor_module_override",
            lambda: registry_doctor,
            create=True,
        ), mock.patch.object(
            sv,
            "_explain_machine_profile",
            lambda: {
                "resolved": True,
                "machine_id": "devbox-like",
                "source_path": str(fleet.machines_path),
                "declared_machines": sorted(machines_config.machines),
            },
            create=True,
        ):
            payload = fn(fleet, tmp)
        replacements = [
            (str(Path(tmp) / "fake-mac-root"), REMOTE_ROOT_PLACEHOLDER),
            ("/fake-mac-root", REMOTE_ROOT_PLACEHOLDER),
            (tmp, FLEET_PLACEHOLDER),
        ]
        br_bin = shutil.which("br")
        if br_bin:
            replacements.append((br_bin, BR_BIN_PLACEHOLDER))
        return _norm(payload, replacements)
    finally:
        # Best-effort cleanup; the temp tree carries no operator state.
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


def example_capabilities() -> dict[str, Any]:
    """`sbp capabilities --json` from the real wrapper."""
    env = os.environ.copy()
    env.setdefault("TERM", "dumb")
    env["SKILLBOX_ROOT"] = str(ROOT_DIR)
    result = subprocess.run(
        ["bash", str(ROOT_DIR / "scripts" / "sbp"), "capabilities", "--json"],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return _norm(json.loads(result.stdout), [(str(ROOT_DIR), RUNTIME_ROOT_PLACEHOLDER)])


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


_ACTIONABLE_RECALIBRATE_ISSUE_TYPES = (
    "missing_for_cwd",
    "scope_violations",
    "global_not_allowed",
    "extra_global",
    "broken_global",
    "broken_project",
)

_DRY_RUN_PREVIEW_KEYS = (
    "action",
    "skill",
    "cwd",
    "dry_run",
    "summary",
    "actions",
    "activation_packet",
    "warnings",
    "noop",
    "changed",
)


def _extract_fix_links(preview: dict[str, Any] | None) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for action in (preview or {}).get("actions") or []:
        if action.get("op") != "link":
            continue
        links.append({
            "skill": action.get("skill"),
            "source": action.get("source"),
            "destination": action.get("destination"),
            "surface": action.get("surface"),
            "scope": action.get("scope"),
            "status": action.get("status"),
        })
    return links


def _trim_dry_run_preview(preview: dict[str, Any] | None) -> dict[str, Any] | None:
    if not preview:
        return None
    return {key: preview[key] for key in _DRY_RUN_PREVIEW_KEYS if key in preview}


def _preview_recalibrate_fix(
    model: dict[str, Any],
    issue_type: str,
    row: dict[str, Any],
    *,
    cwd: str,
) -> dict[str, Any] | None:
    skill_name = str(row.get("name") or "")
    if not skill_name:
        return None
    cwd_text = str(cwd)
    if issue_type == "missing_for_cwd":
        plan = lc.skill_lifecycle_plan(
            model,
            "activate",
            skill_name=skill_name,
            cwd=cwd_text,
            to="project",
            force=True,
        )
    elif issue_type == "scope_violations":
        plan = lc.skill_lifecycle_plan(
            model,
            "prune",
            skill_name=skill_name,
            cwd=cwd_text,
            from_scope="project",
        )
    elif issue_type in {"global_not_allowed", "extra_global"}:
        plan = lc.skill_lifecycle_plan(
            model,
            "prune",
            skill_name=skill_name,
            cwd=cwd_text,
            from_scope="global",
        )
    elif issue_type in {"broken_global", "broken_project"}:
        from_scope = "global" if issue_type == "broken_global" else "project"
        plan = lc.skill_lifecycle_plan(
            model,
            "prune",
            skill_name=skill_name,
            cwd=cwd_text,
            from_scope=from_scope,
        )
    else:
        return None
    return lc.apply_skill_lifecycle_plan(plan, dry_run=True)


def build_recalibrate_fixes(
    model: dict[str, Any],
    issues: dict[str, list[dict[str, Any]]],
    *,
    cwd: str,
) -> list[dict[str, Any]]:
    fixes: list[dict[str, Any]] = []
    for issue_type in _ACTIONABLE_RECALIBRATE_ISSUE_TYPES:
        for row in (issues or {}).get(issue_type) or []:
            if not isinstance(row, dict):
                continue
            skill_name = str(row.get("name") or "")
            command = str(row.get("fix_command") or "").strip()
            if not skill_name or not command:
                continue
            preview = _preview_recalibrate_fix(model, issue_type, row, cwd=cwd)
            trimmed = _trim_dry_run_preview(preview)
            fixes.append({
                "problem": issue_type,
                "skill": skill_name,
                "command": command,
                "links": _extract_fix_links(trimmed),
                "dry_run_preview": trimmed,
                "packet_on_apply": (trimmed or {}).get("activation_packet"),
            })
    fixes.sort(key=lambda item: (item["problem"], item["skill"]))
    return fixes


def assemble_recalibrate_payload(
    visibility_payload: dict[str, Any],
    *,
    model: dict[str, Any],
) -> dict[str, Any]:
    issues = sv._compact_skill_visibility_issues(visibility_payload)
    cwd = str(visibility_payload.get("cwd") or "")
    return {
        "cwd": cwd,
        "matched_scope_rules": visibility_payload.get("matched_scope_rules") or [],
        "matched_project_categories": visibility_payload.get("matched_project_categories") or [],
        "issues": issues,
        "beads": visibility_payload.get("beads") or {},
        "summary": visibility_payload.get("summary") or {},
        "fixes": build_recalibrate_fixes(model, issues, cwd=cwd),
        "recommendations": visibility_payload.get("recommendations") or [],
        "next_actions": visibility_payload.get("next_actions") or [],
    }


def emit_recalibrate_json(
    *,
    cwd: str,
    profile: str = "local-all",
    client: str | None = None,
    extra_argv: list[str] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Live ``sbp recalibrate --json`` payload against the operator runtime."""
    root = runtime_root or ROOT_DIR
    manage = root / ".env-manager" / "manage.py"
    argv = ["python3", str(manage), "skills"]
    if client:
        argv.extend(["--client", client])
    argv.extend([
        "--profile", profile,
        "--cwd", cwd,
        "--issues-only",
        "--no-global",
        "--format", "json",
    ])
    argv.extend(extra_argv or [])
    result = subprocess.run(
        argv,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"skills --issues-only --format json failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    visibility_payload = json.loads(result.stdout)
    model = build_runtime_model(root)
    return assemble_recalibrate_payload(visibility_payload, model=model)


def example_recalibrate() -> dict[str, Any]:
    """`sbp recalibrate --json` at the overlay repo (missing_for_cwd fix row)."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        payload = sv.collect_skill_visibility(
            fleet.model(), cwd=str(fleet.repo("overlay-repo")),
            include_global=False, include_project=True, include_sources=False,
        )
        return assemble_recalibrate_payload(payload, model=fleet.model())

    return _fleet_example(run)


def _skill_toggle_args(
    fleet: FixtureFleetT,
    *,
    repo: str,
    action: str,
    skill_name: str,
    dry_run: bool = True,
) -> Namespace:
    return Namespace(
        skill_action=action,
        skill_name=skill_name,
        cwd=str(fleet.repo(repo)),
        to="project",
        from_scope="project",
        category=[],
        source=None,
        dry_run=dry_run,
        verify=False,
        allow_directories=False,
        force=False,
    )


def _skill_toggle_payload(
    fleet: FixtureFleetT,
    *,
    repo: str,
    action: str,
    skill_name: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    args = _skill_toggle_args(
        fleet, repo=repo, action=action, skill_name=skill_name, dry_run=dry_run,
    )
    return runtime_cli._handle_skill_toggle(args, fleet.model(), dry_run=dry_run)


def build_skill_togglable_payload(
    model: dict[str, Any],
    *,
    cwd: str | Path,
) -> dict[str, Any]:
    """Write-affordance switchboard: every skill flippable at one cwd."""
    return runtime_cli._build_skill_togglable_payload(model, cwd=cwd)


def example_skill_why() -> dict[str, Any]:
    """`sbp skill why <skill>` — absence diagnosis with exact fix command."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        return sv.explain_skill_visibility(
            fleet.model(),
            "needs-beads",
            cwd=str(fleet.repo("overlay-repo")),
            include_global=False,
            include_project=True,
        )

    return _fleet_example(run)


def example_skill_on() -> dict[str, Any]:
    """`sbp skill on <skill> --dry-run --format json` — missing_for_cwd link preview."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        return _skill_toggle_payload(
            fleet, repo="overlay-repo", action="on", skill_name="tiny-ui", dry_run=True,
        )

    return _fleet_example(run)


def example_skill_off() -> dict[str, Any]:
    """`sbp skill off <skill> --dry-run --format json` — unlink preview for an installed skill."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        return _skill_toggle_payload(
            fleet,
            repo="overlay-repo",
            action="off",
            skill_name="tiny-marketing",
            dry_run=True,
        )

    return _fleet_example(run)


def example_skill_togglable() -> dict[str, Any]:
    """`sbp skill togglable --json` — write-affordance switchboard at the overlay repo."""

    def run(fleet: FixtureFleetT, _tmp: str) -> dict[str, Any]:
        return build_skill_togglable_payload(
            fleet.model(), cwd=fleet.repo("overlay-repo"),
        )

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
        "key": "capabilities",
        "command": "sbp capabilities",
        "long": "`sbp capabilities --json`",
        "fn": "scripts/sbp print_capabilities",
        "intro": (
            "The wrapper discovery contract. Agents should start here to learn the stable "
            "command inventory, stdout/stderr rules, dry-run guidance, and the machine-readable "
            "`skill_verbs` decision map for choosing between recalibrate/activate/sync/prune/"
            "on/off/heal/why/togglable and maintenance verbs."
        ),
        "example": example_capabilities,
        "nested": [("capabilities.skill_verb", "`skill_verbs.<verb>` (one skill verb row)", None)],
    },
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
        "long": "`sbp recalibrate [--cwd <repo>] --json`",
        "fn": "assemble_recalibrate_payload (issues-only view + fixes[] dry-run previews)",
        "intro": (
            "Machine-actionable cwd recalibration. `sbp recalibrate --json` emits the "
            "issues-focused `collect_skill_visibility` core (same fields as "
            "`sbp skills --issues-only --format json`) plus a `fixes[]` row per actionable "
            "issue. Each fix carries the literal `fix_command`, link dry-run rows, a trimmed "
            "`dry_run_preview`, and `packet_on_apply` when linking would return an activation "
            "packet. Bare `sbp recalibrate` (no `--json`) still prints the composite human "
            "surface (sync/prune dry-runs, beads graph, MCP audit)."
        ),
        "example": example_recalibrate,
        "nested": [
            ("recalibrate.beads", "`beads` (beads requirement/readiness)", None),
            ("recalibrate.fix", "`fixes[]` (one machine-actionable remediation row)", None),
        ],
    },
    {
        "key": "skill_why",
        "command": "sbp skill why",
        "long": "`sbp skill why <skill> [--cwd <repo>] --format json`",
        "fn": "explain_skill_visibility",
        "intro": (
            "Read-only provenance for ONE skill at ONE cwd, including absence. Same payload "
            "shape as `sbp explain` but routed through the `skill why` verb. Walks the "
            "precedence spine, names the winning layer (if any), and — when invisible — emits "
            "ranked remediation rows with literal `command` strings agents can run without "
            "re-deriving policy."
        ),
        "example": example_skill_why,
        "nested": [],
    },
    {
        "key": "skill_on",
        "command": "sbp skill on",
        "long": "`sbp skill on <skill> [--cwd <repo>] [--dry-run] --format json`",
        "fn": "_handle_skill_toggle (on / activate plan + override pin_on)",
        "intro": (
            "Durable repo-local pin ON plus disk links. Writes `pin_on` to "
            "`.skillbox/skill-overrides.yaml` (survives recalibrate) and links project "
            "skills when needed. Returns an `activation_packet` for immediate session use. "
            "`--dry-run` previews override + link actions without writing; a repeat apply "
            "is a clean no-op (`noop: true`)."
        ),
        "example": example_skill_on,
        "nested": [
            ("skill_on.override", "`override` (repo override-file mutation)", None),
            ("skill_on.activation_packet", "`activation_packet` (immediate SKILL.md packet)", None),
            ("skill_on.action", "`actions[]` (one lifecycle link/unlink row)", None),
            ("skill_on.summary", "`summary` (action counters)", None),
        ],
    },
    {
        "key": "skill_off",
        "command": "sbp skill off",
        "long": "`sbp skill off <skill> [--cwd <repo>] [--dry-run] --format json`",
        "fn": "_handle_skill_toggle (off / prune plan + override pin_off)",
        "intro": (
            "Durable repo-local pin OFF plus project unlink. Writes `pin_off` to "
            "`.skillbox/skill-overrides.yaml` and unlinks project installs. Refuses floor "
            "skills (smart/sbp). `--dry-run` previews override + unlink rows; `activation_packet` "
            "is always null."
        ),
        "example": example_skill_off,
        "nested": [
            ("skill_off.override", "`override` (repo override-file mutation)", None),
            ("skill_off.action", "`actions[]` (one lifecycle unlink row)", None),
            ("skill_off.summary", "`summary` (action counters)", None),
        ],
    },
    {
        "key": "skill_togglable",
        "command": "sbp skill togglable",
        "long": "`sbp skill togglable [--cwd <repo>] --format json`",
        "fn": "build_skill_togglable_payload",
        "intro": (
            "Write-affordance switchboard for one cwd: every skill the policy marks as "
            "flippable here, its current state (`on`, `off`, `missing_for_cwd`, `pinned_on`, "
            "`pinned_off`), who pinned it (`override` vs `policy`), and the literal "
            "`command_to_flip` to transition state. Distinct from `sbp skills` (visibility/read)."
        ),
        "example": example_skill_togglable,
        "nested": [("skill_togglable.item", "`items[]` (one flippable skill row)", None)],
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
    if leaf == "fix":
        fixes = example.get("fixes") or []
        return fixes[0] if fixes else None
    if leaf == "override":
        return example.get("override") or None
    if leaf == "activation_packet":
        return example.get("activation_packet") or None
    if leaf == "action":
        actions = example.get("actions") or []
        return actions[0] if actions else None
    if leaf == "summary" and isinstance(example.get("summary"), dict):
        return example.get("summary") or None
    if leaf == "item":
        items = example.get("items") or []
        return items[0] if items else None
    if leaf == "repo":
        repos = example.get("repos") or []
        return repos[0] if repos else None
    if leaf == "skill_verb":
        return (example.get("skill_verbs") or {}).get("on") or None
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
        lines.append(f"**Invocation:** {surface['long']}")
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
            f"`{FLEET_PLACEHOLDER}` / `{RUNTIME_ROOT_PLACEHOLDER}` / "
            f"`{BR_BIN_PLACEHOLDER}` / `{REMOTE_ROOT_PLACEHOLDER}`.</sub>")
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
    parser.add_argument(
        "--recalibrate-json",
        action="store_true",
        help="Emit live sbp recalibrate --json payload to stdout (used by scripts/sbp).",
    )
    parser.add_argument("--cwd", default=None, help="Cwd for --recalibrate-json.")
    parser.add_argument("--profile", default="local-all", help="Profile for --recalibrate-json.")
    parser.add_argument("--client", default=None, help="Optional client overlay for --recalibrate-json.")
    parser.add_argument(
        "extra",
        nargs="*",
        help="Extra argv tokens forwarded to skills --issues-only (after --recalibrate-json flags).",
    )
    args = parser.parse_args(argv)
    if args.recalibrate_json:
        if not args.cwd:
            print("gen_output_schemas: --recalibrate-json requires --cwd", file=sys.stderr)
            return 2
        payload = emit_recalibrate_json(
            cwd=args.cwd,
            profile=args.profile,
            client=args.client,
            extra_argv=list(args.extra),
        )
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 0
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
