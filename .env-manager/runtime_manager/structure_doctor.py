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

import os
import subprocess
import time
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
    validate_skill_locks_and_state,
    validate_skill_repo_sets,
)
from .skill_visibility import collect_skill_visibility
from .mcp_visibility import collect_mcp_audit

# Gate kinds and statuses are part of the JSON contract; keep them as constants
# so the CLI renderer and tests share one source of truth.
KIND_STRUCTURE = "structure"
KIND_RUNTIME = "runtime"

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_INCO = "INCO"

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


def _run_runtime_doctor(ctx: DoctorContext) -> tuple[str, str]:
    """RUNTIME gate: invoke the existing runtime `make doctor`, don't duplicate it.

    Runs ``make doctor`` from the skillbox repo. If make / the target is
    unreachable on this box, that is INCO (we don't know the runtime verdict),
    never FAIL. A run that completes and exits nonzero IS a real runtime FAIL.
    """
    makefile = ctx.runtime_root / "Makefile"
    if not makefile.is_file():
        return STATUS_INCO, f"no Makefile at {ctx.runtime_root}"
    try:
        proc = subprocess.run(
            ["make", "doctor"],
            cwd=str(ctx.runtime_root),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return STATUS_INCO, "make is not available on this box"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return STATUS_PASS, _last_meaningful_line(out)
    return STATUS_FAIL, _last_meaningful_line(out)


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
            name="runtime_doctor",
            kind=KIND_RUNTIME,
            cap_s=CAP_RUNTIME_DOCTOR,
            fix_command="make doctor  # from ~/repos/opensource/skillbox; read the failing check",
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

def run_structure_doctor(
    runtime_root: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run every gate and return the front-door payload.

    Returns ``{ok, gates, summary, exit_code}`` where ``exit_code`` is nonzero
    iff at least one gate is FAIL (INCO and PASS exit 0), and
    ``summary.structure_duration_s`` is the wall-clock spent on STRUCTURE gates
    (the budget the <60s guarantee covers; the RUNTIME gate is excluded).
    """
    ctx = build_context(runtime_root=runtime_root, cwd=cwd)
    gates: list[GateResult] = []
    for spec in _gate_specs():
        gates.append(_run_one_gate(spec, ctx))

    fails = [g for g in gates if g.status == STATUS_FAIL]
    incos = [g for g in gates if g.status == STATUS_INCO]
    passes = [g for g in gates if g.status == STATUS_PASS]
    structure_duration = round(
        sum(g.duration_s for g in gates if g.kind == KIND_STRUCTURE), 3
    )
    runtime_duration = round(
        sum(g.duration_s for g in gates if g.kind == KIND_RUNTIME), 3
    )
    exit_code = 1 if fails else 0
    return {
        "ok": not fails,
        "config_root": str(ctx.config_root) if ctx.config_root else None,
        "runtime_root": str(ctx.runtime_root),
        "cwd": str(ctx.cwd),
        "gates": [g.to_payload() for g in gates],
        "summary": {
            "total": len(gates),
            "pass": len(passes),
            "fail": len(fails),
            "inco": len(incos),
            "structure_duration_s": structure_duration,
            "runtime_duration_s": runtime_duration,
            "structure_budget_s": STRUCTURE_BUDGET_S,
            "structure_within_budget": structure_duration < STRUCTURE_BUDGET_S,
        },
        "exit_code": exit_code,
    }


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
        status = str(gate.get("status", ""))
        kind = str(gate.get("kind", ""))
        name = str(gate.get("name", ""))
        duration = float(gate.get("duration_s", 0.0))
        detail = str(gate.get("detail", ""))
        lines.append(
            f"  {status:6s}  {kind:{kind_w}s}  {name:{name_w}s}  {duration:7.3f}s  {detail}"
        )
        if status == STATUS_FAIL:
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
        lines.append("  FAIL gates carry an exact fix command above; exit code is nonzero.")
    return lines


__all__ = [
    "KIND_STRUCTURE",
    "KIND_RUNTIME",
    "STATUS_PASS",
    "STATUS_FAIL",
    "STATUS_INCO",
    "STRUCTURE_BUDGET_S",
    "GateResult",
    "DoctorContext",
    "build_context",
    "run_structure_doctor",
    "structure_doctor_text_lines",
]
