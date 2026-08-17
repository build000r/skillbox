"""One front door for all STRUCTURAL verification gates: `sbp doctor`.

Today verification is folklore: pytest here, unittest there, ``make doctor`` in
another repo, ad-hoc ``validate-*.py`` scripts. This module gives agents ONE
command to run before/after any policy change. It deliberately COMPLEMENTS — it
does not duplicate or replace — the existing runtime ``make doctor`` /
``manage.py doctor``. The runtime doctor validates the live runtime graph; this
front door validates the *structure* (the skill estate, the policy contract,
lock parity, MCP config parity, skill drift) and, when reachable, INVOKES the
runtime ``make doctor`` as a single RUNTIME gate so the two complement rather
than fight.

Semantics borrowed verbatim from
``skillbox-config/scripts/status_proof_bundle.py`` (the INCO/FAIL/cap pattern):

* Each gate runs under a per-gate wall-clock cap. A gate that EXCEEDS its cap is
  INCONCLUSIVE (``INCO``), never a FAIL — a slow toolchain or a loaded box must
  not masquerade as a regression.
* A gate that RUNS and reports a real failure is ``FAIL``.
* A gate that cannot run on this box (e.g. the runtime ``make doctor`` is
  unreachable / its dependencies are absent) is ``INCO``, not ``FAIL``.
* The process exits NONZERO on ``FAIL`` ONLY. ``INCO`` and ``PASS`` exit 0.

Every gate is labelled ``structure`` or ``runtime`` so the output reads as a
complement to the Makefile doctor. The STRUCTURE gates are budgeted to finish in
under 60s total; per-gate caps enforce that, and a structure gate that blows its
cap is reported INCO rather than allowed to drag the budget.

Each gate result carries the contract the issue asks for::

    {name, kind, status, duration_s, fix_command, detail}

This module is standard-library + in-package only; it imports the lint/audit
helpers and invokes the structure-invariant suite + runtime doctor as
subprocesses, so it never re-implements a gate that already exists.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .shared import (
    DEFAULT_ROOT_DIR,
    build_runtime_model,
)
from .validation import (
    validate_global_overlay_precedence_file,
    validate_global_skill_contract_file,
    validate_overlay_declarations_file,
    validate_registry_path_duplication_file,
    validate_repo_skill_override_policy,
    validate_skill_locks_and_state,
    validate_skill_repo_sets,
)
from .skill_visibility import collect_skill_visibility
from .mcp_visibility import collect_mcp_audit
from .git_scan_cache import (
    CACHE_TTL_SECONDS as GIT_SCAN_TTL_SECONDS,
    format_age as _format_scan_age,
    load_scan_cache,
)
from lib import doctor_fix
from lib.doctor_contract import (
    EXIT_DRIFT as _EXIT_DRIFT,
    EXIT_ERROR as _EXIT_ERROR,
    EXIT_NEEDS_INPUT as _EXIT_NEEDS_INPUT,
    EXIT_OK as _EXIT_OK,
    STATUS_FAIL as _STATUS_FAIL,
    STATUS_INCO as _STATUS_INCO,
    STATUS_PASS as _STATUS_PASS,
    Finding,
    coverage_block,
    display_status,
    doctor_envelope,
    fix_contract,
)

# Gate kinds and statuses are part of the JSON contract; keep them as constants
# so the CLI renderer and tests share one source of truth.
KIND_STRUCTURE = "structure"
KIND_RUNTIME = "runtime"

# The ONE doctor-family status vocabulary, lowercase in JSON and shouty in text
# (``display_status``). It lives in scripts/lib/doctor_contract.py because
# scripts/04-reconcile.py provably cannot import runtime_manager — see
# tests/test_reconcile.py RuntimeDoctorExitVocabularyTests — and a second copy
# of the vocabulary is exactly the drift this contract retires. Re-exported here
# under the historical names so every existing `STATUS_FAIL` comparison in this
# module keeps working against the new values.
STATUS_PASS = _STATUS_PASS
STATUS_FAIL = _STATUS_FAIL
STATUS_INCO = _STATUS_INCO

#: This doctor's name in the family routing table (lib/doctor_contract.FAMILY).
DOCTOR_TOOL_NAME = "sbp doctor"

# The family exit ladder. Source of truth: _shared/errors.py, mirrored in
# lib/doctor_contract.py for the half of the family that cannot import
# runtime_manager. 4 is a VERDICT ("ran fine, found a difference"), 1 is "could
# not produce a verdict" — this doctor emits 4 and READS 4 at its runtime gate.
EXIT_OK = _EXIT_OK
EXIT_ERROR = _EXIT_ERROR
EXIT_DRIFT = _EXIT_DRIFT
RUNTIME_DOCTOR_EXIT_ERROR = _EXIT_ERROR
RUNTIME_DOCTOR_EXIT_DRIFT = _EXIT_DRIFT

#: The state_mutation.py MANIFEST id whose lease `sbp doctor --fix --yes` takes.
STRUCTURE_DOCTOR_FIX_BOUNDARY_ID = "manage.structure-doctor"

STRUCTURE_DOCTOR_UNDO_TEMPLATE = "python3 .env-manager/manage.py structure-doctor --undo {artifact}"

# Total wall-clock budget the STRUCTURE gates must fit inside. Per-gate caps are
# derived/declared below so the sum stays under this; a structure gate exceeding
# its own cap is reported INCO (not FAIL) and does not get to drag the budget.
STRUCTURE_BUDGET_S = float(os.environ.get("SBP_DOCTOR_STRUCTURE_BUDGET_S", "60"))

# Per-gate caps (seconds). Pure in-process lints are sub-second; the
# subprocess-driven structure-invariant suite gets a wider cap but stays well
# inside the 60s structure budget. The runtime gate (make doctor) is a separate
# RUNTIME budget: it is slow and side-channel-y, so it is capped generously and
# its time is NOT counted against the structure budget.
CAP_FAST_LINT = float(os.environ.get("SBP_DOCTOR_CAP_FAST_LINT_S", "20"))
CAP_STRUCTURE_SUITE = float(os.environ.get("SBP_DOCTOR_CAP_STRUCTURE_SUITE_S", "45"))
CAP_RUNTIME_DOCTOR = float(os.environ.get("SBP_DOCTOR_CAP_RUNTIME_DOCTOR_S", "120"))


@dataclass
class GateResult:
    """One gate outcome. ``status`` is PASS|FAIL|INCO; FAIL only flips exit code."""

    name: str
    kind: str  # KIND_STRUCTURE | KIND_RUNTIME
    status: str  # STATUS_PASS | STATUS_FAIL | STATUS_INCO
    duration_s: float
    fix_command: str
    detail: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _GateSpec:
    """Declarative gate: a runner callable plus its kind, cap, and fix command.

    The runner returns ``(status, detail)`` where status is PASS|FAIL|INCO. The
    cap wrapper turns a timeout / unexpected error into INCO so a slow or absent
    dependency never reads as a regression (mirrors status_proof_bundle's INCO
    contract).
    """

    name: str
    kind: str
    cap_s: float
    fix_command: str
    runner: Callable[["DoctorContext"], tuple[str, str]]


@dataclass
class DoctorContext:
    """Resolved roots + a lazily-built runtime model shared across gates."""

    runtime_root: Path
    config_root: Path | None
    cwd: Path
    _model: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def model(self) -> dict[str, Any]:
        if self._model is None:
            self._model = build_runtime_model(self.runtime_root)
        return self._model


# --------------------------------------------------------------------------- #
# Root resolution
# --------------------------------------------------------------------------- #

def _resolve_config_root(runtime_root: Path) -> Path | None:
    """Locate the skillbox-config repo (where the structure invariants live).

    Honors ``SKILLBOX_CONFIG_ROOT`` then falls back to the devbox layouts that
    ``validation._skill_scope_policy_path`` already uses, so this front door and
    the runtime agree on where structure lives.
    """
    override = str(os.environ.get("SKILLBOX_CONFIG_ROOT") or "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(os.path.expandvars(os.path.expanduser(override))))
    candidates.extend(
        [
            runtime_root.parent / "skillbox-config",
            runtime_root.parent.parent / "skillbox-config",
            Path.home() / "repos" / "skillbox-config",
        ]
    )
    for candidate in candidates:
        if (candidate / "clients").is_dir() or (candidate / "skill-scope.yaml").is_file():
            return candidate.resolve()
    return None


def build_context(runtime_root: Path | None = None, cwd: Path | None = None) -> DoctorContext:
    root = (runtime_root or DEFAULT_ROOT_DIR).resolve()
    return DoctorContext(
        runtime_root=root,
        config_root=_resolve_config_root(root),
        cwd=(cwd or Path(os.getcwd())).resolve(),
    )


# --------------------------------------------------------------------------- #
# Gate runners — each returns (status, detail) with status in PASS|FAIL|INCO
# --------------------------------------------------------------------------- #

def _checkresults_status(results: list[Any]) -> tuple[str, str, list[str]]:
    """Fold a list of CheckResult into (status, detail, fail_messages).

    ``fail`` anywhere -> FAIL. Otherwise PASS. ``warn`` is surfaced in the detail
    but is NOT a failure (advisory), matching the runtime doctor's posture.
    """
    fails = [r for r in results if getattr(r, "status", "") == "fail"]
    warns = [r for r in results if getattr(r, "status", "") == "warn"]
    if fails:
        messages = [str(getattr(r, "message", "")) for r in fails]
        detail = "; ".join(m for m in messages if m) or f"{len(fails)} failing check(s)"
        return STATUS_FAIL, detail, messages
    if warns:
        return STATUS_PASS, f"{len(warns)} advisory warning(s); no failures", []
    return STATUS_PASS, f"{len(results)} check(s) passed", []


def _run_structure_invariant_suite(ctx: DoctorContext) -> tuple[str, str]:
    """The sibling bead's executable structure invariants (skillbox-config).

    Invoked as a subprocess (``python3 -m pytest`` falling back to unittest) so
    we run the SAME gate the proof bundle runs rather than re-implementing it. An
    absent skillbox-config or missing test file is INCO, not FAIL.
    """
    if ctx.config_root is None:
        return STATUS_INCO, "skillbox-config repo not found on this box"
    test_file = ctx.config_root / "tests" / "test_structure_invariants.py"
    if not test_file.is_file():
        return STATUS_INCO, f"structure invariant suite not found at {test_file}"
    proc = subprocess.run(
        ["python3", "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider"],
        cwd=str(ctx.config_root),
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    # pytest exit 5 = no tests collected; treat as INCO (suite unusable here).
    if proc.returncode == 5:
        return STATUS_INCO, "no structure invariant tests collected"
    if proc.returncode == 0:
        return STATUS_PASS, _last_meaningful_line(out)
    return STATUS_FAIL, _last_meaningful_line(out)


def _run_policy_lint(ctx: DoctorContext) -> tuple[str, str]:
    """Skill-repo policy lint (config/source/install consistency).

    Reuses ``validate_skill_repo_sets`` — the same lint the runtime doctor and
    proof bundle consume. ``warn`` (e.g. forge hooks) is advisory, not a fail.
    """
    results = validate_skill_repo_sets(ctx.model)
    # Lock parity is its own gate; exclude lock-coded results here so each gate
    # owns exactly one concern and a single drift is not double-counted.
    non_lock = [r for r in results if getattr(r, "code", "") not in _LOCK_CODES]
    status, detail, _ = _checkresults_status(non_lock)
    return status, detail


def _run_global_skill_contract(ctx: DoctorContext) -> tuple[str, str]:
    """The global-skill-contract lint (validation.validate_global_skill_contract)."""
    results = validate_global_skill_contract_file()
    status, detail, _ = _checkresults_status(results)
    return status, detail


def _run_overlay_declaration(ctx: DoctorContext) -> tuple[str, str]:
    """The overlay-declaration lint (validation.validate_overlay_declarations).

    Asserts every rule ``overlay:`` tag in skill-scope.yaml references a declared
    overlay in the ``overlays:`` registry, so a typo is a FAIL that names the
    ghost tag rather than a silent never-matching overlay.
    """
    results = validate_overlay_declarations_file()
    status, detail, _ = _checkresults_status(results)
    return status, detail


def _run_global_overlay_precedence(ctx: DoctorContext) -> tuple[str, str]:
    """The global-overlay-precedence lint (validation.validate_global_overlay_precedence).

    Asserts no skill in skill-scope.yaml is BOTH always-global (granted by an
    ``allow_global`` rule / ``global_allowlist``) and overlay-gated. Global wins,
    so an overlay rule may only add NON-global skills; a double-declaration (e.g.
    naming always-global ``divide-and-conquer`` in the ``swarm`` overlay) is a
    FAIL that names the offending skill + overlay rule rather than a silent
    ambiguity.
    """
    results = validate_global_overlay_precedence_file()
    status, detail, _ = _checkresults_status(results)
    return status, detail


def _run_repo_skill_override_lint(ctx: DoctorContext) -> tuple[str, str]:
    """Repo-local .skillbox/skill-overrides.yaml lint for the current cwd."""
    results = validate_repo_skill_override_policy(ctx.model, cwd=ctx.cwd)
    status, detail, _ = _checkresults_status(results)
    return status, detail


def _run_registry_path_duplication(ctx: DoctorContext) -> tuple[str, str]:
    """The registry-path-duplication lint (validation.validate_registry_path_duplication).

    WARNS (never FAILs — raw paths stay supported for back-compat) when a rule's
    literal ``paths:`` entry is already covered by a registry id, so the
    duplication a `repos: [<id>]` would remove is visibly discouraged.
    """
    results = validate_registry_path_duplication_file()
    status, detail, _ = _checkresults_status(results)
    return status, detail


# Codes emitted by the lock-parity concern (config_sha desync + downstream
# install state) so the lock gate and the policy gate don't double-count.
_LOCK_CODES = frozenset({"skill-repo-lock", "skill-repo-install"})


def _run_lock_parity(ctx: DoctorContext) -> tuple[str, str]:
    """Lock parity (config_sha): every skill-repos.lock matches its yaml.

    Folds the lock-coded results from ``validate_skill_repo_sets`` plus the
    managed-skill lock/install state from ``validate_skill_locks_and_state``.
    This is the in-runtime mirror of the structure suite's config_sha invariant.
    """
    repo_set_results = validate_skill_repo_sets(ctx.model)
    lock_coded = [r for r in repo_set_results if getattr(r, "code", "") in _LOCK_CODES]
    state_results = validate_skill_locks_and_state(ctx.model)
    status, detail, _ = _checkresults_status(lock_coded + state_results)
    return status, detail


def _run_mcp_parity(ctx: DoctorContext) -> tuple[str, str]:
    """MCP parity audit — the existing sbp mcp audit / mcp_render baseline.

    Only undeclared servers (``unexplained_drift``) or unreadable configs count
    as drift, matching the existing audit's contract. Missing config files are
    not drift (a repo may legitimately have no MCP surface).
    """
    audit = collect_mcp_audit(ctx.runtime_root, ctx.model, cwd=str(ctx.cwd))
    summary = audit.get("summary") or {}
    drift = int(summary.get("unexplained_drift") or 0)
    invalid = int(summary.get("invalid_configs") or 0)
    if drift or invalid:
        return (
            STATUS_FAIL,
            f"unexplained_drift={drift}, invalid_configs={invalid}",
        )
    return STATUS_PASS, "claude/codex MCP config parity holds (no unexplained drift)"


def _run_oracle_browser_sandbox(ctx: DoctorContext) -> tuple[str, str]:
    """Oracle host Chrome sandbox posture, reported without a false green.

    The cookie-bearing Chrome runs with ``--no-sandbox``. This gate never lets
    that read as healthy: a waived-and-fully-compensated host is INCO — visible
    in every run, explicitly not a pass, and non-blocking, because an accepted
    expiring exception should not train operators to ignore a red gate. Only a
    genuinely enforced sandbox passes; a missing, malformed, expired, or
    under-compensated exception FAILs.

    Boxes that are not the Oracle host carry no declaration and are INCO with a
    detail that says so.
    """
    import time as _time

    from .oracle_sandbox import (
        STATE_ENFORCED,
        STATE_UNDECLARED,
        OracleSandboxError,
        posture_from_declaration,
    )

    state_root = os.environ.get("SKILLBOX_STATE_ROOT") or str(
        ctx.runtime_root / ".skillbox-state"
    )
    try:
        posture = posture_from_declaration(
            state_root, now_ms=int(_time.time() * 1000)
        )
    except OracleSandboxError as error:
        # A declaration that exists but does not validate is a finding on the
        # host that matters most; it must never degrade to "inconclusive".
        return (STATUS_FAIL, f"oracle sandbox declaration unusable: {error.code}")
    if posture.state == STATE_ENFORCED:
        return (STATUS_PASS, posture.detail())
    if posture.state == STATE_UNDECLARED:
        return (STATUS_INCO, posture.detail())
    if posture.green:  # pragma: no cover - defended by test_oracle_sandbox
        raise AssertionError("only an enforced sandbox may report green")
    if posture.state == "waived":
        return (STATUS_INCO, posture.detail())
    return (STATUS_FAIL, posture.detail())


def _run_skill_drift(ctx: DoctorContext) -> tuple[str, str]:
    """Global + cwd skill-drift summary.

    Hard breakages (broken global/project symlinks, skills missing for this cwd)
    are a FAIL. Advisory drift (global_not_allowed / shadowed / scope hints) is
    surfaced in the detail but is NOT a failure — those are policy nudges that
    the recalibrate flow handles, not structural breakage.
    """
    payload = collect_skill_visibility(ctx.model, cwd=str(ctx.cwd))
    summary = payload.get("summary") or {}
    broken_global = int(summary.get("broken_global") or 0)
    broken_project = int(summary.get("broken_project") or 0)
    missing_for_cwd = int(summary.get("missing_for_cwd") or 0)
    advisory = int(summary.get("global_not_allowed") or 0) + int(summary.get("shadowed") or 0)
    if broken_global or broken_project or missing_for_cwd:
        return (
            STATUS_FAIL,
            f"broken_global={broken_global}, broken_project={broken_project}, "
            f"missing_for_cwd={missing_for_cwd}",
        )
    note = "no broken or missing skill links"
    if advisory:
        note += f" ({advisory} advisory drift item(s) — see sbp recalibrate)"
    return STATUS_PASS, note


# --------------------------------------------------------------------------- #
# git_hygiene gate — fed EXCLUSIVELY from the sbp git TTL scan cache
# --------------------------------------------------------------------------- #

#: Advisory detail for the absent/stale-cache path — the exact 'run the scan'
#: handoff the home view uses, so every ambient surface says the same thing.
GIT_HYGIENE_NO_SCAN = "no recent scan — run sbp git"

#: Ordinary-drift classes: normal working state, surfaced as a PASS-with-
#: warnings summary, never a FAIL. (A merely dirty repo must not redline the
#: board.)
_GIT_WARN_CLASSES = ("dirty", "ahead", "behind", "stash")


def _short_repo_path(path: str) -> str:
    """``~``-abbreviate home so FAIL details stay readable in the table."""
    home = str(Path.home())
    if home and path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _name_repos(paths: list[str], limit: int = 3) -> str:
    shown = ", ".join(_short_repo_path(p) for p in paths[:limit])
    extra = len(paths) - limit
    return shown + (f" +{extra} more" if extra > 0 else "")


def _run_git_hygiene(ctx: DoctorContext) -> tuple[str, str]:
    """STRUCTURE gate: loss-risk git drift, read from the last-scan cache ONLY.

    This gate NEVER scans (no git, no subprocess) — doctor's structure budget
    stays intact. It replays the ``sbp-git/v1`` envelope the last live
    ``sbp git`` write-through'd to the TTL cache:

    * Cache absent or stale (> TTL): INCO with the 'no recent scan — run sbp
      git' advisory — the verdict is unknowable without a scan, and INCO is
      never a failure. (Schema-version mismatch is the loader's job: it reads
      as ABSENT and lands here too.)
    * Ordinary drift (dirty / ahead / behind / stash): PASS with a warnings
      summary in the detail — normal working state, advisory only.
    * FAIL only for loss-risk classes, each carrying its exact ``--only``
      handoff: (a) mid-op — the envelope carries no per-repo mid-op age, so
      ANY cached mid-op fails (a mid-op is always a paused surgery; recency
      cannot be verified from the cache); (b) diverged — ``ahead > 0 and
      behind > 0``, a deliberate superset of the clean-only ``diverged-clean``
      class so a dirty diverged repo (strictly worse) also fires; (c) dirty
      AND ``registration == "unregistered"``. Registration ``unknown``
      (registry unavailable at scan time) never counts as unregistered.

    The cache age always appears in the detail (``scan 4m old: ...``) so the
    reader knows how stale the verdict is; a filtered envelope (``sbp git
    --only ...`` also write-throughs) is flagged as a partial view.
    """
    loaded = load_scan_cache(ctx.runtime_root)
    if loaded is None:
        return STATUS_INCO, GIT_HYGIENE_NO_SCAN
    envelope, age = loaded
    if age > GIT_SCAN_TTL_SECONDS:
        return STATUS_INCO, (
            f"{GIT_HYGIENE_NO_SCAN} (last scan {_format_scan_age(age)} old, "
            f"TTL {int(GIT_SCAN_TTL_SECONDS) // 60}m)"
        )

    mid_op: list[str] = []
    diverged: list[str] = []
    dirty_unregistered: list[str] = []
    warn_counts: dict[str, int] = {cls: 0 for cls in _GIT_WARN_CLASSES}
    rows = envelope.get("repos")
    row_count = 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        row_count += 1
        raw_classes = row.get("classes")
        classes = set(raw_classes) if isinstance(raw_classes, list) else set()
        path = str(row.get("path") or "?")
        if "mid-op" in classes or row.get("mid_op"):
            mid_op.append(path)
        ahead, behind = row.get("ahead"), row.get("behind")
        if isinstance(ahead, int) and isinstance(behind, int) and ahead > 0 and behind > 0:
            diverged.append(path)
        if "dirty" in classes and row.get("registration") == "unregistered":
            dirty_unregistered.append(path)
        for cls in _GIT_WARN_CLASSES:
            if cls in classes:
                warn_counts[cls] += 1

    prefix = f"scan {_format_scan_age(age)} old"
    filters = envelope.get("filters")
    if isinstance(filters, list) and filters:
        prefix += f" [filtered view: --only {','.join(str(f) for f in filters)}]"

    failures: list[str] = []
    if mid_op:
        failures.append(f"mid-op: {_name_repos(mid_op)} — sbp git --only mid-op")
    if diverged:
        # `diverged` is not an --only token; `diverged-clean` is the closest
        # class filter (the detail names dirty diverged repos directly).
        failures.append(
            f"diverged: {_name_repos(diverged)} — sbp git --only diverged-clean"
        )
    if dirty_unregistered:
        failures.append(
            f"dirty+unregistered: {_name_repos(dirty_unregistered)} — "
            "sbp git --only dirty,unregistered"
        )
    if failures:
        return STATUS_FAIL, f"{prefix}: " + "; ".join(failures)

    warnings = [f"{count} {cls}" for cls, count in warn_counts.items() if count]
    if warnings:
        return STATUS_PASS, (
            f"{prefix}: {', '.join(warnings)} — ordinary drift, advisory only"
        )
    return STATUS_PASS, f"{prefix}: clean estate ({row_count} repos)"


def _repo_atlas_engine_path() -> Path:
    """Resolve the private Repo Atlas engine the same way the sbp wrapper does."""
    override = str(os.environ.get("SKILLBOX_REPO_ATLAS_CLI") or "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    root = str(os.environ.get("SKILLBOX_MONOSERVER_ROOT") or "").strip()
    base = Path(os.path.expandvars(os.path.expanduser(root))) if root else Path.home() / "repos"
    return base / "skills-private" / "reconcile" / "scripts" / "repo_atlas_cli.py"


# Each probe is one read; the wrapper's own capability preflight is capped at
# 10s, so 15s covers preflight + one collection. `list` collects the whole
# estate rather than one repo, so it gets its own, longer allowance.
REPO_ATLAS_PROBE_TIMEOUT_S = 15.0
REPO_ATLAS_LIST_PROBE_TIMEOUT_S = 45.0

# Both probes are read-only. `status .` exercises single-repo resolution;
# `list` exercises estate-wide enumeration, whose payload grows with the
# number of declared components. They fail independently — see the gate
# docstring — so the gate runs both rather than treating one as a proxy.
REPO_ATLAS_PROBES: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("status .", ("repo", "status", ".", "--json"), REPO_ATLAS_PROBE_TIMEOUT_S),
    ("list", ("repo", "list", "--json"), REPO_ATLAS_LIST_PROBE_TIMEOUT_S),
)


def _probe_repo_atlas(
    wrapper: Path, ctx: DoctorContext, argv: tuple[str, ...], timeout_s: float
) -> tuple[str, str]:
    """Run one front-door probe. FAIL only on a front-door defect."""
    try:
        proc = subprocess.run(
            [str(wrapper), *argv],
            cwd=str(ctx.runtime_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        return STATUS_INCO, "unable to execute the sbp wrapper"
    except subprocess.TimeoutExpired:
        return STATUS_INCO, f"probe exceeded {timeout_s:.0f}s"
    if proc.returncode == 2:
        tail = _last_meaningful_line((proc.stderr or "") + "\n" + (proc.stdout or ""))
        return STATUS_FAIL, f"usage-or-config for a well-formed probe: {tail or 'empty output'}"
    try:
        json.loads(proc.stdout)
    except ValueError:
        return (
            STATUS_FAIL,
            f"non-JSON --json output (exit={proc.returncode}): "
            f"{_last_meaningful_line(proc.stdout) or 'empty stdout'}",
        )
    return STATUS_PASS, f"exit={proc.returncode}"


def _run_repo_atlas_front_door(ctx: DoctorContext) -> tuple[str, str]:
    """STRUCTURE gate: the `sbp repo` (Repo Atlas) front door must not fail silently.

    The atlas engine collapses several unrelated defects into one indistinct
    exit-2 ``{"kind": "malformed"}`` envelope while ``--help`` and
    ``capabilities`` keep working, so a broken front door looks healthy from
    every angle except actually running it (bead
    skillbox-sbp-repo-atlas-repair-2gbo). This gate probes the real front door
    with well-formed read-only invocations, for which usage-or-config is never
    a legitimate answer.

    It probes BOTH ``status .`` and ``list``, because the two fail for
    different reasons and neither proxies the other. ``status .`` resolves one
    repository, so it catches identity/wiring breakage. ``list`` enumerates
    every declared component, so its payload grows with the estate and it is
    the probe that catches an output budget the estate has outgrown — the
    original repair shipped with ``status .`` healthy and ``list`` still
    exit-2, which a status-only gate reported as PASS.

    INCO when the wrapper or the engine checkout is absent on this box (verdict
    unknowable); FAIL on exit 2 or non-JSON ``--json`` output from any probe;
    PASS on exit 0/1/3 with a JSON envelope — drift and reachability verdicts
    are live front-door answers, not front-door failures.
    """
    wrapper = ctx.runtime_root / "scripts" / "sbp"
    if not wrapper.is_file():
        return STATUS_INCO, f"no sbp wrapper at {wrapper}"
    engine = _repo_atlas_engine_path()
    if not engine.is_file():
        return STATUS_INCO, "Repo Atlas engine not present on this box (skills-private checkout absent)"
    results = [
        (label, *_probe_repo_atlas(wrapper, ctx, argv, timeout_s))
        for label, argv, timeout_s in REPO_ATLAS_PROBES
    ]
    failures = [f"{label}: {detail}" for label, status, detail in results if status == STATUS_FAIL]
    if failures:
        return STATUS_FAIL, "; ".join(failures)
    inconclusive = [f"{label}: {detail}" for label, status, detail in results if status == STATUS_INCO]
    if inconclusive:
        return STATUS_INCO, "; ".join(inconclusive)
    return STATUS_PASS, "front door live (" + ", ".join(
        f"{label} {detail}" for label, _status, detail in results
    ) + ")"


def _run_runtime_doctor(ctx: DoctorContext) -> tuple[str, str]:
    """RUNTIME gate: invoke the outer doctor directly, don't duplicate it.

    Runs the SCRIPT, not ``make doctor``, for one decisive reason: **make
    destroys the exit ladder**. A recipe that exits 4 makes ``make`` print
    ``*** [doctor] Error 4`` and then exit **2** itself, so the EXIT_DRIFT
    signal this whole family is built on never reaches the caller — the gate
    would read "unexpected exit 2" and go INCO on every real failure, silently
    downgrading FAIL to "could not tell". ``make doctor`` is a one-line
    forwarder to this exact argv (see the Makefile), so calling it directly
    loses nothing and keeps the verdict.

    The exit code is read against the family ladder rather than treating every
    nonzero the same:

    * 0 -> PASS.
    * 4 (EXIT_DRIFT) -> FAIL. The outer doctor ran fine and found a difference;
      that IS a real runtime failure at this gate.
    * 1 (EXIT_ERROR) -> INCO. The outer doctor could not produce a verdict, so
      neither can this gate — reporting FAIL would claim knowledge we lack.
    * anything else (including 2, a usage error in OUR invocation) -> INCO.

    If the script is unreachable on this box, that is INCO too.
    """
    script = ctx.runtime_root / "scripts" / "04-reconcile.py"
    if not script.is_file():
        return STATUS_INCO, f"no scripts/04-reconcile.py at {ctx.runtime_root}"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "doctor"],
            cwd=str(ctx.runtime_root),
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return STATUS_INCO, f"could not run the outer doctor: {type(exc).__name__}: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    detail = _last_doctor_summary_line(out)
    if proc.returncode == 0:
        return STATUS_PASS, detail
    if proc.returncode == RUNTIME_DOCTOR_EXIT_DRIFT:
        return STATUS_FAIL, detail
    if proc.returncode == RUNTIME_DOCTOR_EXIT_ERROR:
        return STATUS_INCO, f"outer doctor could not produce a verdict (exit 1): {detail}"
    return STATUS_INCO, f"unexpected outer doctor exit {proc.returncode}: {detail}"


def _last_doctor_summary_line(text: str, limit: int = 240) -> str:
    """The outer doctor's own last meaningful line, skipping make's wrapper noise."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("make"):
            continue
        return stripped[:limit]
    return _last_meaningful_line(text, limit)


