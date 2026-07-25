"""Deterministic inventory of every public state-root mutation boundary.

This module answers one question for every *public* surface Skillbox exposes —
the ``manage`` CLI, the standalone ``pulse`` daemon CLI, the ``box`` infra CLI,
the operator MCP server, and the ``Makefile`` — namely:

    when an operator or agent runs this, what persistent state can change,
    under which predicate, and who owns the final write?

It is an **inventory and a ratchet, not a lock**. Nothing here acquires,
releases, or brokers a lock; :mod:`runtime_manager.state_mutation` is
standard-library only and imports nothing from the rest of ``runtime_manager``
at module scope, exactly like :mod:`runtime_manager.command_registry`. The
authoritative reentrant state-root mutation lease is a *separate*, later piece
of work that consumes the boundary IDs declared here.

Why this exists (verified substrate, re-confirmed against the tree)
------------------------------------------------------------------

1. **The two "locked" helpers are per-file, not per-state-root.**
   ``_shared/fs.locked_json_update`` (``_shared/fs.py:329``) flocks a sidecar
   ``<path>.lock``; ``lib/opslib.locked_inventory_update``
   (``scripts/lib/opslib.py:137``) flocks ``<inventory>.lock``. Neither
   excludes a writer touching a *different* file under the same state root, so
   neither is a state-root mutex.

2. **Focus and pulse do NOT serialize against each other**, despite comments
   claiming they do. ``workflows.py:3509-3511`` says focus writes are
   serialized "against the pulse-write window" and ``pulse.py:1493-1498`` says
   the pulse snapshot is serialized "against focus writers" — but focus locks
   ``workspace/.focus.json.lock`` (``runtime_ops.py:6094`` ``FOCUS_STATE_REL``)
   while pulse locks ``logs/runtime/pulse.state.json.lock``
   (``pulse.py:199`` + ``pulse.py:1479``). Different sidecars: zero mutual
   exclusion.

3. **Sessions, workers, the backup drill, and restore all write outside both
   helpers.** ``_shared/session.py:201`` uses ``write_text_file``;
   ``_shared/worker.py:547`` uses ``atomic_write_text`` and ``worker.py:793``
   opens append handles; ``state_backup.py:806`` writes drill evidence via
   ``_write_json_0600``; ``state_backup.py:853`` does ``shutil.rmtree`` on the
   previous state root during restore. None of them take any lock.

Non-goals
---------

* **No locking is implemented here.** ``lock_owner`` records who owns the final
  write *today*; ``UNOWNED`` is the honest and common answer.
* **Not every atomic helper is a public boundary.** ``atomic_write_json``,
  ``write_json_file``, ``write_text_file``, ``_append_jsonl`` and friends are
  write *primitives*. A boundary is a surface an operator or agent can invoke
  by name. Primitives appear only as evidence.

Completeness contract
---------------------

:data:`MANIFEST` must classify exactly the surfaces that
:func:`enumerate_live_surfaces` finds in the tree. ``tests/
test_state_mutation_inventory.py`` fails on any drift in either direction, so a
new ``manage`` subcommand, ``box`` verb, MCP tool, or Make target cannot land
unclassified.

:data:`OWNED_GAPS` records surfaces that could **not** be classified without
executing a mutation or guessing an ambiguous dry-run predicate. While any gap
is open, :func:`inventory_complete` returns ``False``. That is the intended
terminal state for an honest inventory — it is not a failure to paper over.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

MANIFEST_SCHEMA_VERSION = "2026-07-25+state-mutation-boundaries.v1"

# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------

#: A surface never writes persistent state.
READ = "read"
#: A surface exposes a dry-run affordance and writes **nothing** when it is set.
TRUE_DRY_RUN = "true_dry_run"
#: A surface always writes when it succeeds; there is no preview mode.
UNCONDITIONAL_MUTATION = "unconditional_mutation"
#: A surface writes only under a predicate (a flag, a confirmation, or observed
#: state). Includes surfaces whose "dry run" is NOT write-free.
CONDITIONAL_MUTATION = "conditional_mutation"

CLASSIFICATIONS = (READ, TRUE_DRY_RUN, UNCONDITIONAL_MUTATION, CONDITIONAL_MUTATION)
MUTATING_CLASSIFICATIONS = (TRUE_DRY_RUN, UNCONDITIONAL_MUTATION, CONDITIONAL_MUTATION)

SURFACE_MANAGE = "manage"
SURFACE_PULSE = "pulse"
SURFACE_BOX = "box"
SURFACE_OPERATOR_MCP = "operator_mcp"
SURFACE_MAKE = "make"

SURFACE_KINDS = (
    SURFACE_MANAGE,
    SURFACE_PULSE,
    SURFACE_BOX,
    SURFACE_OPERATOR_MCP,
    SURFACE_MAKE,
)

SURFACE_ENTRYPOINTS = {
    SURFACE_MANAGE: ".env-manager/manage.py",
    SURFACE_PULSE: ".env-manager/pulse.py",
    SURFACE_BOX: "scripts/box.py",
    SURFACE_OPERATOR_MCP: "scripts/operator_mcp_server.py",
    SURFACE_MAKE: "Makefile",
}

#: Not a boundary field — the set of state-root resolvers the boundaries below
#: name. Five *different* expressions resolve the same ``SKILLBOX_STATE_ROOT``
#: env var with three *different* fallbacks (cwd-relative, repo-relative, and
#: model-driven). Any future single-writer lease has to pick one of these as
#: canonical; today they can disagree whenever the process cwd is not the repo
#: root.
STATE_ROOT_SOURCES: Mapping[str, str] = {
    "n/a": "boundary writes no persistent state",
    "cli.skill_default_review": (
        "cli.py:4984 _skill_default_review_dir -> env SKILLBOX_STATE_ROOT else "
        "'.skillbox-state' resolved against Path.cwd() (CWD-RELATIVE)"
    ),
    "state_backup.state_root": (
        "state_backup.py:72 _resolve_state_root -> explicit arg, model storage.state_root, "
        "then env STATE_ROOT_ENV=SKILLBOX_STATE_ROOT (state_backup.py:23)"
    ),
    "state_backup.backup_root": (
        "state_backup.py:95 _resolve_backup_root -> env SKILLBOX_BACKUP_ROOT; refuses a root "
        "inside the state root (state_backup.py:109)"
    ),
    "workflows.stewardship": (
        "workflows.py:2386 _stewardship_state_root -> model storage.state_root else env "
        "SKILLBOX_STATE_ROOT else None (no fallback path at all)"
    ),
    "opslib.inventory": (
        "scripts/lib/opslib.py:235 resolve_inventory_path -> env SKILLBOX_STATE_ROOT else "
        "<repo>/.skillbox-state; the inventory itself is <repo>/workspace/boxes.json unless "
        "SKILLBOX_BOX_INVENTORY overrides it (opslib.py:239)"
    ),
    "box.state_root": (
        "scripts/box.py:797 -> env SKILLBOX_STATE_ROOT else './.skillbox-state' (CWD-RELATIVE)"
    ),
    "opmcp.state_root": (
        "scripts/operator_mcp_server.py:670 -> env SKILLBOX_STATE_ROOT else './.skillbox-state' "
        "(CWD-RELATIVE); also roots the dry-run marker dir (operator_mcp_server.py:1500)"
    ),
    "selftest.state_root": (
        "scripts/self-test.sh:178 ${SKILLBOX_STATE_ROOT:-${REPO_ROOT}/.skillbox-state} "
        "(REPO-RELATIVE — disagrees with the cwd-relative resolvers above)"
    ),
    "runtime_model.root_dir": (
        "runtime model root_dir (scripts/lib/runtime_model.py:539 default './.skillbox-state' for "
        "storage.state_root); repo-tracked paths under <root_dir>, not a state root"
    ),
    "home": "$HOME — ~/.claude, ~/.skillbox-state, user crontab; outside every state root",
    "remote": "the state root ON THE REMOTE BOX; not resolvable from the operator host",
    "external": "state owned by a process outside this repo (docker, git target repo, HTTP service)",
}

# --------------------------------------------------------------------------
# Boundary record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Boundary:
    """One public surface and everything a future lease needs to know about it.

    ``boundary_id`` is the stable name other contracts reference. Everything
    else is evidence. Mutating boundaries MUST carry a real
    ``state_root_source``, ``dry_run_predicate``, ``nested_call_policy``,
    ``lease_span``, and ``lock_owner``; read boundaries leave them ``"n/a"``.
    """

    boundary_id: str
    surface: str
    key: str
    classification: str
    entry_points: tuple[str, ...]
    state_root_source: str = "n/a"
    dry_run_predicate: str = "n/a"
    nested_call_policy: str = "leaf"
    lease_span: str = "n/a"
    lock_owner: str = "n/a"
    writes: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    delegates_to: tuple[str, ...] = ()
    gap: str = ""

    @property
    def is_mutation(self) -> bool:
        return self.classification in MUTATING_CLASSIFICATIONS

    @property
    def is_gap(self) -> bool:
        return bool(self.gap)


def _b(
    surface: str,
    key: str,
    classification: str,
    *,
    entry_points: Iterable[str] | None = None,
    state_root_source: str = "n/a",
    dry_run_predicate: str = "n/a",
    nested_call_policy: str = "leaf",
    lease_span: str = "n/a",
    lock_owner: str = "n/a",
    writes: Iterable[str] = (),
    evidence: Iterable[str] = (),
    delegates_to: Iterable[str] = (),
    gap: str = "",
) -> Boundary:
    boundary_id = f"{surface}.{key.replace(' ', '.')}"
    entries = tuple(entry_points) if entry_points else (
        f"{SURFACE_ENTRYPOINTS[surface]} {key}" if surface != SURFACE_MAKE else f"make {key}",
    )
    return Boundary(
        boundary_id=boundary_id,
        surface=surface,
        key=key,
        classification=classification,
        entry_points=entries,
        state_root_source=state_root_source,
        dry_run_predicate=dry_run_predicate,
        nested_call_policy=nested_call_policy,
        lease_span=lease_span,
        lock_owner=lock_owner,
        writes=tuple(writes),
        evidence=tuple(evidence),
        delegates_to=tuple(delegates_to),
        gap=gap,
    )


def _read(surface: str, key: str, *evidence: str, **kw: Any) -> Boundary:
    return _b(surface, key, READ, evidence=evidence, **kw)


# Shared lock-owner vocabulary. These strings are the *current* reality.
UNOWNED = "UNOWNED — no lock serializes this write"
LOCK_FOCUS = (
    "flock workspace/.focus.json.lock via _shared/fs.locked_json_update (fs.py:329); "
    "PER-FILE ONLY — excludes no other state-root writer"
)
LOCK_PULSE = (
    "flock logs/runtime/pulse.state.json.lock via _shared/fs.locked_json_update (fs.py:329); "
    "PER-FILE ONLY — does NOT exclude focus despite pulse.py:1493 comment"
)
LOCK_INVENTORY = (
    "flock workspace/boxes.json.lock via lib/opslib.locked_inventory_update (opslib.py:137) "
    "from box.save_inventory (box.py:2089); PER-FILE ONLY"
)
LOCK_OVERRIDES = (
    "flock on <repo>/.skillbox/skill-overrides.yaml via "
    "policy_eval.update_repo_override_policy (policy_eval.py:1273-1313); PER-FILE ONLY"
)
LOCK_SELFTEST = "flock ${SKILLBOX_STATE_ROOT}/self-test/toolchain/.lock (scripts/self-test.sh:186)"
MARKER_NOT_A_LOCK = (
    "dry-run marker under ${STATE_ROOT}/dryrun-markers "
    "(operator_mcp_server.py:1500) — an advisory consent stamp, NOT a lock"
)

# --------------------------------------------------------------------------
# manage CLI — 98 leaf surfaces
# --------------------------------------------------------------------------

_MANAGE_READ = (
    ("capabilities", "cli.py:3512 _handle_capabilities -> command_registry.registry_payload()"),
    ("client-diff", "publish.py:854 diff_client_bundle; candidate bundle built in tempfile.TemporaryDirectory (publish.py:874)"),
    ("client-open", ""),  # placeholder replaced below (mutation) — never emitted
    ("distribution-preview", "distribution/preview.py:57 preview_manifest; reads the lockfile only"),
    ("doctor", "cli.py:4023 -> runtime_ops.py:1856 doctor_results; check_filesystem (runtime_ops.py:629) only scans"),
    ("explain", "cli.py:4406 _handle_explain -> agent_decisions.explain_payload"),
    ("fleet converge", "cli.py:4138 _handle_fleet_converge -> fleet_converge.py:740 build_fleet_converge_plan; zero write primitives in fleet_converge.py"),
    ("forge status", "forge.py:407 forge_status"),
    ("graph", "cli.py:4355 -> agent_graph_engine.graph_command_payload"),
    ("logs", "cli.py:7158 -> runtime_ops.py:5987 collect_service_logs (tail only)"),
    ("mcp-audit", "cli.py:4107 -> mcp_visibility.py:220 collect_mcp_audit; only os.readlink (mcp_visibility.py:80)"),
    ("next", "cli.py:4338 -> agent_decisions.next_action_payload"),
    ("operator-booking availability", "operator_booking.py:57; HTTP GET only, no local write"),
    ("operator-booking config", "operator_booking.py:57; HTTP GET only, no local write"),
    ("operator-booking list", "operator_booking.py:57; HTTP GET only, no local write"),
    ("operator-booking times", "operator_booking.py:57; HTTP GET only, no local write"),
    ("overlay list", "cli.py:6644 _handle_overlay list branch"),
    ("parity-report", "cli.py:4728 -> parity_report.py:747; no mkdir/write in parity_report.py"),
    ("ports", "cli.py:4012 -> port_registry.port_registry_payload"),
    ("pressure-report", "cli.py:3611 -> pressure_report.py:353; subprocess du/pgrep probes only"),
    ("rch-report", "cli.py:3625 -> rch_report.py:155; subprocess `rch status --json` probes only"),
    ("render", "cli.py:3973 _handle_render"),
    ("robot-docs", "cli.py:3530 _handle_robot_docs"),
    ("robot-triage", "cli.py:3539 _handle_robot_triage -> build_runtime_model"),
    ("sbh-report", "cli.py:3706 -> sbh_report.py:300; subprocess `sbh doctor/status` probes only"),
    ("search", "cli.py:4497 -> agent_search.search_payload; no write primitives in agent_search.py"),
    ("session-status", "cli.py:3078 -> _shared/session.py:514 session_status_payload"),
    ("skill lint", "cli.py:6289 branch"),
    ("skill plan", "cli.py:6338 `dry_run = bool(args.dry_run or skill_action == \"plan\")` forces preview"),
    ("skill togglable", "cli.py:6320 branch"),
    ("skill toggleable", "cli.py:6320 branch (alias of togglable)"),
    ("skill what-if", "cli.py:6327 branch"),
    ("skill why", "cli.py:6301 branch"),
    ("skill-audit", "cli.py:4077 -> collect_skill_audit"),
    ("skills", "cli.py:4048 -> collect_skill_visibility"),
    ("snap actions", "cli.py:4677 branch"),
    ("snap diff", "cli.py:4693 branch"),
    ("snap replay", "cli.py:4707 branch"),
    ("state-backup verify", "state_backup.py:593 verify_state_backup; hashes an existing archive"),
    ("status", "cli.py:4038 -> runtime_status"),
    (
        "structure-doctor",
        "cli.py:3821 -> structure_doctor.py:574; writes no state. CAVEAT: shells out to "
        "pytest (structure_doctor.py:204) which drops __pycache__ bytecode dirs — "
        "incidental, not state-root state",
    ),
    ("worker-status", ""),  # placeholder replaced below (mutation) — never emitted
)

_MANAGE_BOUNDARIES: tuple[Boundary, ...] = tuple(
    _read(SURFACE_MANAGE, key, ev) for key, ev in _MANAGE_READ if ev
) + (
    _b(
        SURFACE_MANAGE, "acceptance", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE — `acceptance` has no --dry-run; the sync+focus argv are hardcoded",
        nested_call_policy="reenters_subprocess: manage.sync, manage.focus (re-entrant manage.py fork)",
        lease_span="whole_command (spans two nested manage.py processes)",
        lock_owner=UNOWNED + " at the acceptance level; the nested focus write takes " + LOCK_FOCUS,
        writes=("everything manage.sync writes", "workspace/.focus.json", "CLAUDE.md / AGENTS.md"),
        evidence=(
            "cli.py:2633 _handle_acceptance -> workflows.py:2021 run_acceptance",
            "workflows.py:1767-1768 hardcoded sync_args / focus_args",
            "workflows.py:1831 run_manage_json_command re-entry",
        ),
    ),
    _b(
        SURFACE_MANAGE, "bootstrap", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if dry_run:` runtime_ops.py:5185 (run_tasks early-continue); `if not dry_run:` runtime_ops.py:2365; per-write `if dry_run: return` _shared/fs.py:237",
        nested_call_policy="reenters_inprocess: sync_runtime + run_tasks; task commands are subprocess.Popen(shell=True)",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("repo clones", "artifacts", "env files", "port contracts", "log dirs", "skill lockfiles", "task logs"),
        evidence=("cli.py:6674", "runtime_ops.py:2349 sync_runtime", "runtime_ops.py:5142 run_tasks", "runtime_ops.py:5198 subprocess.Popen(shell=True)"),
    ),
    _b(
        SURFACE_MANAGE, "cass-evidence", CONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="UNKNOWN — lives in the out-of-repo helper",
        nested_call_policy="delegates_external: $SKILLBOX_CONFIG_ROOT/scripts/sbp_evidence.py",
        lease_span="unknown",
        lock_owner="unknown (external process)",
        writes=("unknown — the --proposals path is implemented outside this repo",),
        evidence=(
            "cli.py:3894 _handle_cass_evidence",
            "cli.py:3906 helper = config_root / 'scripts' / 'sbp_evidence.py'",
            "cli.py:3919-3921 `if getattr(args, \"proposals\", False): cmd += [\"--proposals\"]`",
            "cli.py:3928 `proc = subprocess.run(cmd, check=False)`",
            "helper absent on this host: ~/repos/skillbox-config/scripts/sbp_evidence.py missing, SKILLBOX_CONFIG_ROOT unset -> cli.py:3907 returns evidence_helper_missing",
        ),
        gap=(
            "BLOCKING: the write behaviour of `--proposals` is implemented in "
            "skillbox-config/scripts/sbp_evidence.py, which is not in this repo and is not "
            "installed on this host. Classifying it would require either executing the "
            "delegate or guessing. Recorded as conditional_mutation on the pessimistic "
            "assumption that --proposals writes."
        ),
    ),
    _b(
        SURFACE_MANAGE, "client-init", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if dry_run:` _shared/fs.py:237 inside write_text_file (called at client_scaffold.py:878); `if not dry_run:` client_scaffold.py:634 for copy_tree_atomic",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("client overlay tree: overlay.yaml, skill-repos.yaml, local/shared skill dirs",),
        evidence=("cli.py:2412", "_shared/client_scaffold.py:808 scaffold_client_overlay", "client_scaffold.py:870 `if existing_paths and not force:`"),
    ),
    _b(
        SURFACE_MANAGE, "client-open", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE — dry_run is hardcoded False and force hardcoded True",
        nested_call_policy="reenters_subprocess: manage.focus; reenters_inprocess: project_client_bundle, sync_context",
        lease_span="whole_command",
        lock_owner=UNOWNED + "; the nested focus write takes " + LOCK_FOCUS,
        writes=("sand/<client-id>/ (replaced wholesale)", "sand/<client-id>/.mcp.json", "CLAUDE.md / AGENTS.md"),
        evidence=(
            "cli.py:2672 -> publish.py:1185 open_client_surface",
            "publish.py:1145-1146 `dry_run=False,` / `force=True,` in _open_client_surface_projected",
            "publish.py:1066-1067 same pair on the bundle path",
        ),
    ),
    _b(
        SURFACE_MANAGE, "client-project", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if not dry_run:` _shared/client.py:647 (metadata), :591 (shutil.copy2), :597 (write_text_file)",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("builds/clients/<client-id>/ payload + projection metadata json",),
        evidence=("cli.py:2643 -> _shared/client.py:603 project_client_bundle", "_shared/client.py:236 `if has_contents and not force:`"),
    ),
    _b(
        SURFACE_MANAGE, "client-publish", CONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NO --dry-run flag exists. Write predicate is the computed diff: `if changed:` publish.py:724",
        nested_call_policy="reenters_subprocess: manage.acceptance when --acceptance; git commit in the target repo when --commit",
        lease_span="whole_command",
        lock_owner=UNOWNED + " (target repo is a foreign git worktree)",
        writes=("<target-dir>/clients/<cid>/current/", "publish.json", "acceptance.json", "deploy.json", "git commit when --commit"),
        evidence=("cli.py:2701 -> publish.py:674 publish_client_bundle", "publish.py:724 `if changed:` -> _apply_publish_changes"),
    ),
    _b(
        SURFACE_MANAGE, "context", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if not dry_run:` context_rendering.py:466 / :479 / :482 / :485 / :492",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("CLAUDE.md", "AGENTS.md symlink", "logs/runtime/runtime.log"),
        evidence=("cli.py:3998 -> context_rendering.py:633 sync_context -> :453 write_agent_context_files",),
    ),
    _b(
        SURFACE_MANAGE, "distribution-publish", CONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NO --dry-run flag. Predicate is content drift: `if changed:` distribution/publish.py:305",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("<artifact-root>/skills/<name>/<version>/bundle.tar.gz", "<manifest-path>"),
        evidence=("cli.py:2763 -> distribution/publish.py:37", "distribution/publish.py:190-191 mkdir + shutil.copyfile", "distribution/publish.py:207-208 atomic_write_text"),
    ),
    _b(
        SURFACE_MANAGE, "distribution-rollback", CONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="`if args.list:` cli.py:2838 returns before any write; there is no --dry-run",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("<install-target>/<skill>/ trees (replaced)", "<lockfile>"),
        evidence=("cli.py:2836 -> distribution/rollback.py:50", "rollback.py:180 install_dir mkdir", "rollback.py:223-224 lockfile atomic_write_text"),
    ),
    _b(
        SURFACE_MANAGE, "down", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if dry_run:` runtime_ops.py:5969 — early continue before stop_process / remove_pid_file / log_runtime_event",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("service pid files (unlinked)", "logs/runtime/runtime.log"),
        evidence=("cli.py:7086 -> runtime_ops.py:5931 stop_services",),
    ),
    _b(
        SURFACE_MANAGE, "evidence", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if getattr(args, \"write\", False):` cli.py:4247 — opt-in write, default off",
        nested_call_policy="leaf",
        lease_span="single_write",
        lock_owner=UNOWNED,
        writes=("tests/artifacts/perf/<run-id>/runtime-evidence/evidence.json", ".../evidence.md"),
        evidence=("cli.py:4239 _handle_evidence", "cli.py:4263 out_dir.mkdir", "cli.py:4266-4267 write_text"),
    ),
    _b(
        SURFACE_MANAGE, "first-box", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE — init_private_repo runs before any conditional",
        nested_call_policy="reenters_subprocess: manage.onboard, manage.acceptance, manage.sync, manage.focus, manage.client-open",
        lease_span="whole_command (spans several nested manage.py processes)",
        lock_owner=UNOWNED,
        writes=("<private-path>/ + git init", "<private-path>/clients/", "repo .env (SKILLBOX_CLIENTS_HOST_ROOT)", "everything the nested boundaries write"),
        evidence=("cli.py:2479 -> workflows.py:739 run_first_box", "workflows.py:496 init_private_repo(...)", "_shared/client.py:925 `ensure_directory(target_dir, dry_run=False)`"),
    ),
    _b(
        SURFACE_MANAGE, "fleet relink", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if apply:` cli.py:4180 -> apply_relink_plan(plan, dry_run=False); inner `if dry_run:` fleet_relink.py:632. Plan-only unless --yes/apply",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("installed skill symlinks repointed in place",),
        evidence=("cli.py:4162 -> fleet_relink.py:405 build_relink_plan / :578 apply_relink_plan",),
    ),
    _b(
        SURFACE_MANAGE, "focus", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE — `focus` has no --dry-run",
        nested_call_policy="reenters_inprocess: sync_runtime, run_tasks, start_services, sync_live_context",
        lease_span=(
            "INTENDED whole_command (sync -> bootstrap -> up -> context -> persist). "
            "ACTUAL lock coverage is only the final single_write of .focus.json"
        ),
        lock_owner=LOCK_FOCUS,
        writes=("workspace/.focus.json", "compose override", "everything manage.sync writes", "service pid files", "CLAUDE.md / AGENTS.md"),
        evidence=(
            "cli.py:2921 -> workflows.py:3736 run_focus",
            "workflows.py:3750 `focus_path = root_dir / FOCUS_STATE_REL` (runtime_ops.py:6094)",
            "workflows.py:3511 `locked_json_update(focus_path, lambda _current: focus_data)`",
            "workflows.py:3509 comment claims serialization 'against the pulse-write window' — FALSE, different sidecar",
        ),
    ),
    _b(
        SURFACE_MANAGE, "forge accept", UNCONDITIONAL_MUTATION,
        state_root_source="home",
        dry_run_predicate="NONE — `forge accept` has no --dry-run (parser cli.py:2181-2185)",
        nested_call_policy="delegates_external: git in the skill's repo",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("git ff-merge of forge/<skill> + branch -d", "~/.claude/forge-decisions.jsonl"),
        evidence=("cli.py:2539 -> forge.py:845 forge_accept", "forge.py:872 merge", "forge.py:903 _append_ledger"),
    ),
    _b(
        SURFACE_MANAGE, "forge init", CONDITIONAL_MUTATION,
        state_root_source="home",
        dry_run_predicate="NO --dry-run. Per-write predicates: `if _session_end_hook_present(...)` forge.py:143; marker/needle presence for the codex patch; `if with_cron:` forge.py:1030",
        nested_call_policy="delegates_external: crontab(1)",
        lease_span="whole_command",
        lock_owner=UNOWNED + " (writes into $HOME, outside every state root)",
        writes=("~/.claude/settings.json (SessionEnd hook)", "~/.claude/skills/codex-tmux/scripts/run.py", "user crontab"),
        evidence=("cli.py:2586 -> forge.py:1002 forge_init", "forge.py:148 _write_json_object", "forge.py:203 run_py.write_text", "forge.py:220 subprocess_run(['crontab','-'])"),
    ),
    _b(
        SURFACE_MANAGE, "forge propose", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if dry_run:` forge.py:800 — sets would_create_branch and returns before any git call or write",
        nested_call_policy="delegates_external: git in the skill's repo",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("git branch forge/<skill> + commit", "<skill>/SKILL.md appended section", "~/.claude/forge-proposals.jsonl"),
        evidence=("cli.py:2506 -> forge.py:728 forge_propose", "forge.py:607 skill_md.write_text", "forge.py:610-613 _append_ledger"),
    ),
    _b(
        SURFACE_MANAGE, "forge reject", UNCONDITIONAL_MUTATION,
        state_root_source="home",
        dry_run_predicate="NONE — only `if not clean_reason:` forge.py:925 raises; then the delete runs",
        nested_call_policy="delegates_external: git in the skill's repo",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("git branch -D forge/<skill>", "~/.claude/forge-decisions.jsonl"),
        evidence=("cli.py:2542 -> forge.py:910 forge_reject", "forge.py:944 `_run_git(repo, [\"branch\", \"-D\", branch], ...)`"),
    ),
    _b(
        SURFACE_MANAGE, "mcp sync", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if apply:` mcp_render.py:611 — --dry-run is the DEFAULT; --apply opts in. Inner skips: `if surface.get(\"refused\"): continue` :614, `if not surface[\"changed\"]: continue` :617",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("<config-root>/.mcp.json", "<config-root>/.codex/config.toml"),
        evidence=("cli.py:4205 _handle_mcp_sync -> mcp_render.py:582 render_mcp_sync", "mcp_render.py:620-621 mkdir + write_text"),
    ),
    _b(
        SURFACE_MANAGE, "mmdx", CONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="`if not open_file:` mmdx_open.py:590 returns before spawning; `--no-parser-install` appended unless --allow-parser-install (mmdx_open.py:449-450)",
        nested_call_policy="delegates_external: workspace/skill-repos/build000r-skills/mmdx/scripts/mmd.py",
        lease_span="UNBOUNDED — the spawned viewer outlives the command",
        lock_owner="unknown (external viewer process)",
        writes=("mmdx node_modules when --allow-parser-install", "the diagram source, rewritten by the browser viewer AFTER this command returns"),
        evidence=(
            "cli.py:3259 -> mmdx_open.py:649 mmdx_open_payload -> :429 _open_selected_mmdx",
            "mmdx_open.py:440-458 subprocess.run([sys.executable, str(script), path, '--open', ...], timeout=45)",
            "delegate present: workspace/skill-repos/build000r-skills/mmdx/scripts/mmd.py",
        ),
        gap=(
            "BLOCKING: the delegate is an external skill-repo script that launches a browser "
            "viewer whose write-back handler can rewrite the diagram source after this "
            "command has already exited, and --allow-parser-install runs `npm install`. "
            "The post-return write window cannot be bounded without executing the viewer. "
            "Recorded pessimistically as conditional_mutation."
        ),
    ),
    _b(
        SURFACE_MANAGE, "onboard", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if dry_run:` workflows.py:406 — early return before sync/bootstrap/up; the scaffold itself receives dry_run=dry_run (workflows.py:83)",
        nested_call_policy="reenters_inprocess: scaffold_client_overlay, sync_runtime, sync_context, run_tasks, start_services",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("client overlay tree", "everything manage.sync writes", "logs/runtime/runtime.log"),
        evidence=("cli.py:2463 -> workflows.py:362 run_onboard",),
    ),
    _b(
        SURFACE_MANAGE, "operator-booking book", TRUE_DRY_RUN,
        state_root_source="external",
        dry_run_predicate="`if dry_run:` operator_booking.py:487 — returns before the POST",
        nested_call_policy="delegates_external: SPAPS booking HTTP API",
        lease_span="single_write (remote)",
        lock_owner="external service",
        writes=("remote x402 booking hold + magic-link email; NO local write",),
        evidence=("cli.py:3784 -> operator_booking.py:57 operator_booking_payload",),
    ),
    _b(
        SURFACE_MANAGE, "overlay activate", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if dry_run:` lifecycle.py:941 (link) / lifecycle.py:993 (unlink)",
        nested_call_policy="reenters_inprocess: apply_skill_lifecycle_plan",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("skill symlinks under <cwd>/.claude/skills and <cwd>/.codex/skills",),
        evidence=("cli.py:6644 -> lifecycle.py:156 activate_overlay_scoped_skills",),
    ),
    _b(
        SURFACE_MANAGE, "overlay off", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if bool(getattr(args, \"dry_run\", False)):` cli.py:6650; unlink guarded by `and not bool(getattr(args, \"dry_run\", False))` cli.py:6516",
        nested_call_policy="reenters_inprocess: unlink_overlay_scoped_skills",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("$SKILLBOX_OVERLAY_STATE or ~/.skillbox-state/overlays", "unlinked skill symlinks"),
        evidence=("cli.py:6448 _apply_persistent_overlay_action -> policy_eval.py:680 set_overlay", "policy_eval.py:133-134 overlay state root", "policy_eval.py:693 atomic_write_text"),
    ),
    _b(
        SURFACE_MANAGE, "overlay on", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if bool(getattr(args, \"dry_run\", False)):` cli.py:6650",
        nested_call_policy="reenters_inprocess: policy_eval.set_overlay",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("$SKILLBOX_OVERLAY_STATE or ~/.skillbox-state/overlays",),
        evidence=("cli.py:6448 -> policy_eval.py:680 set_overlay", "policy_eval.py:693 atomic_write_text"),
    ),
    _b(
        SURFACE_MANAGE, "overlay toggle", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if bool(getattr(args, \"dry_run\", False)):` cli.py:6650; unlink branch also guarded at cli.py:6516",
        nested_call_policy="reenters_inprocess: policy_eval.set_overlay + lifecycle unlink/activate",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("$SKILLBOX_OVERLAY_STATE or ~/.skillbox-state/overlays", "skill symlinks"),
        evidence=("cli.py:6448 -> policy_eval.py:680 set_overlay",),
    ),
    _b(
        SURFACE_MANAGE, "private-init", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE — no --dry-run flag exists; ensure_directory is called with dry_run=False",
        nested_call_policy="delegates_external: git init",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("<target>/ + git init", "<target>/clients/", "migrated overlay trees", "<root>/.env (SKILLBOX_CLIENTS_HOST_ROOT)"),
        evidence=("cli.py:2610 -> _shared/client.py:918 init_private_repo", "_shared/client.py:925 `ensure_directory(target_dir, dry_run=False)`"),
    ),
    _b(
        SURFACE_MANAGE, "rch-stage", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if run_requested or prepare_requested:` cli.py:3675 — plan-only is the default; --prepare/--run opt in",
        nested_call_policy="delegates_external: rsync/ssh wrappers via subprocess",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("plan['stage']['root']", "local_projects_root", "alias symlink", "ssh/rsync wrappers", "manifest_path", "source mirror"),
        evidence=("cli.py:3640 -> rch_adapter.py:375-379 / :387-388 / :218-219 mkdir+write_text", "rch_adapter.py:432 subprocess.run"),
    ),
    _b(
        SURFACE_MANAGE, "registry-docs", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if write:` registry_docs.py:60 — opt-in --write, default off",
        nested_call_policy="leaf",
        lease_span="single_write",
        lock_owner=UNOWNED,
        writes=("docs/API_REFERENCE.md",),
        evidence=("cli.py:3517 -> registry_docs.py:51 registry_docs_payload", "registry_docs.py:62 path.write_text"),
    ),
    _b(
        SURFACE_MANAGE, "restart", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if not args.dry_run:` cli.py:7119; every callee receives dry_run=args.dry_run (cli.py:7115/7118/7126/7130)",
        nested_call_policy="reenters_inprocess: stop_services, sync_runtime, run_tasks, start_services",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("everything manage.sync writes", "service pid files", "logs/runtime/runtime.log"),
        evidence=("cli.py:7108 _handle_restart",),
    ),
    _b(
        SURFACE_MANAGE, "session-end", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE — no --dry-run on any session-* verb",
        nested_call_policy="leaf",
        lease_span="whole_command (meta.json + events.jsonl + handoff.md must land together)",
        lock_owner=UNOWNED + " — session state bypasses locked_json_update entirely",
        writes=("<client_log_root>/sessions/<id>/meta.json", ".../events.jsonl", ".../handoff.md", "logs/runtime/runtime.log"),
        evidence=("cli.py:3026 -> _shared/session.py:427 end_client_session", "_shared/session.py:452 write_json_file", "_shared/session.py:201 write_text_file"),
    ),
    _b(
        SURFACE_MANAGE, "session-event", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE",
        nested_call_policy="leaf",
        lease_span="whole_command (append to events.jsonl + rewrite meta.json)",
        lock_owner=UNOWNED,
        writes=("sessions/<id>/events.jsonl", "sessions/<id>/meta.json", "logs/runtime/runtime.log"),
        evidence=("cli.py:2996 -> _shared/session.py:399 append_client_session_event -> :420 _persist_session_event",),
    ),
    _b(
        SURFACE_MANAGE, "session-resume", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("sessions/<id>/meta.json", "sessions/<id>/events.jsonl", "logs/runtime/runtime.log"),
        evidence=("cli.py:3052 -> _shared/session.py:477 resume_client_session", "_shared/session.py:498 write_json_file"),
    ),
    _b(
        SURFACE_MANAGE, "session-start", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE",
        nested_call_policy="leaf",
        lease_span="whole_command (session dir + 3 files created together)",
        lock_owner=UNOWNED,
        writes=("sessions/<id>/", "meta.json", "handoff.md", "events.jsonl", "logs/runtime/runtime.log"),
        evidence=("cli.py:2967 -> _shared/session.py:335 start_client_session", "_shared/session.py:372 `ensure_directory(paths[\"session_dir\"], dry_run=False)`"),
    ),
    _b(
        SURFACE_MANAGE, "skill activate", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if dry_run:` lifecycle.py:941 (link) / lifecycle.py:993 (unlink)",
        nested_call_policy="reenters_inprocess: apply_skill_lifecycle_plan",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("skill symlinks under <cwd>/.claude/skills, <cwd>/.codex/skills, category/global roots",),
        evidence=("cli.py:6371 -> lifecycle.py:1057 apply_skill_lifecycle_plan",),
    ),
    _b(
        SURFACE_MANAGE, "skill add", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if dry_run:` lifecycle.py:941 / :993",
        nested_call_policy="reenters_inprocess: apply_skill_lifecycle_plan",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("skill symlinks",),
        evidence=("cli.py:6371 -> lifecycle.py:1057",),
    ),
    _b(
        SURFACE_MANAGE, "skill default", CONDITIONAL_MUTATION,
        state_root_source="cli.skill_default_review",
        dry_run_predicate=(
            "THREE MODES, and one of them is NOT write-free. --repo: `if dry_run:` cli.py:4960 "
            "(true dry run). --global: `if dry_run:` cli.py:5605 plus a --yes gate at cli.py:5577. "
            "--repos/--category: `if dry_run:` cli.py:5206 STILL WRITES a review marker via "
            "_record_fleet_skill_default_review -> cli.py:5068-5069 "
            "`marker_path.parent.mkdir(...)` + `atomic_write_text(...)`. Apply then refuses "
            "without a matching marker (cli.py:5265-5272). Deliberate, but --dry-run is not inert."
        ),
        nested_call_policy="reenters_inprocess: policy_eval.update_repo_override_policy per matched repo",
        lease_span="whole_command (fleet mode spans N repos; the marker is the only cross-repo consent record)",
        lock_owner=LOCK_OVERRIDES + "; the review marker itself is " + UNOWNED,
        writes=("<repo>/.skillbox/skill-overrides.yaml per matched repo", "operator skill-scope.yaml (--global)", "${STATE_ROOT}/skill-default-previews/<sha>.json (--dry-run, fleet mode)"),
        evidence=("cli.py:4908 _handle_repo_skill_default", "cli.py:5572 _handle_global_skill_default", "cli.py:5238 _handle_fleet_skill_default", "cli.py:4984 _skill_default_review_dir"),
    ),
    _b(
        SURFACE_MANAGE, "skill heal", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if dry_run:` cli.py:4837",
        nested_call_policy="reenters_inprocess: policy_eval.update_repo_override_policy",
        lease_span="whole_command",
        lock_owner=LOCK_OVERRIDES,
        writes=("<repo>/.skillbox/skill-overrides.yaml", "skill symlinks"),
        evidence=("cli.py:6232 _handle_skill_heal -> cli.py:4827 _apply_skill_heal_pin",),
    ),
    _b(
        SURFACE_MANAGE, "skill move", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if dry_run:` lifecycle.py:941 / :993; confirmation gate cli.py:6360-6366 requires --yes",
        nested_call_policy="reenters_inprocess: apply_skill_lifecycle_plan",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("skill symlinks (unlink + relink)",),
        evidence=("cli.py:6371 -> lifecycle.py:1057",),
    ),
    _b(
        SURFACE_MANAGE, "skill off", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if dry_run:` cli.py:4802",
        nested_call_policy="reenters_inprocess: policy_eval.update_repo_override_policy + lifecycle unlink",
        lease_span="whole_command",
        lock_owner=LOCK_OVERRIDES,
        writes=("<repo>/.skillbox/skill-overrides.yaml", "skill symlinks"),
        evidence=("cli.py:6157 _handle_skill_toggle -> cli.py:4792 _apply_skill_pin -> policy_eval.py:1273",),
    ),
    _b(
        SURFACE_MANAGE, "skill on", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if dry_run:` cli.py:4802",
        nested_call_policy="reenters_inprocess: policy_eval.update_repo_override_policy + lifecycle link",
        lease_span="whole_command",
        lock_owner=LOCK_OVERRIDES,
        writes=("<repo>/.skillbox/skill-overrides.yaml", "skill symlinks"),
        evidence=("cli.py:6157 -> cli.py:4792 -> policy_eval.py:1273 update_repo_override_policy", "policy_eval.py:1294 _atomic_write_override_text"),
    ),
    _b(
        SURFACE_MANAGE, "skill prune", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if dry_run:` lifecycle.py:993; --yes required (cli.py:6360-6366); shutil.rmtree only with --allow-directories",
        nested_call_policy="reenters_inprocess: apply_skill_lifecycle_plan",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("skill symlinks (unlinked)", "skill directories when --allow-directories"),
        evidence=("cli.py:6371 -> lifecycle.py:1057",),
    ),
    _b(
        SURFACE_MANAGE, "skill remove", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if dry_run:` lifecycle.py:993; --yes required (cli.py:6360-6366)",
        nested_call_policy="reenters_inprocess: apply_skill_lifecycle_plan",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("skill symlinks (unlinked)",),
        evidence=("cli.py:6371 -> lifecycle.py:1057",),
    ),
    _b(
        SURFACE_MANAGE, "skill sync", TRUE_DRY_RUN,
        state_root_source="home",
        dry_run_predicate="`if dry_run:` lifecycle.py:941 / :993; --yes required when --prune is set (cli.py:6360-6366)",
        nested_call_policy="reenters_inprocess: apply_skill_lifecycle_plan",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("skill symlinks (link + unlink)",),
        evidence=("cli.py:6371 -> lifecycle.py:1057",),
    ),
    _b(
        SURFACE_MANAGE, "snap create", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`if getattr(args, \"write\", False):` cli.py:4690 — opt-in --write, default off. Bare `manage.py snap` also lands here (cli.py:4672)",
        nested_call_policy="leaf",
        lease_span="single_write",
        lock_owner=UNOWNED,
        writes=(".skillbox-state/snapshots/agent_ops/<snapshot_id>.json",),
        evidence=("cli.py:4665 _handle_snap", "agent_snapshots.py:225 save_snapshot", "agent_snapshots.py:231 path.write_text", "agent_snapshots.py:21 SNAPSHOT_DIR"),
    ),
    _b(
        SURFACE_MANAGE, "state-backup create", UNCONDITIONAL_MUTATION,
        state_root_source="state_backup.backup_root",
        dry_run_predicate="NONE — no --dry-run on state-backup create",
        nested_call_policy="leaf",
        lease_span=(
            "INTENDED: the whole tar of the state root must be crash-consistent. ACTUAL: none — "
            "the archive is streamed while any other boundary may be writing the same tree"
        ),
        lock_owner=UNOWNED + " — nothing quiesces the state root while it is being archived",
        writes=("$SKILLBOX_BACKUP_ROOT/<stamp>.tar.gz", "$SKILLBOX_BACKUP_ROOT/<stamp>.manifest.json"),
        evidence=("cli.py:7176 _handle_state_backup -> state_backup.py:231 create_state_backup", "state_backup.py:241 `destination_root.mkdir(parents=True, exist_ok=True)`"),
    ),
    _b(
        SURFACE_MANAGE, "state-backup drill", UNCONDITIONAL_MUTATION,
        state_root_source="state_backup.state_root",
        dry_run_predicate="NONE — evidence is written on every drill; extraction itself goes to a TemporaryDirectory",
        nested_call_policy="leaf",
        lease_span="single_write",
        lock_owner=UNOWNED + " — bypasses locked_json_update; uses _write_json_0600 (state_backup.py:494)",
        writes=("<state_root>/state-backup/last-drill.json (DRILL_EVIDENCE_REL, state_backup.py:26)",),
        evidence=("state_backup.py:744 drill_state_backup", "state_backup.py:806 `evidence_path = _write_drill_evidence(source_root, payload)`"),
    ),
    _b(
        SURFACE_MANAGE, "state-backup list", CONDITIONAL_MUTATION,
        state_root_source="state_backup.backup_root",
        dry_run_predicate="NONE. A nominally read-only verb that CREATES the backup root if absent",
        nested_call_policy="leaf",
        lease_span="single_write",
        lock_owner=UNOWNED,
        writes=("$SKILLBOX_BACKUP_ROOT/ (directory created)",),
        evidence=("state_backup.py:685 list_state_backups", "state_backup.py:690 `destination_root.mkdir(parents=True, exist_ok=True)`"),
    ),
    _b(
        SURFACE_MANAGE, "state-backup restore", CONDITIONAL_MUTATION,
        state_root_source="state_backup.state_root",
        dry_run_predicate="`if not i_understand_data_loss:` state_backup.py:820 raises STATE_BACKUP_RESTORE_CONFIRMATION_REQUIRED. There is no preview mode — the flag is consent, not a dry run",
        nested_call_policy="reenters_inprocess: create_state_backup (safety backup) before the swap",
        lease_span=(
            "INTENDED: exclusive over the ENTIRE state root for the duration of the swap. "
            "ACTUAL: none — rename+rmtree run with every other writer live"
        ),
        lock_owner=UNOWNED + " — the single highest-risk unowned write in the tree",
        writes=("<state_root> renamed aside then replaced wholesale", "previous state root shutil.rmtree'd"),
        evidence=("state_backup.py:812 restore_state_backup", "state_backup.py:853 `shutil.rmtree(previous)`", "state_backup.py:859 `shutil.rmtree(staging)`"),
    ),
    _b(
        SURFACE_MANAGE, "stewardship-report", CONDITIONAL_MUTATION,
        state_root_source="workflows.stewardship",
        dry_run_predicate="`if write or output_dir_arg:` workflows.py:3050 — opt-in",
        nested_call_policy="leaf",
        lease_span="single_write",
        lock_owner=UNOWNED,
        writes=("<overlay_dir>/reports/stewardship/<cid>-stewardship-<slug>.json", "... .md"),
        evidence=("cli.py:2948 -> workflows.py:3032 run_stewardship_report", "workflows.py:3017-3019 report paths", "workflows.py:3026-3028 write_json_file / write_text_file"),
    ),
    _b(
        SURFACE_MANAGE, "swimmers-launch", TRUE_DRY_RUN,
        state_root_source="external",
        dry_run_predicate="`if dry_run:` swimmers_launch.py:301 — returns before the POST",
        nested_call_policy="delegates_external: Swimmers HTTP endpoint",
        lease_span="single_write (remote)",
        lock_owner="external service",
        writes=("remote Swimmers agent sessions; NO local write",),
        evidence=("cli.py:3225 -> swimmers_launch.py:265 launch_swimmers_batch", "swimmers_launch.py:112 urllib.request.urlopen"),
    ),
    _b(
        SURFACE_MANAGE, "sync", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="every callee receives dry_run=dry_run (runtime_ops.py:2351-2362); `if not dry_run:` runtime_ops.py:2365 for the event, `if not dry_run:` context_rendering.py:466 for CLAUDE.md",
        nested_call_policy="reenters_inprocess: sync_runtime + sync_context",
        lease_span=(
            "INTENDED: whole_command over the managed tree. ACTUAL: none — every write is "
            "independently atomic, nothing is atomic across the set"
        ),
        lock_owner=UNOWNED,
        writes=("managed dirs", "repo clones", "artifacts", "env files", "port contracts", "log dirs", "skill link sets", "dcg config", "ingress artifacts", "CLAUDE.md/AGENTS.md", "logs/runtime/runtime.log"),
        evidence=("cli.py:3981 _handle_sync -> runtime_ops.py:2349 sync_runtime + context_rendering.py:633 sync_context",),
    ),
    _b(
        SURFACE_MANAGE, "up", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="local-profile path `if dry_run:` workflows.py:4237; legacy path `if not args.dry_run:` cli.py:7000 with sync_runtime(model, dry_run=args.dry_run) at cli.py:6997",
        nested_call_policy="reenters_inprocess: sync_runtime, run_tasks, start_services",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("everything manage.sync writes", "service pid files", "containers started", "logs/runtime/runtime.log"),
        evidence=("cli.py:7064 _handle_up -> cli.py:6818 _handle_local_profile_up -> workflows.py:4199 run_up", "cli.py:6991 _legacy_up_payload"),
    ),
    _b(
        SURFACE_MANAGE, "worker-artifacts", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate=(
            "NO FLAG — the write predicate is OBSERVED RUN STATE, not an argument: "
            "`if state not in {\"launching\", \"running\"}: return payload` worker.py:711-712. "
            "A nominal read reconciles a dead run to terminal and persists it"
        ),
        nested_call_policy="leaf",
        lease_span="whole_command (run.json + events.jsonl reconciled together)",
        lock_owner=UNOWNED,
        writes=("<state_root>/worker/runs/<id>/run.json", ".../events.jsonl", "logs/runtime/runtime.log"),
        evidence=("cli.py:3202 -> _shared/worker.py:987 worker_artifacts_payload -> :709 _reconcile_worker_payload", "worker.py:739 _persist_worker_payload"),
    ),
    _b(
        SURFACE_MANAGE, "worker-promote-learning", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE — idempotent early-return only if already promoted (worker.py:1121)",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("<state_root>/worker/runs/<id>/run.json", ".../events.jsonl", "logs/runtime/runtime.log"),
        evidence=("cli.py:3210 -> _shared/worker.py:1107 promote_worker_learning", "worker.py:1131 write_json_file"),
    ),
    _b(
        SURFACE_MANAGE, "worker-status", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="Same observed-state predicate as worker-artifacts: `if state not in {\"launching\", \"running\"}: return payload` worker.py:711-712",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("<state_root>/worker/runs/<id>/run.json", ".../events.jsonl", "logs/runtime/runtime.log"),
        evidence=("cli.py:3194 -> _shared/worker.py:972 worker_status_payload -> :709 _reconcile_worker_payload",),
    ),
    _b(
        SURFACE_MANAGE, "worker-submit", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE",
        nested_call_policy="delegates_external: spawns the `hermes` runtime via subprocess.Popen; that process keeps writing into the run dir AFTER this command returns",
        lease_span="whole_command for the submit; UNBOUNDED for the spawned runtime's writes",
        lock_owner=UNOWNED,
        writes=("<state_root>/worker/runs/<id>/run.json", ".../events.jsonl", ".../stdout|stderr logs", "logs/runtime/runtime.log"),
        evidence=("cli.py:3111 -> _shared/worker.py:844 create_worker_run", "worker.py:951 write_json_file", "worker.py:793-796 append handles + Popen", "worker.py:547 atomic_write_text"),
    ),
)

# --------------------------------------------------------------------------
# pulse CLI — 4 surfaces
# --------------------------------------------------------------------------

_PULSE_BOUNDARIES: tuple[Boundary, ...] = (
    _b(
        SURFACE_PULSE, "run", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE — pulse has no --dry-run anywhere; the only two dry_run mentions in the file are hardcoded False (pulse.py:363, pulse.py:1126)",
        nested_call_policy="reenters_inprocess: sync_runtime(model, dry_run=False) under --auto-sync; restarts services and kills rogue port listeners",
        lease_span=(
            "INTENDED: one reconcile cycle. ACTUAL: only the final single_write of "
            "pulse.state.json is locked; the restart/sync/prune work in the same cycle is unlocked"
        ),
        lock_owner=LOCK_PULSE,
        writes=("logs/runtime/pulse.state.json", "logs/runtime/pulse.pid", "logs/runtime/port-guard.telemetry.json", "rotated + pruned log files", "restarted services"),
        evidence=(
            "pulse.py:1516 run_daemon",
            "pulse.py:1536 write_pid -> pulse.py:274-275 write_text + os.replace",
            "pulse.py:1126 `actions = sync_runtime(model, dry_run=False)`",
            "pulse.py:1498 `locked_json_update(state_path, lambda _current: snapshot)`",
            "pulse.py:1493 comment claims serialization against focus writers — FALSE, different sidecar",
            "pulse.py:1410 prune_expired_log_files unlinks",
        ),
    ),
    _b(
        SURFACE_PULSE, "start", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE",
        nested_call_policy="reenters_subprocess: forks `pulse.py run` with start_new_session=True; all pulse.run writes follow",
        lease_span="UNBOUNDED — the daemon it forks outlives this command",
        lock_owner=UNOWNED + " for the spawn itself; the forked daemon then takes " + LOCK_PULSE,
        writes=("logs/runtime/pulse.log (created + appended)", "everything pulse.run writes"),
        evidence=("pulse.py:1598 start_daemon", "pulse.py:1653-1661 log open + subprocess.Popen(..., start_new_session=True)"),
    ),
    _b(
        SURFACE_PULSE, "status", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate=(
            "NO FLAG — the write predicate is observed staleness. existing_pid() unlinks a "
            "stale pid file: `pid_path.unlink()` pulse.py:303. pulse.py:1928-1930 calls the "
            "bare invocation 'read-only'; that comment is wrong"
        ),
        nested_call_policy="leaf",
        lease_span="single_write",
        lock_owner=UNOWNED,
        writes=("logs/runtime/pulse.pid (unlinked when stale)",),
        evidence=("pulse.py:1683 print_status", "pulse.py:1688 existing_pid(root_dir)", "pulse.py:286 existing_pid", "pulse.py:303 `pid_path.unlink()`"),
    ),
    _b(
        SURFACE_PULSE, "stop", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE",
        nested_call_policy="leaf",
        lease_span="single_signal",
        lock_owner=UNOWNED,
        writes=("SIGTERM to the daemon pid", "logs/runtime/pulse.pid (unlinked when stale, via existing_pid)"),
        evidence=("pulse.py:1936-1943 inline stop branch in main", "pulse.py:1941 `os.kill(pid, signal.SIGTERM)`", "pulse.py:303 stale pid unlink"),
    ),
)

# --------------------------------------------------------------------------
# box CLI — 15 surfaces
# --------------------------------------------------------------------------

_BOX_UP_LIKE_EVIDENCE = (
    "scripts/box.py:2064 save_inventory -> :2089 locked_inventory_update",
    "scripts/box.py:2086 _append_inventory_journal (raw os.open/os.write/os.fsync append)",
)

_BOX_BOUNDARIES: tuple[Boundary, ...] = (
    _read(SURFACE_BOX, "capabilities", "scripts/box.py:5361 dispatch -> box_capabilities_payload + emit_json"),
    _read(SURFACE_BOX, "list", "scripts/box.py:5044 cmd_list — pure read of the inventory"),
    _read(SURFACE_BOX, "profiles", "scripts/box.py:5078 cmd_profiles — pure read"),
    _read(SURFACE_BOX, "robot-docs", "scripts/box.py:5364 dispatch -> box_robot_docs_guide"),
    _read(SURFACE_BOX, "robot-triage", "scripts/box.py:5371 dispatch -> box_robot_triage_payload"),
    _read(
        SURFACE_BOX, "posture-proof",
        "scripts/box.py:4942 cmd_posture_proof — network/doctl probes only; the 'proof artifact' "
        "is emitted to stdout at box.py:4972, never written to disk. Note box.py:97-113 "
        "BOX_COMMAND_NAMES omits posture-proof, so the --json rewrite does not fire for it",
    ),
    _b(
        SURFACE_BOX, "down", TRUE_DRY_RUN,
        state_root_source="opslib.inventory",
        dry_run_predicate=(
            "`if dry_run:` box.py:4399 returns _emit_box_down_dry_run before the first "
            "save_inventory (box.py:4427). CAVEAT: box.py:5350 "
            "`down_confirmed = bool(args.dry_run or args.yes or args.confirm == args.box_id)` "
            "means --dry-run alone satisfies the confirmation gate at box.py:5356, so the dry "
            "run still loads operator secrets into the process env (box.py:4396-4398). "
            "Process-env only; no disk write"
        ),
        nested_call_policy="remote_exec: doctl destroy + ssh drain against the box",
        lease_span="cross_process_infra (destroy + volume detach + inventory update)",
        lock_owner=LOCK_INVENTORY,
        writes=("workspace/boxes.json", "workspace/boxes-journal.jsonl", "the DigitalOcean droplet + volume (destroyed)"),
        evidence=_BOX_UP_LIKE_EVIDENCE + ("scripts/box.py:4366 cmd_down", "box.py:4375 `if not dry_run and not confirmed:`"),
    ),
    _b(
        SURFACE_BOX, "import", UNCONDITIONAL_MUTATION,
        state_root_source="opslib.inventory",
        dry_run_predicate="NONE — alias of `register`; --no-probe only skips the SSH probe (box.py:4658), it does NOT skip the inventory write",
        nested_call_policy="remote_exec: optional SSH probe",
        lease_span="single_write",
        lock_owner=LOCK_INVENTORY,
        writes=("workspace/boxes.json", "workspace/boxes-journal.jsonl"),
        evidence=_BOX_UP_LIKE_EVIDENCE + ("scripts/box.py:5283 import parser -> cmd_register box.py:4626", "box.py:4661 save_inventory"),
    ),
    _b(
        SURFACE_BOX, "inventory-rebuild", UNCONDITIONAL_MUTATION,
        state_root_source="opslib.inventory",
        dry_run_predicate=(
            "NONE. --from-journal is a REQUIREMENT (`if not from_journal:` box.py:4676 errors), "
            "not a preview. There is no dry-run, no confirmation, and the rebuild is written "
            "with journal=False, tolerate_corrupt=True (box.py:4688-4689) — i.e. it overwrites "
            "boxes.json from the journal without journalling the overwrite"
        ),
        nested_call_policy="leaf",
        lease_span="single_write",
        lock_owner=LOCK_INVENTORY,
        writes=("workspace/boxes.json (replaced wholesale)",),
        evidence=_BOX_UP_LIKE_EVIDENCE + ("scripts/box.py:4674 cmd_inventory_rebuild", "box.py:4685-4690 save_inventory(..., journal=False, tolerate_corrupt=True)"),
    ),
    _b(
        SURFACE_BOX, "register", UNCONDITIONAL_MUTATION,
        state_root_source="opslib.inventory",
        dry_run_predicate="NONE — --no-probe (box.py:5280) skips only the SSH probe; --force (box.py:5279) permits clobbering an active entry (box.py:4648)",
        nested_call_policy="remote_exec: optional SSH probe",
        lease_span="single_write",
        lock_owner=LOCK_INVENTORY,
        writes=("workspace/boxes.json", "workspace/boxes-journal.jsonl"),
        evidence=_BOX_UP_LIKE_EVIDENCE + ("scripts/box.py:4626 cmd_register", "box.py:4661 save_inventory(filtered_boxes)"),
    ),
    _b(
        SURFACE_BOX, "ssh", UNCONDITIONAL_MUTATION,
        state_root_source="remote",
        dry_run_predicate="NONE — the process is REPLACED by ssh; nothing after it runs",
        nested_call_policy="interactive_unbounded: whatever the operator types on the box",
        lease_span="interactive_session — unbounded, and no lease can outlive execvp",
        lock_owner=UNOWNED + " (control leaves the process entirely)",
        writes=("arbitrary remote state",),
        evidence=("scripts/box.py:4996 cmd_ssh", "box.py:5066 `os.execvp(\"ssh\", [\"ssh\", *DEFAULT_SSH_OPTS, \"--\", f\"{box.ssh_user}@{target}\"])`"),
    ),
    _b(
        SURFACE_BOX, "status", CONDITIONAL_MUTATION,
        state_root_source="opslib.inventory",
        dry_run_predicate=(
            "`if not enabled: return False` box.py:2122-2123 inside "
            "persist_inventory_if_ssh_targets_changed, plus a snapshot-drift check (box.py:2124) "
            "and an existence check (box.py:2126). CLI default is OFF (--write-cache store_true, "
            "box.py:5256). WARNING: the function signature defaults the other way — "
            "`def cmd_status(..., write_cache: bool = True, ...)` box.py:4710 — so any non-CLI "
            "caller writes by default"
        ),
        nested_call_policy="remote_exec: parallel SSH/doctl health probes (box.py:4805-4806 ThreadPoolExecutor)",
        lease_span="single_write",
        lock_owner=LOCK_INVENTORY,
        writes=("workspace/boxes.json (ssh-target cache refresh)", "workspace/boxes-journal.jsonl"),
        evidence=_BOX_UP_LIKE_EVIDENCE + ("scripts/box.py:4710 cmd_status", "box.py:2117 persist_inventory_if_ssh_targets_changed", "box.py:2128 save_inventory(boxes)"),
    ),
    _b(
        SURFACE_BOX, "unregister", UNCONDITIONAL_MUTATION,
        state_root_source="opslib.inventory",
        dry_run_predicate="NONE",
        nested_call_policy="leaf",
        lease_span="single_write",
        lock_owner=LOCK_INVENTORY,
        writes=("workspace/boxes.json", "workspace/boxes-journal.jsonl"),
        evidence=_BOX_UP_LIKE_EVIDENCE + ("scripts/box.py:4499 cmd_unregister", "box.py:4519 update_box(box, state=\"destroyed\")", "box.py:4520 save_inventory(boxes)"),
    ),
    _b(
        SURFACE_BOX, "up", TRUE_DRY_RUN,
        state_root_source="opslib.inventory",
        dry_run_predicate=(
            "`if dry_run:` box.py:3783 returns _box_up_dry_run_payload before _run_new_box_up "
            "(box.py:3789); resume path `if dry_run:` box.py:3765; storage guard "
            "`if not dry_run and not _ensure_box_up_storage(...)` box.py:3769; manifest relax "
            "`if dry_run:` box.py:3627. No save_inventory, subprocess, or file write on the "
            "dry-run path"
        ),
        nested_call_policy="remote_exec: doctl create + tailscale enroll + ssh deploy; the remote heredoc writes .env/.mcp.json ON THE BOX (box.py:1616-1655)",
        lease_span="cross_process_infra — inventory is saved at NINE separate points during provisioning (box.py:2818, 2993, 3046, 3082, 3092, 3114, 3158, 3217, 3407), each an independent lock acquisition",
        lock_owner=LOCK_INVENTORY,
        writes=("workspace/boxes.json (9 checkpoints)", "workspace/boxes-journal.jsonl", "DigitalOcean droplet + volume", "remote box runtime contract"),
        evidence=_BOX_UP_LIKE_EVIDENCE + ("scripts/box.py:3715 cmd_up", "box.py:3708 os.environ['DIGITALOCEAN_ACCESS_TOKEN'] set only after the dry-run return"),
    ),
    _b(
        SURFACE_BOX, "upgrade", TRUE_DRY_RUN,
        state_root_source="opslib.inventory",
        dry_run_predicate="`if dry_run:` box.py:3912 records four skip steps and returns EXIT_OK at box.py:3918, before _resolve_existing_box_target / scp_file / ssh_script / save_inventory (box.py:4029)",
        nested_call_policy="remote_exec: scp + ssh redeploy",
        lease_span="cross_process_infra",
        lock_owner=LOCK_INVENTORY,
        writes=("workspace/boxes.json", "workspace/boxes-journal.jsonl", "remote box runtime"),
        evidence=_BOX_UP_LIKE_EVIDENCE + ("scripts/box.py:3868 cmd_upgrade", "box.py:4029 save_inventory"),
    ),
)

# --------------------------------------------------------------------------
# operator MCP server — 10 tools
# --------------------------------------------------------------------------

_OPMCP_BOUNDARIES: tuple[Boundary, ...] = (
    _read(SURFACE_OPERATOR_MCP, "operator_boxes", "scripts/operator_mcp_server.py:358 tool decl (read_only=True at :365) -> handle_operator_boxes :993"),
    _read(SURFACE_OPERATOR_MCP, "operator_box_status", "scripts/operator_mcp_server.py:373 (read_only=True at :381) -> handle_operator_box_status :998"),
    _read(SURFACE_OPERATOR_MCP, "operator_doctor", "scripts/operator_mcp_server.py:599 (read_only=True at :606) -> handle_operator_doctor :1428"),
    _read(SURFACE_OPERATOR_MCP, "operator_profiles", "scripts/operator_mcp_server.py:343 (read_only=True at :350) -> handle_operator_profiles :988"),
    _read(SURFACE_OPERATOR_MCP, "operator_render", "scripts/operator_mcp_server.py:614 (read_only=True at :620) -> handle_operator_render :1433"),
    _b(
        SURFACE_OPERATOR_MCP, "operator_box_exec", CONDITIONAL_MUTATION,
        state_root_source="remote",
        dry_run_predicate=(
            "Three-way. Read-only classifier bypass: "
            "`if classification[\"verdict\"] == \"read-only\" and not dry_run_param:` :1243. "
            "Dry run stamps a marker: `if dry_run_param: _stamp_dryrun_marker(...)` :1255-1256. "
            "Real run requires that marker: `if not _has_dryrun_marker(\"operator_box_exec\", marker_key)` :1287. "
            "Marker key binds to the EXACT command via sha256 (:1511 _box_exec_marker_key)"
        ),
        nested_call_policy="remote_exec: arbitrary ssh command; blast radius is the command string",
        lease_span="cross_process_infra (remote); the local marker is consumed one-shot at :1331",
        lock_owner=MARKER_NOT_A_LOCK,
        writes=("arbitrary remote state", "${STATE_ROOT}/dryrun-markers/.skillbox-dryrun-operator_box_exec-<key>"),
        evidence=("scripts/operator_mcp_server.py:490 tool decl", "operator_mcp_server.py:1147 handle_operator_box_exec", "operator_mcp_server.py:1723 _stamp_dryrun_marker", "operator_mcp_server.py:1773 _clear_dryrun_marker"),
    ),
    _b(
        SURFACE_OPERATOR_MCP, "operator_compose_down", CONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="dry-run branch simulates via `docker compose ps` (:1387); the real run requires a marker: `if not _has_dryrun_marker(\"operator_compose_down\", \"local\")` :1411-1412",
        nested_call_policy="delegates_external: docker compose down",
        lease_span="cross_process_infra",
        lock_owner=MARKER_NOT_A_LOCK + "; the compose project itself is the real serializer",
        writes=("local docker containers/networks torn down", "${STATE_ROOT}/dryrun-markers/..."),
        evidence=("scripts/operator_mcp_server.py:574 tool decl (destructive=True at :582)", "operator_mcp_server.py:1382 handle_operator_compose_down"),
    ),
    _b(
        SURFACE_OPERATOR_MCP, "operator_compose_up", UNCONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate=(
            "NONE — this is the ONE mutating operator tool with no dry_run parameter and no "
            "marker requirement. Its only inputs are `build` and `surfaces` (:552); "
            "handle_operator_compose_up (:1335) goes straight to run_compose. Asymmetric with "
            "operator_compose_down, which does require a marker"
        ),
        nested_call_policy="delegates_external: docker compose up",
        lease_span="cross_process_infra",
        lock_owner=UNOWNED + " (compose project lock is the only serializer)",
        writes=("local docker images built", "containers started"),
        evidence=("scripts/operator_mcp_server.py:543 tool decl", "operator_mcp_server.py:1335 handle_operator_compose_up"),
    ),
    _b(
        SURFACE_OPERATOR_MCP, "operator_provision", CONDITIONAL_MUTATION,
        state_root_source="opslib.inventory",
        dry_run_predicate="`if dry_run_param: args.append(\"--dry-run\")` / `elif not _has_dryrun_marker(\"operator_provision\", box_id_param): return _dry_run_required_error(` :1078-1087",
        nested_call_policy="reenters_subprocess: box.up (which itself checkpoints the inventory nine times)",
        lease_span="cross_process_infra",
        lock_owner=MARKER_NOT_A_LOCK + "; the nested box.up write takes " + LOCK_INVENTORY,
        writes=("everything box.up writes", "${STATE_ROOT}/dryrun-markers/..."),
        delegates_to=("box.up",),
        evidence=("scripts/operator_mcp_server.py:398 tool decl", "operator_mcp_server.py:1010 handle_operator_provision", "operator_mcp_server.py:1098 _clear_dryrun_marker"),
    ),
    _b(
        SURFACE_OPERATOR_MCP, "operator_teardown", CONDITIONAL_MUTATION,
        state_root_source="opslib.inventory",
        dry_run_predicate="`if dry_run_param: args.append(\"--dry-run\")` / `elif not _has_dryrun_marker(\"operator_teardown\", box_id_param): return _dry_run_required_error(` :1124-1127",
        nested_call_policy="reenters_subprocess: box.down",
        lease_span="cross_process_infra",
        lock_owner=MARKER_NOT_A_LOCK + "; the nested box.down write takes " + LOCK_INVENTORY,
        writes=("everything box.down writes", "${STATE_ROOT}/dryrun-markers/..."),
        delegates_to=("box.down",),
        evidence=("scripts/operator_mcp_server.py:464 tool decl (destructive=True at :472)", "operator_mcp_server.py:1102 handle_operator_teardown", "operator_mcp_server.py:1142 _clear_dryrun_marker"),
    ),
)

# --------------------------------------------------------------------------
# Make targets — 51 surfaces
# --------------------------------------------------------------------------

_MAKE_DELEGATION_ONLY = (
    "Make adds no gating of its own: the target's classification is exactly its delegate's. "
    "An agent with shell access reaches the delegate directly, so Make is a convenience "
    "wrapper, never a control point"
)


def _make_delegate(target: str, delegate: str, classification: str, *, extra: str = "") -> Boundary:
    note = _MAKE_DELEGATION_ONLY + ((" " + extra) if extra else "")
    return _b(
        SURFACE_MAKE, target, classification,
        state_root_source="runtime_model.root_dir" if delegate.startswith(("manage.", "pulse.")) else "opslib.inventory",
        dry_run_predicate=note,
        nested_call_policy=f"reenters_subprocess: {delegate}",
        lease_span="inherited from " + delegate,
        lock_owner="inherited from " + delegate,
        writes=(f"whatever {delegate} writes",),
        evidence=(f"Makefile target `{target}` shells out to {delegate}",),
        delegates_to=(delegate,),
    )


def _make_read_delegate(target: str, delegate: str) -> Boundary:
    return _b(
        SURFACE_MAKE, target, READ,
        nested_call_policy=f"reenters_subprocess: {delegate}",
        evidence=(f"Makefile target `{target}` shells out to {delegate} (read)",),
        delegates_to=(delegate,),
    )


_MAKE_BOUNDARIES: tuple[Boundary, ...] = (
    _read(SURFACE_MAKE, "help", "Makefile:44 help — printf block only"),
    _read(SURFACE_MAKE, "render", "Makefile render -> `python3 scripts/04-reconcile.py render` (outer reconcile, read-only)"),
    _read(SURFACE_MAKE, "doctor", "Makefile doctor -> `python3 scripts/04-reconcile.py doctor` (outer reconcile, read-only)"),
    _read(SURFACE_MAKE, "logs", "Makefile logs -> `$(COMPOSEF) logs -f --tail=200`"),
    _make_read_delegate("runtime-render", "manage.render"),
    _make_read_delegate("runtime-status", "manage.status"),
    _make_read_delegate("runtime-skills", "manage.skills"),
    _make_read_delegate("runtime-skill-audit", "manage.skill-audit"),
    _make_read_delegate("runtime-logs", "manage.logs"),
    _make_read_delegate("dev-sanity", "manage.doctor"),
    _make_read_delegate("swimmers-runtime-status", "manage.status"),
    _make_read_delegate("box-status", "box.status"),
    _make_read_delegate("box-list", "box.list"),
    _make_read_delegate("box-profiles", "box.profiles"),
    _make_read_delegate("pulse-status", "pulse.status"),
    _read(SURFACE_MAKE, "swimmers-status", "Makefile swimmers-status -> `./scripts/05-swimmers.sh status`; depends on bootstrap-env, which mutates"),
    _read(SURFACE_MAKE, "swimmers-logs", "Makefile swimmers-logs -> `./scripts/05-swimmers.sh logs`; depends on bootstrap-env, which mutates"),
    _b(
        SURFACE_MAKE, "e2e-smoke", TRUE_DRY_RUN,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="the script runs `manage.py sync --dry-run` (e2e-smoke.sh:516) and then PROVES inertness: step_state_mutation (e2e-smoke.sh:617) fails the run if any watched mtime changed",
        nested_call_policy="reenters_subprocess: manage.render, manage.sync (dry-run only); starts and stops stub processes",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("$(mktemp -d) scratch only (e2e-smoke.sh:77-80)",),
        evidence=("Makefile e2e-smoke -> ./scripts/e2e-smoke.sh", "e2e-smoke.sh:504 manage.py render", "e2e-smoke.sh:516 manage.py sync --dry-run", "e2e-smoke.sh:726 run_step 'state-mutation' 'required'"),
        delegates_to=("manage.render", "manage.sync"),
    ),
    _b(
        SURFACE_MAKE, "bootstrap-env", CONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="`@test -f $(_STATE_ROOT)/operator/.env || test -f ./.env || cp .env.example $(_STATE_ROOT)/operator/.env` — seeds only when both files are absent; the mkdir is unconditional",
        nested_call_policy="reenters_make: install-hooks (prerequisite)",
        lease_span="single_write",
        lock_owner=UNOWNED,
        writes=("$(_STATE_ROOT)/operator/", "$(_STATE_ROOT)/operator/.env"),
        evidence=("Makefile:95 `bootstrap-env: install-hooks`", "Makefile bootstrap-env recipe `@mkdir -p $(_STATE_ROOT)/operator`"),
        delegates_to=(),
    ),
    _b(
        SURFACE_MAKE, "install-hooks", CONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="`@if git rev-parse --git-dir >/dev/null 2>&1; then` — mutates git config only inside a git worktree",
        nested_call_policy="delegates_external: git config",
        lease_span="single_write",
        lock_owner=UNOWNED,
        writes=("git config core.hooksPath = .githooks",),
        evidence=("Makefile:99 install-hooks",),
    ),
    _b(
        SURFACE_MAKE, "python-cov-xml", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE — `@python3 -m coverage erase` runs first, unconditionally",
        nested_call_policy="delegates_external: coverage",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=(".coverage", "coverage.xml"),
        evidence=("Makefile:173 python-cov-xml",),
    ),
    _b(
        SURFACE_MAKE, "wrappers-install", UNCONDITIONAL_MUTATION,
        state_root_source="home",
        dry_run_predicate="NONE — `@mkdir -p \"$(WRAPPER_BIN_DIR)\"` then `ln -sf` into $HOME/.local/bin",
        nested_call_policy="reenters_make: dev-shims-install",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("$(WRAPPER_BIN_DIR)/sbp", "$(WRAPPER_BIN_DIR)/sbo", "dev shims"),
        evidence=("Makefile:179 wrappers-install",),
    ),
    _b(
        SURFACE_MAKE, "dev-shims-install", UNCONDITIONAL_MUTATION,
        state_root_source="home",
        dry_run_predicate="NONE",
        nested_call_policy="leaf",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("$(DEV_SHIM_BIN_DIR)/* symlinks",),
        evidence=("Makefile:189 dev-shims-install",),
    ),
    _b(
        SURFACE_MAKE, "self-test", UNCONDITIONAL_MUTATION,
        state_root_source="selftest.state_root",
        dry_run_predicate="NONE — the gate always provisions the toolchain and publishes a receipt",
        nested_call_policy="delegates_external: scripts/self-test.sh; lanes run in a throwaway clone, never the worktree",
        lease_span="whole_command, and this is the ONLY surface in the tree that takes a real cross-process lease",
        lock_owner=LOCK_SELFTEST,
        writes=("${STATE_ROOT}/self-test/toolchain/", "${STATE_ROOT}/self-test/receipts/", "$(mktemp -d) work dir"),
        evidence=(
            "Makefile:110 self-test -> ./scripts/self-test.sh --trigger make",
            "scripts/self-test.sh:178-180 STATE_ROOT / TOOLCHAIN_DIR / RECEIPT_DIR",
            "scripts/self-test.sh:181 mkdir -p",
            "scripts/self-test.sh:186-190 LOCK_FILE + `flock -w 3600 9`",
        ),
    ),
    _b(
        SURFACE_MAKE, "self-test-refresh", UNCONDITIONAL_MUTATION,
        state_root_source="selftest.state_root",
        dry_run_predicate="NONE — --refresh forces toolchain re-provisioning even on a cache hit",
        nested_call_policy="delegates_external: scripts/self-test.sh --refresh",
        lease_span="whole_command",
        lock_owner=LOCK_SELFTEST,
        writes=("${STATE_ROOT}/self-test/toolchain/ (re-provisioned)", "${STATE_ROOT}/self-test/receipts/"),
        evidence=("Makefile:116 self-test-refresh", "scripts/self-test.sh:143 --refresh"),
    ),
    _b(
        SURFACE_MAKE, "self-test-worktree", UNCONDITIONAL_MUTATION,
        state_root_source="selftest.state_root",
        dry_run_predicate="NONE — --worktree overlays uncommitted files onto the throwaway checkout; the operator worktree stays untouched (self-test.sh:232)",
        nested_call_policy="delegates_external: scripts/self-test.sh --worktree",
        lease_span="whole_command",
        lock_owner=LOCK_SELFTEST,
        writes=("${STATE_ROOT}/self-test/toolchain/", "${STATE_ROOT}/self-test/receipts/"),
        evidence=("Makefile:113 self-test-worktree", "scripts/self-test.sh:134-135 SOURCE_MODE=worktree-overlay"),
    ),
    _b(
        SURFACE_MAKE, "build", UNCONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NONE",
        nested_call_policy="delegates_external: docker compose build; reenters_make: bootstrap-env",
        lease_span="cross_process_infra",
        lock_owner=UNOWNED,
        writes=("docker images",),
        evidence=("Makefile:206 `build: bootstrap-env` -> `@$(COMPOSEF) build`",),
    ),
    _b(
        SURFACE_MAKE, "up", UNCONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NONE",
        nested_call_policy="delegates_external: docker compose up -d workspace; reenters_make: bootstrap-env",
        lease_span="cross_process_infra",
        lock_owner=UNOWNED,
        writes=("workspace container started",),
        evidence=("Makefile:209 `up: bootstrap-env`",),
    ),
    _b(
        SURFACE_MAKE, "up-surfaces", UNCONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NONE",
        nested_call_policy="delegates_external: docker compose --profile surfaces up -d api web",
        lease_span="cross_process_infra",
        lock_owner=UNOWNED,
        writes=("api + web containers started",),
        evidence=("Makefile:212 `up-surfaces: bootstrap-env`",),
    ),
    _b(
        SURFACE_MAKE, "down", UNCONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NONE — and unlike `up`/`build` it has no bootstrap-env prerequisite",
        nested_call_policy="delegates_external: docker compose down",
        lease_span="cross_process_infra",
        lock_owner=UNOWNED,
        writes=("all compose containers + networks torn down",),
        evidence=("Makefile:215 down",),
    ),
    _b(
        SURFACE_MAKE, "shell", UNCONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NONE",
        nested_call_policy="interactive_unbounded: `$(COMPOSEF) exec workspace zsh` — whatever the operator types in the container",
        lease_span="interactive_session — unbounded",
        lock_owner=UNOWNED,
        writes=("arbitrary container + mounted-volume state",),
        evidence=("Makefile:218 `shell: bootstrap-env`",),
    ),
    _b(
        SURFACE_MAKE, "swimmers-install", UNCONDITIONAL_MUTATION,
        state_root_source="runtime_model.root_dir",
        dry_run_predicate="NONE",
        nested_call_policy="delegates_external: scripts/05-swimmers.sh install",
        lease_span="whole_command",
        lock_owner=UNOWNED,
        writes=("swimmers log dir + install dir (05-swimmers.sh:88 mkdir -p)", "swimmers binary (05-swimmers.sh:296 cp)"),
        evidence=("Makefile:224 `swimmers-install: bootstrap-env`", "scripts/05-swimmers.sh:420 install branch"),
    ),
    _b(
        SURFACE_MAKE, "swimmers-start", UNCONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NONE",
        nested_call_policy="delegates_external: scripts/05-swimmers.sh start -> docker compose",
        lease_span="cross_process_infra",
        lock_owner=UNOWNED,
        writes=("swimmers containers started",),
        evidence=("Makefile:227 swimmers-start", "scripts/05-swimmers.sh:423 start branch", "05-swimmers.sh:34 docker compose"),
    ),
    _b(
        SURFACE_MAKE, "swimmers-stop", UNCONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NONE",
        nested_call_policy="delegates_external: scripts/05-swimmers.sh stop -> docker compose",
        lease_span="cross_process_infra",
        lock_owner=UNOWNED,
        writes=("swimmers containers stopped",),
        evidence=("Makefile:230 swimmers-stop", "scripts/05-swimmers.sh:426 stop branch"),
    ),
    _b(
        SURFACE_MAKE, "swimmers-restart", UNCONDITIONAL_MUTATION,
        state_root_source="external",
        dry_run_predicate="NONE",
        nested_call_policy="delegates_external: scripts/05-swimmers.sh restart -> docker compose",
        lease_span="cross_process_infra",
        lock_owner=UNOWNED,
        writes=("swimmers containers restarted",),
        evidence=("Makefile:233 swimmers-restart", "scripts/05-swimmers.sh:429 restart branch"),
    ),
    _b(
        SURFACE_MAKE, "box-ssh", UNCONDITIONAL_MUTATION,
        state_root_source="remote",
        dry_run_predicate=_MAKE_DELEGATION_ONLY,
        nested_call_policy="reenters_subprocess: box.ssh (interactive_unbounded)",
        lease_span="interactive_session — unbounded",
        lock_owner="inherited from box.ssh",
        writes=("arbitrary remote state",),
        evidence=("Makefile:259 box-ssh -> `python3 scripts/box.py ssh $(BOX_ARGS)`",),
        delegates_to=("box.ssh",),
    ),
    _make_delegate("acceptance", "manage.acceptance", UNCONDITIONAL_MUTATION),
    _make_delegate("runtime-sync", "manage.sync", TRUE_DRY_RUN),
    _make_delegate("runtime-bootstrap", "manage.bootstrap", TRUE_DRY_RUN),
    _make_delegate("runtime-up", "manage.up", TRUE_DRY_RUN),
    _make_delegate("runtime-down", "manage.down", TRUE_DRY_RUN),
    _make_delegate("runtime-restart", "manage.restart", TRUE_DRY_RUN),
    _make_delegate("onboard", "manage.onboard", TRUE_DRY_RUN),
    _make_delegate("first-box", "manage.first-box", UNCONDITIONAL_MUTATION),
    _make_delegate("context", "manage.context", TRUE_DRY_RUN),
    _make_delegate("pulse-start", "pulse.start", UNCONDITIONAL_MUTATION),
    _make_delegate("pulse-stop", "pulse.stop", UNCONDITIONAL_MUTATION),
    _make_delegate(
        "box-up", "box.up", TRUE_DRY_RUN,
        extra="No Makefile-level dry-run gate: the operator MCP marker enforcement does not apply here.",
    ),
    _make_delegate(
        "box-down", "box.down", TRUE_DRY_RUN,
        extra="Destroys infrastructure. No Makefile-level gate; `make box-down BOX=id` bypasses the operator MCP marker entirely.",
    ),
    _make_delegate("box-register", "box.register", UNCONDITIONAL_MUTATION),
    _make_delegate("box-unregister", "box.unregister", UNCONDITIONAL_MUTATION),
)

#: Every classified public boundary, sorted for byte-stable rendering.
MANIFEST: tuple[Boundary, ...] = tuple(
    sorted(
        _MANAGE_BOUNDARIES + _PULSE_BOUNDARIES + _BOX_BOUNDARIES + _OPMCP_BOUNDARIES + _MAKE_BOUNDARIES,
        key=lambda b: (b.surface, b.key),
    )
)

_BY_ID: Mapping[str, Boundary] = {b.boundary_id: b for b in MANIFEST}


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def boundary(boundary_id: str) -> Boundary:
    """Return the boundary with ``boundary_id`` or raise ``KeyError``."""
    return _BY_ID[boundary_id]


def boundary_ids() -> tuple[str, ...]:
    return tuple(b.boundary_id for b in MANIFEST)


def mutations() -> tuple[Boundary, ...]:
    return tuple(b for b in MANIFEST if b.is_mutation)


def owned_gaps() -> tuple[Boundary, ...]:
    """Boundaries that could not be classified without executing a mutation."""
    return tuple(b for b in MANIFEST if b.is_gap)


def inventory_complete() -> bool:
    """``False`` while any OWNED GAP is open. That is the honest terminal state."""
    return not owned_gaps()


def classification_counts() -> dict[str, int]:
    counts = {name: 0 for name in CLASSIFICATIONS}
    for entry in MANIFEST:
        counts[entry.classification] += 1
    return counts


def classified_keys() -> dict[str, tuple[str, ...]]:
    """``{surface: sorted keys}`` as declared in :data:`MANIFEST`."""
    grouped: dict[str, list[str]] = {name: [] for name in SURFACE_KINDS}
    for entry in MANIFEST:
        grouped[entry.surface].append(entry.key)
    return {name: tuple(sorted(keys)) for name, keys in grouped.items()}


# --------------------------------------------------------------------------
# Live enumeration — the ratchet's other half
# --------------------------------------------------------------------------


def _repo_root(root_dir: Path | str | None = None) -> Path:
    if root_dir is not None:
        return Path(root_dir)
    # runtime_manager/ -> .env-manager/ -> repo root
    return Path(__file__).resolve().parents[2]


def enumerate_manage_surfaces(root_dir: Path | str | None = None) -> tuple[str, ...]:
    """Leaf ``manage`` surfaces, read from the live argparse tree.

    A command with nested subparsers or an ``action`` positional decomposes into
    ``"<command> <subaction>"`` keys and the bare parent is NOT emitted, because
    the subactions classify differently (``state-backup verify`` is a read;
    ``state-backup restore`` replaces the entire state root). ``manage.py snap``
    with no subaction is an alias of ``snap create`` (cli.py:4672).

    The import is deliberately lazy and local: this module must stay
    standard-library-only at import time.
    """
    import argparse
    import sys

    env_manager = _repo_root(root_dir) / ".env-manager"
    if str(env_manager) not in sys.path:
        sys.path.insert(0, str(env_manager))
    from runtime_manager import cli as _cli  # noqa: PLC0415

    parser = _cli._build_parser()  # noqa: SLF001
    command_action = next(
        action
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction) and action.dest == "command"
    )
    keys: list[str] = []
    for name in sorted(command_action.choices):
        subparser = command_action.choices[name]
        nested: list[str] | None = None
        actions: list[str] | None = None
        for action in subparser._actions:  # noqa: SLF001
            if isinstance(action, argparse._SubParsersAction):
                nested = sorted(action.choices)
            elif not action.option_strings and action.dest == "action" and action.choices:
                actions = sorted(str(choice) for choice in action.choices)
        leaves = nested or actions
        if leaves:
            keys.extend(f"{name} {leaf}" for leaf in leaves)
        else:
            keys.append(name)
    return tuple(sorted(keys))


def _add_parser_names(source: Path) -> tuple[str, ...]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_parser":
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            names.add(node.args[0].value)
    return tuple(sorted(names))


def enumerate_pulse_surfaces(root_dir: Path | str | None = None) -> tuple[str, ...]:
    """``pulse`` subcommands, read from the source AST (never imported)."""
    return _add_parser_names(_repo_root(root_dir) / ".env-manager" / "pulse.py")


def enumerate_box_surfaces(root_dir: Path | str | None = None) -> tuple[str, ...]:
    """``box`` subcommands, read from the source AST (never imported)."""
    return _add_parser_names(_repo_root(root_dir) / "scripts" / "box.py")


def enumerate_operator_mcp_surfaces(root_dir: Path | str | None = None) -> tuple[str, ...]:
    """Operator MCP tool names, read from the ``TOOLS`` literal via AST."""
    source = _repo_root(root_dir) / "scripts" / "operator_mcp_server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        target_names: list[str] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        if "TOOLS" not in target_names or not isinstance(value, ast.List):
            continue
        for element in value.elts:
            if not isinstance(element, ast.Dict):
                continue
            for key, val in zip(element.keys, element.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "name"
                    and isinstance(val, ast.Constant)
                    and isinstance(val.value, str)
                ):
                    names.append(val.value)
    return tuple(sorted(set(names)))


_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?!=)")
_MAKE_DELEGATE_RE = re.compile(r"(manage\.py|box\.py|pulse\.py)\s+([a-z][a-z0-9-]*)")
_MAKE_ENTRYPOINT_SURFACE = {
    "manage.py": SURFACE_MANAGE,
    "box.py": SURFACE_BOX,
    "pulse.py": SURFACE_PULSE,
}


def _make_recipes(root_dir: Path | str | None = None) -> dict[str, list[str]]:
    """``{target: recipe lines}`` parsed straight out of the Makefile text."""
    text = (_repo_root(root_dir) / "Makefile").read_text(encoding="utf-8")
    recipes: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("\t"):
            if current is not None:
                recipes[current].append(line)
            continue
        match = _MAKE_TARGET_RE.match(line)
        if match and match.group(1) != ".PHONY":
            current = match.group(1)
            recipes.setdefault(current, [])
        elif line.strip() and not line.startswith((" ", "\t")):
            current = None
    return recipes


def enumerate_make_surfaces(root_dir: Path | str | None = None) -> tuple[str, ...]:
    """Makefile targets, parsed from the Makefile text."""
    return tuple(sorted(_make_recipes(root_dir)))


def enumerate_live_surfaces(root_dir: Path | str | None = None) -> dict[str, tuple[str, ...]]:
    """``{surface: sorted keys}`` as they exist in the tree right now."""
    return {
        SURFACE_MANAGE: enumerate_manage_surfaces(root_dir),
        SURFACE_PULSE: enumerate_pulse_surfaces(root_dir),
        SURFACE_BOX: enumerate_box_surfaces(root_dir),
        SURFACE_OPERATOR_MCP: enumerate_operator_mcp_surfaces(root_dir),
        SURFACE_MAKE: enumerate_make_surfaces(root_dir),
    }


def coverage_report(root_dir: Path | str | None = None) -> dict[str, Any]:
    """Diff live surfaces against :data:`MANIFEST`.

    ``unclassified`` means a surface exists in the tree with no manifest row —
    the exact failure this bead exists to prevent. ``stale`` means the manifest
    names a surface that no longer exists.
    """
    live = enumerate_live_surfaces(root_dir)
    declared = classified_keys()
    unclassified: list[str] = []
    stale: list[str] = []
    for surface in SURFACE_KINDS:
        live_keys = set(live[surface])
        declared_keys = set(declared[surface])
        unclassified.extend(f"{surface}:{key}" for key in sorted(live_keys - declared_keys))
        stale.extend(f"{surface}:{key}" for key in sorted(declared_keys - live_keys))
    return {
        "ok": not unclassified and not stale,
        "counts": {surface: len(live[surface]) for surface in SURFACE_KINDS},
        "total_live": sum(len(live[surface]) for surface in SURFACE_KINDS),
        "total_classified": len(MANIFEST),
        "unclassified": tuple(unclassified),
        "stale": tuple(stale),
    }


def detect_wrapper_bypass(root_dir: Path | str | None = None) -> tuple[str, ...]:
    """Find Make targets that reach a mutating entrypoint without declaring it.

    Make is not a control point — a target that shells out to ``box.py down``
    inherits that boundary's blast radius whether or not anybody wrote it down.
    This re-derives every ``manage.py`` / ``box.py`` / ``pulse.py`` invocation
    from the Makefile recipes and reports any that the target's
    ``delegates_to`` does not declare, plus any declared delegate that is not a
    known boundary ID.
    """
    findings: list[str] = []
    declared = {b.key: b for b in MANIFEST if b.surface == SURFACE_MAKE}
    for target, lines in sorted(_make_recipes(root_dir).items()):
        derived: set[str] = set()
        for line in lines:
            for entrypoint, command in _MAKE_DELEGATE_RE.findall(line):
                derived.add(f"{_MAKE_ENTRYPOINT_SURFACE[entrypoint]}.{command}")
        entry = declared.get(target)
        if entry is None:
            if derived:
                findings.append(
                    f"make:{target} invokes {sorted(derived)} but has no manifest row"
                )
            continue
        for delegate in sorted(derived - set(entry.delegates_to)):
            findings.append(
                f"make:{target} invokes {delegate} but does not declare it in delegates_to"
            )
        for delegate in sorted(set(entry.delegates_to) - derived):
            if delegate not in _BY_ID:
                findings.append(
                    f"make:{target} declares unknown boundary id {delegate}"
                )
    for entry in MANIFEST:
        for delegate in entry.delegates_to:
            if delegate not in _BY_ID:
                findings.append(
                    f"{entry.boundary_id} declares unknown boundary id {delegate}"
                )
    return tuple(sorted(set(findings)))


# --------------------------------------------------------------------------
# Deterministic rendering
# --------------------------------------------------------------------------

#: Serialization key order is part of the contract; two renders must be equal
#: byte-for-byte, so nothing here may include a timestamp, a hostname, a path
#: outside the repo, or an unsorted mapping.
_PAYLOAD_KEY_ORDER = (
    "boundary_id",
    "surface",
    "key",
    "classification",
    "entry_points",
    "state_root_source",
    "dry_run_predicate",
    "nested_call_policy",
    "lease_span",
    "lock_owner",
    "writes",
    "evidence",
    "delegates_to",
    "gap",
)


def boundary_payload(entry: Boundary) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in _PAYLOAD_KEY_ORDER:
        value = getattr(entry, name)
        payload[name] = list(value) if isinstance(value, tuple) else value
    return payload


def manifest_payload() -> dict[str, Any]:
    """The full manifest as plain data, in a fixed key order."""
    return {
        "schema": MANIFEST_SCHEMA_VERSION,
        "classifications": list(CLASSIFICATIONS),
        "surfaces": list(SURFACE_KINDS),
        "surface_entrypoints": {name: SURFACE_ENTRYPOINTS[name] for name in SURFACE_KINDS},
        "state_root_sources": {name: STATE_ROOT_SOURCES[name] for name in sorted(STATE_ROOT_SOURCES)},
        "counts": classification_counts(),
        "total": len(MANIFEST),
        "inventory_complete": inventory_complete(),
        "owned_gaps": [entry.boundary_id for entry in owned_gaps()],
        "boundaries": [boundary_payload(entry) for entry in MANIFEST],
    }


def render_manifest() -> str:
    """Byte-stable JSON rendering. ``render_manifest() == render_manifest()``."""
    return json.dumps(manifest_payload(), indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def render_manifest_text() -> str:
    """Byte-stable human-readable rendering."""
    lines = [
        f"# state mutation boundary manifest ({MANIFEST_SCHEMA_VERSION})",
        f"# boundaries: {len(MANIFEST)}",
    ]
    counts = classification_counts()
    for name in CLASSIFICATIONS:
        lines.append(f"#   {name}: {counts[name]}")
    lines.append(f"# inventory_complete: {str(inventory_complete()).lower()}")
    for entry in owned_gaps():
        lines.append(f"# OWNED GAP {entry.boundary_id}: {entry.gap}")
    lines.append("")
    for entry in MANIFEST:
        lines.append(f"{entry.boundary_id}\t{entry.classification}\t{entry.lock_owner}")
    return "\n".join(lines) + "\n"


__all__ = [
    "MANIFEST",
    "MANIFEST_SCHEMA_VERSION",
    "CLASSIFICATIONS",
    "MUTATING_CLASSIFICATIONS",
    "SURFACE_KINDS",
    "SURFACE_ENTRYPOINTS",
    "STATE_ROOT_SOURCES",
    "READ",
    "TRUE_DRY_RUN",
    "UNCONDITIONAL_MUTATION",
    "CONDITIONAL_MUTATION",
    "Boundary",
    "boundary",
    "boundary_ids",
    "boundary_payload",
    "classification_counts",
    "classified_keys",
    "coverage_report",
    "detect_wrapper_bypass",
    "enumerate_box_surfaces",
    "enumerate_live_surfaces",
    "enumerate_make_surfaces",
    "enumerate_manage_surfaces",
    "enumerate_operator_mcp_surfaces",
    "enumerate_pulse_surfaces",
    "inventory_complete",
    "manifest_payload",
    "mutations",
    "owned_gaps",
    "render_manifest",
    "render_manifest_text",
]