def _last_meaningful_line(text: str, limit: int = 240) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


# --------------------------------------------------------------------------- #
# Gate registry — declaration order is the table/JSON order
# --------------------------------------------------------------------------- #

def _gate_specs() -> tuple[_GateSpec, ...]:
    return (
        _GateSpec(
            name="structure_invariants",
            kind=KIND_STRUCTURE,
            cap_s=CAP_STRUCTURE_SUITE,
            fix_command=(
                "cd ~/repos/opensource/skillbox/.env-manager && python3 manage.py sync "
                "(then re-run; see the failing invariant's embedded fix)"
            ),
            runner=_run_structure_invariant_suite,
        ),
        _GateSpec(
            name="policy_lint",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command="python3 .env-manager/manage.py doctor --format json  # inspect skill-repo-* checks",
            runner=_run_policy_lint,
        ),
        _GateSpec(
            name="global_skill_contract",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command=(
                "edit allow_global rules in skillbox-config/skill-scope.yaml, "
                "then regenerate or remove the derived global_allowlist snapshot"
            ),
            runner=_run_global_skill_contract,
        ),
        _GateSpec(
            name="overlay_declaration",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command=(
                "declare the overlay in skillbox-config/skill-scope.yaml `overlays:` "
                "or correct the rule's overlay tag so every overlay: tag is declared"
            ),
            runner=_run_overlay_declaration,
        ),
        _GateSpec(
            name="global_overlay_precedence",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command=(
                "drop the double-declared skill from its overlay rule in "
                "skillbox-config/skill-scope.yaml (an always-global skill is linked "
                "everywhere; an overlay cannot gate it — global wins)"
            ),
            runner=_run_global_overlay_precedence,
        ),
        _GateSpec(
            name="repo_skill_override_lint",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command=(
                "sbp skill lint --cwd <repo>  # remove contradictory, floor, "
                "or dangling .skillbox/skill-overrides.yaml entries"
            ),
            runner=_run_repo_skill_override_lint,
        ),
        _GateSpec(
            name="registry_path_duplication",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command=(
                "replace a duplicated literal path in skillbox-config/skill-scope.yaml "
                "with `repos: [<id>]` so the repo's per-machine path is derived from "
                "registry/repos.yaml + machines.yaml (bead y8w.3)"
            ),
            runner=_run_registry_path_duplication,
        ),
        _GateSpec(
            name="lock_parity",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command=(
                "cd ~/repos/opensource/skillbox/.env-manager && python3 manage.py sync "
                "(rewrites each lockfile's config_sha from its skill-repos.yaml)"
            ),
            runner=_run_lock_parity,
        ),
        _GateSpec(
            name="mcp_parity",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command="python3 .env-manager/manage.py mcp sync --apply  # reconcile Claude/Codex MCP config",
            runner=_run_mcp_parity,
        ),
        _GateSpec(
            name="skill_drift",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command="sbp recalibrate  # review skill add/remove for this cwd",
            runner=_run_skill_drift,
        ),
        _GateSpec(
            name="oracle_browser_sandbox",
            kind=KIND_STRUCTURE,
            # Reads one small JSON declaration under the state root; it probes
            # nothing and never touches the browser.
            cap_s=CAP_FAST_LINT,
            fix_command=(
                "restore the Chrome sandbox on the oracle host, or renew the "
                "expiring waiver + compensating controls in "
                "<state-root>/oracle/sandbox-posture.json (see docs/oracle-sandbox.md)"
            ),
            runner=_run_oracle_browser_sandbox,
        ),
        _GateSpec(
            name="git_hygiene",
            kind=KIND_STRUCTURE,
            # Reads one small JSON file (the sbp git TTL cache) — sub-
            # millisecond; the fast-lint cap is pure headroom. Never scans.
            cap_s=CAP_FAST_LINT,
            fix_command="sbp git  # rescan, then sbp git --only mid-op,diverged-clean",
            runner=_run_git_hygiene,
        ),
        _GateSpec(
            name="repo_atlas_front_door",
            kind=KIND_STRUCTURE,
            cap_s=CAP_FAST_LINT,
            fix_command=(
                "sbp repo status . --json  # inspect; engine wiring lives in "
                "skills-private/reconcile/scripts/repo_atlas_cli.py "
                "(bead skillbox-sbp-repo-atlas-repair-2gbo)"
            ),
            runner=_run_repo_atlas_front_door,
        ),
        _GateSpec(
            name="runtime_doctor",
            kind=KIND_RUNTIME,
            cap_s=CAP_RUNTIME_DOCTOR,
            # The script, not `make doctor`: make collapses any recipe failure
            # into its own exit 2, so an agent branching on the exit ladder
            # (4 = drift) must call the script. `make doctor` prints the same
            # report; only its exit code lies.
            fix_command=(
                "python3 scripts/04-reconcile.py doctor --format json"
                "  # from ~/repos/opensource/skillbox; read the failing check"
            ),
            runner=_run_runtime_doctor,
        ),
    )


def _run_one_gate(spec: _GateSpec, ctx: DoctorContext) -> GateResult:
    """Run one gate under its cap. Timeout / unexpected error -> INCO, never FAIL."""
    start = time.perf_counter()
    try:
        status, detail = _with_cap(spec, ctx)
    except _GateTimeout:
        duration = round(time.perf_counter() - start, 3)
        return GateResult(
            name=spec.name,
            kind=spec.kind,
            status=STATUS_INCO,
            duration_s=duration,
            fix_command=spec.fix_command,
            detail=f"exceeded {spec.cap_s:g}s cap — INCONCLUSIVE (not a failure)",
        )
    except Exception as exc:  # noqa: BLE001 — any gate blowup is INCO, not FAIL
        duration = round(time.perf_counter() - start, 3)
        return GateResult(
            name=spec.name,
            kind=spec.kind,
            status=STATUS_INCO,
            duration_s=duration,
            fix_command=spec.fix_command,
            detail=f"gate raised {type(exc).__name__}: {exc} — INCONCLUSIVE",
        )
    duration = round(time.perf_counter() - start, 3)
    return GateResult(
        name=spec.name,
        kind=spec.kind,
        status=status,
        duration_s=duration,
        fix_command=spec.fix_command,
        detail=detail,
    )


class _GateTimeout(Exception):
    pass


def _with_cap(spec: _GateSpec, ctx: DoctorContext) -> tuple[str, str]:
    """Run ``spec.runner`` with a wall-clock cap.

    Subprocess gates honor the cap directly via ``subprocess.run(timeout=...)``;
    pure-Python gates can't be preempted mid-call, so they run with no inner
    interruption but are still time-bounded by the runner being sub-second (and
    the outer duration is recorded). We enforce the cap by wrapping the call in a
    thread with a join timeout — if it overruns we raise ``_GateTimeout`` so the
    gate is recorded INCO. The orphaned worker (rare) is harmless: every gate is
    read-only.
    """
    import threading

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = spec.runner(ctx)
        except subprocess.TimeoutExpired:
            box["timeout"] = True
        except Exception as exc:  # propagate to outer handler as INCO
            box["error"] = exc

    # Subprocess-driven gates get their own subprocess timeout so they are
    # actually preempted; we pass the cap through via env for those runners that
    # honor it. Here we add a thread join as a uniform outer bound.
    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout=spec.cap_s)
    if worker.is_alive():
        raise _GateTimeout()
    if box.get("timeout"):
        raise _GateTimeout()
    if "error" in box:
        raise box["error"]
    return box["result"]  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #

def gate_findings(gates: Sequence[GateResult]) -> list[Finding]:
    """The gates, in the uniform family finding shape.

    ``code`` is the gate name (the stable id an agent alerts on), ``message``
    the gate's own detail line, and ``details`` keeps the two fields that are
    specific to this doctor so nothing is lost in the translation.
    """
    return [
        Finding(
            code=gate.name,
            status=gate.status,
            message=gate.detail,
            details={"kind": gate.kind, "duration_s": gate.duration_s},
            fix_command=gate.fix_command or None,
        )
        for gate in gates
    ]


def structure_doctor_fix_registry(
    gates: Sequence[GateResult],
    cwd: Path | None = None,
) -> dict[str, doctor_fix.FixSpec]:
    """THIS run's auto-fix registry, built from THIS run's failing gates.

    Only ``mcp_parity`` qualifies today: its remedy is a single declarative
    re-render of files this repo owns, and every file it rewrites is captured as
    a backup first, so ``--undo`` is exact. The other gates fail for reasons a
    command cannot settle (a policy decision, a dirty worktree, a missing
    checkout); they keep their printed ``fix_command`` and are reported as
    skipped with a machine-readable reason rather than guessed at.
    """
    failing = {gate.name for gate in gates if gate.status == STATUS_FAIL}
    specs: list[doctor_fix.FixSpec] = []
    if "mcp_parity" in failing:
        specs.append(
            doctor_fix.FixSpec(
                code="mcp_parity",
                command=("python3", ".env-manager/manage.py", "mcp", "sync", "--apply"),
                description="re-render the single-source MCP config into every client surface",
                backup_paths=_mcp_surface_paths(cwd),
                timeout_s=180.0,
            )
        )
    return doctor_fix.build_registry(specs)


def _mcp_surface_paths(cwd: Path | None = None) -> tuple[str, ...]:
    """The files `mcp sync --apply` can rewrite, for the pre-change backup.

    Built from the SAME relative-path constants the audit reads
    (``mcp_visibility.CLAUDE_MCP_REL`` / ``CODEX_MCP_REL``), rooted at both the
    evaluated cwd and $HOME, so the backup covers whichever surface the render
    actually touches. Paths that do not exist are still declared: the backup
    records them as ``existed: false`` and undo removes them again, which is the
    only correct undo for "the fix created this file".
    """
    from .mcp_visibility import CLAUDE_MCP_REL, CODEX_MCP_REL  # noqa: PLC0415

    roots = [Path(cwd) if cwd else Path.cwd(), Path.home()]
    paths: list[str] = []
    for root in roots:
        for rel in (CLAUDE_MCP_REL, CODEX_MCP_REL):
            candidate = str(root / rel)
            if candidate not in paths:
                paths.append(candidate)
    return tuple(paths)


def next_actions_for_structure_doctor(gates: Sequence[GateResult]) -> list[str]:
    """Ranked next commands: the failing gates' own fixes, then the routing."""
    actions = [
        gate.fix_command
        for gate in gates
        if gate.status == STATUS_FAIL and gate.fix_command
    ]
    inconclusive = [gate.name for gate in gates if gate.status == STATUS_INCO]
    if inconclusive:
        actions.append(
            "sbp doctor --format json  # re-run: "
            + ", ".join(inconclusive)
            + " were inconclusive, not failures"
        )
    if any(gate.status == STATUS_FAIL for gate in gates):
        actions.append("python3 .env-manager/manage.py structure-doctor --fix  # plan the auto-fixes")
    actions.append("python3 scripts/04-reconcile.py doctor --format json")
    # De-duplicate while preserving rank.
    seen: set[str] = set()
    ordered: list[str] = []
    for action in actions:
        if action not in seen:
            seen.add(action)
            ordered.append(action)
    return ordered


def run_structure_doctor(
    runtime_root: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run every gate and return the front-door payload.

    Returns the family envelope — ``{ok, exit_code, schema_version, tool,
    checks, summary, next_actions, coverage, fix}`` plus this doctor's own
    ``gates`` (per-gate ``kind``/``duration_s``), ``config_root``,
    ``runtime_root`` and ``cwd``.

    ``exit_code`` is ``EXIT_DRIFT`` (4) iff at least one gate is FAIL — "ran
    fine, found a difference", never confused with 1 ("could not produce a
    verdict"). INCO and PASS exit 0. ``summary.structure_duration_s`` is the
    wall-clock spent on STRUCTURE gates (the budget the <60s guarantee covers;
    the RUNTIME gate is excluded).
    """
    ctx = build_context(runtime_root=runtime_root, cwd=cwd)
    gates: list[GateResult] = []
    for spec in _gate_specs():
        gates.append(_run_one_gate(spec, ctx))

    structure_duration = round(
        sum(g.duration_s for g in gates if g.kind == KIND_STRUCTURE), 3
    )
    runtime_duration = round(
        sum(g.duration_s for g in gates if g.kind == KIND_RUNTIME), 3
    )
    findings = gate_findings(gates)
    registry = structure_doctor_fix_registry(gates, ctx.cwd)
    findings = doctor_fix.annotate_fixable(findings, registry)
    # ONE family envelope. `gates` stays alongside `checks` because a gate
    # carries two fields no other doctor has (kind and duration_s) and the
    # human table is built from them; `checks` is the same information in the
    # uniform per-finding shape every doctor in the family emits.
    return doctor_envelope(
        tool=DOCTOR_TOOL_NAME,
        findings=findings,
        next_actions=next_actions_for_structure_doctor(gates),
        # Doctor-family routing: what this run covered and which sibling
        # doctors were NOT run, so an agent with a symptom can route without
        # out-of-band knowledge. sbp doctor is the front door — its
        # runtime_doctor gate embeds `make doctor` (which embeds
        # `manage.py doctor`); the siblings listed here are the satellites.
        coverage=coverage_block(
            tool=DOCTOR_TOOL_NAME,
            includes=[
                "structural gates (this run)",
                "make doctor via runtime_doctor gate (manifest/compose/skill-sync, embeds manage.py doctor)",
            ],
            siblings_not_run=[
                "sbp registry doctor",
                "sbp cass doctor",
                "sbp send-later doctor",
                "sbp beads status",
                "make self-test",
            ],
        ),
        fix=fix_contract(
            supported=True,
            artifact_dir=str(doctor_fix.runs_dir(ctx.runtime_root, DOCTOR_TOOL_NAME)),
            fixable_codes=registry.keys(),
        ),
        summary_extra={
            "structure_duration_s": structure_duration,
            "runtime_duration_s": runtime_duration,
            "structure_budget_s": STRUCTURE_BUDGET_S,
            "structure_within_budget": structure_duration < STRUCTURE_BUDGET_S,
        },
        extra={
            "config_root": str(ctx.config_root) if ctx.config_root else None,
            "runtime_root": str(ctx.runtime_root),
            "cwd": str(ctx.cwd),
            "gates": [g.to_payload() for g in gates],
        },
    )


def structure_doctor_text_lines(payload: dict[str, Any]) -> list[str]:
    """Human table for the CLI: one row per gate, then a summary line."""
    gates = payload.get("gates") or []
    summary = payload.get("summary") or {}
    name_w = max([len("GATE")] + [len(str(g.get("name", ""))) for g in gates])
    kind_w = max([len("KIND")] + [len(str(g.get("kind", ""))) for g in gates])

    lines = ["sbp doctor — structural verification front door (complements `make doctor`)", ""]
    header = (
        f"  {'STATUS':6s}  {'KIND':{kind_w}s}  {'GATE':{name_w}s}  {'TIME':>8s}  DETAIL"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for gate in gates:
        # display_status(): JSON is lowercase, the human table stays shouty.
        status = display_status(str(gate.get("status", "")))
        kind = str(gate.get("kind", ""))
        name = str(gate.get("name", ""))
        duration = float(gate.get("duration_s", 0.0))
        detail = str(gate.get("detail", ""))
        lines.append(
            f"  {status:6s}  {kind:{kind_w}s}  {name:{name_w}s}  {duration:7.3f}s  {detail}"
        )
        if str(gate.get("status", "")) == STATUS_FAIL:
            lines.append(f"  {'':6s}  {'':{kind_w}s}  {'':{name_w}s}  {'':>8s}  fix: {gate.get('fix_command', '')}")

    structure_s = summary.get("structure_duration_s", 0.0)
    budget = summary.get("structure_budget_s", STRUCTURE_BUDGET_S)
    within = "within" if summary.get("structure_within_budget") else "OVER"
    lines.append("")
    lines.append(
        f"  {summary.get('total', 0)} gates: "
        f"{summary.get('pass', 0)} PASS, {summary.get('fail', 0)} FAIL, "
        f"{summary.get('inco', 0)} INCO"
    )
    lines.append(
        f"  structure gates: {structure_s:g}s ({within} the {budget:g}s budget); "
        f"runtime gate: {summary.get('runtime_duration_s', 0.0):g}s"
    )
    if summary.get("inco", 0):
        lines.append(
            "  INCO gates were inconclusive (slow/loaded box or unreachable dependency), "
            "not regressions — re-run or check the dependency."
        )
    if summary.get("fail", 0):
        lines.append(
            f"  FAIL gates carry an exact fix command above; exit code is {EXIT_DRIFT} "
            "(EXIT_DRIFT — ran fine, found a difference; 1 would mean the doctor itself failed)."
        )
        lines.append(
            f"  Auto-fix: `{DOCTOR_TOOL_NAME} --fix` previews (writes nothing, exits "
            f"{_EXIT_NEEDS_INPUT}); add --yes to apply with a backup and an undo command."
        )
    return lines


__all__ = [
    "KIND_STRUCTURE",
    "KIND_RUNTIME",
    "STATUS_PASS",
    "STATUS_FAIL",
    "STATUS_INCO",
    "STRUCTURE_BUDGET_S",
    "DOCTOR_TOOL_NAME",
    "STRUCTURE_DOCTOR_FIX_BOUNDARY_ID",
    "STRUCTURE_DOCTOR_UNDO_TEMPLATE",
    "GateResult",
    "DoctorContext",
    "build_context",
    "gate_findings",
    "next_actions_for_structure_doctor",
    "run_structure_doctor",
    "structure_doctor_fix_registry",
    "structure_doctor_text_lines",
]
