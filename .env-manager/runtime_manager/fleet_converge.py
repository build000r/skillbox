"""Fleet converge: a per-repo heal *plan* for the whole skill estate.

Why this exists
===============

A codex session healed one drifted repo by hand in ~20 tool
calls; ~23 repos remain. Re-deriving "what is wrong here and what is the exact
command to fix it" once per repo does not scale and is not reviewable. This
module walks the deduped canonical fleet (the same candidate set
``collect_skill_audit`` scans) and emits ONE diffable document: every repo's
drift, grouped by triage class, where every action carries the EXACT
single-repo command an agent runs to apply it.

PLAN ONLY. This module reads the audit/visibility/MCP surfaces and renders a
plan. It NEVER writes, links, prunes, or migrates anything — preserving the
read-only-first ``sbp`` contract. The commands it emits are themselves
``--dry-run`` by default; an operator reviews the plan, then applies per-repo
or in bulk.

The five triage classes
=======================

Every per-repo action belongs to exactly one class:

* ``relink``      — a broken link whose same-named source still lives under a
                    current source root (taxonomy ``origin=moved``). One
                    ``ln -sfn`` repoints it. Also carries ``other-machine``
                    links (taxonomy ``origin=other-machine``): a foreign target
                    is a migration (drop the link), surfaced here as the
                    relink/migrate family so the cross-machine decision clusters
                    with its cousins.
* ``prune``       — a dangling broken link (taxonomy ``origin=dangling``) or an
                    unreadable one (``origin=unreadable``): dead weight to
                    remove (or, for unreadable, to inspect).
* ``sync``        — a skill the cwd policy expects but that is not currently
                    effective (``missing_for_cwd``). ``skill sync`` links it.
* ``policy``      — a scope violation: a skill installed where policy forbids
                    it. Carries the offending rule id so the operator can edit
                    the rule OR prune the install.
* ``mcp``         — Claude/Codex MCP parity drift: a server missing on one
                    surface, or unexplained drift. ``mcp sync`` re-renders both
                    surfaces from the single declaration.

Determinism
===========

The plan is built to be byte-stable across runs so it diffs cleanly:

* repos are visited in the candidate order ``_skill_audit_candidate_paths``
  already sorts (realpath-deduped, lexicographic);
* within a repo, actions are emitted class-by-class in a fixed class order, and
  within a class sorted by ``(skill_or_server, path)``;
* the taxonomy ``origin`` order inside relink/prune mirrors
  :data:`runtime_manager.skill_visibility.BROKEN_LINK_CLASSES`.

Performance
===========

Per-repo ``collect_skill_visibility`` + ``collect_mcp_audit`` are stat-bound.
On ~45 repos this stays well under the 60s budget; the per-repo work is
parallelized across a thread pool (``max_workers``) since each repo's collect
is independent and IO-bound.
"""

from __future__ import annotations

import os
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import skill_visibility as _sv
from .mcp_visibility import collect_mcp_audit


# --- fleet-scoped memoization -----------------------------------------------
#
# The broken-link taxonomy classifies EVERY broken link by calling
# ``_skill_source_options(model, name)``, which walks/parses every skill source
# root's SKILL.md corpus. Across a 100+ repo fleet with hundreds of broken
# links that is O(links x source_skills) YAML parsing — the whole runtime cost.
# But the source corpus is repo-independent (it depends only on the shared
# model), so for the duration of ONE plan build we memoize the three pure,
# repo-independent helpers the taxonomy leans on. This is a caller-side cache:
# it patches nothing about taxonomy *behavior* (same inputs -> same outputs),
# only avoids recomputing identical results. The originals are restored on exit
# so the module is unchanged outside the build.


@contextmanager
def _memoized_source_lookups() -> Iterator[None]:
    """Memoize the repo-independent source-corpus lookups for one plan build.

    Wraps ``_declared_skill_occurrences`` (keyed by model identity),
    ``_skill_source_candidates`` (keyed by root path), and
    ``_skill_source_options`` (keyed by ``(model, name, explicit_source)``) on
    the ``skill_visibility`` module. A lock guards the caches so the per-repo
    thread pool can share them safely. Originals are restored on exit.
    """
    orig_declared = _sv._declared_skill_occurrences
    orig_candidates = _sv._skill_source_candidates
    orig_options = _sv._skill_source_options

    declared_cache: dict[int, Any] = {}
    candidates_cache: dict[str, Any] = {}
    options_cache: dict[tuple[int, str, str | None], Any] = {}
    lock = threading.Lock()

    def declared(model: dict[str, Any]) -> Any:
        key = id(model)
        with lock:
            hit = declared_cache.get(key)
        if hit is not None:
            return hit
        value = orig_declared(model)
        with lock:
            declared_cache[key] = value
        return value

    def candidates(root: Path) -> Any:
        key = str(root)
        with lock:
            hit = candidates_cache.get(key)
        if hit is not None:
            return hit
        value = orig_candidates(root)
        with lock:
            candidates_cache[key] = value
        return value

    def options(model: dict[str, Any], skill_name: str, *, explicit_source: str | None = None) -> Any:
        # An explicit source is rare and cheap; only memoize the common
        # (model, name) path so we never cache a per-call explicit override.
        key = (id(model), skill_name, explicit_source)
        with lock:
            hit = options_cache.get(key)
        if hit is not None:
            return hit
        value = orig_options(model, skill_name, explicit_source=explicit_source)
        with lock:
            options_cache[key] = value
        return value

    _sv._declared_skill_occurrences = declared
    _sv._skill_source_candidates = candidates
    _sv._skill_source_options = options
    try:
        yield
    finally:
        _sv._declared_skill_occurrences = orig_declared
        _sv._skill_source_candidates = orig_candidates
        _sv._skill_source_options = orig_options


# Stable class order for the plan (and for the human table sections).
CONVERGE_CLASSES = ("relink", "prune", "sync", "policy", "mcp")

# Which taxonomy origins map into which heal class. ``moved`` and
# ``other-machine`` are both relink-family (repoint vs. migrate); ``dangling``
# and ``unreadable`` are both prune-family (remove vs. inspect).
_ORIGIN_TO_CLASS = {
    "moved": "relink",
    "other-machine": "relink",
    "dangling": "prune",
    "unreadable": "prune",
}


def _manage_py() -> str:
    """The entrypoint prefix every emitted command uses.

    Mirrors the existing audit ``next_actions`` style (``manage.py skill ...``)
    so a plan command pastes straight onto ``python3 .env-manager/manage.py``
    (or its ``sbp`` wrapper alias).
    """
    return "manage.py"


# --- per-class action builders ----------------------------------------------


def _relink_and_prune_actions(
    broken_links: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split classified broken-link rows into relink and prune actions.

    ``broken_links`` are the already-classified occurrence dicts from the
    visibility ``issues['broken_project']`` group (each carrying ``origin`` /
    ``suggested_action`` / ``fix_command`` from the landed taxonomy). We do not
    re-derive the heal command: the taxonomy already computed the single
    narrowest one per class, and re-deriving would risk drift from the audit.
    """
    relink: list[dict[str, Any]] = []
    prune: list[dict[str, Any]] = []
    for link in broken_links:
        origin = str(link.get("origin") or "dangling")
        heal_class = _ORIGIN_TO_CLASS.get(origin, "prune")
        action = {
            "class": heal_class,
            "skill": str(link.get("name") or ""),
            "origin": origin,
            "suggested_action": str(
                link.get("suggested_action")
                or _sv.BROKEN_LINK_ACTIONS.get(origin, "prune")
            ),
            "path": str(link.get("path") or ""),
            "link_target": str(
                link.get("link_target_abs") or link.get("link_target") or ""
            ),
            "command": str(link.get("fix_command") or ""),
        }
        (relink if heal_class == "relink" else prune).append(action)
    return relink, prune


def _sync_actions(
    missing_for_cwd: list[dict[str, Any]],
    repo_path: str,
) -> list[dict[str, Any]]:
    """One ``skill sync`` action per policy-expected-but-absent skill.

    The exact command names the skill explicitly so a bulk applier can run the
    minimal per-skill link rather than a whole-repo sync, and carries the rule
    id that made the skill expected here.
    """
    actions: list[dict[str, Any]] = []
    for item in missing_for_cwd:
        name = str(item.get("name") or "")
        if not name:
            continue
        rule = str(item.get("scope_rule") or "")
        actions.append(
            {
                "class": "sync",
                "skill": name,
                "scope_rule": rule,
                "scope_policy_path": str(item.get("scope_policy_path") or ""),
                "reason": str(item.get("reason") or ""),
                # Shell-quote the skill name and repo path: this is advertised as
                # the EXACT single-repo command, so a name/path with a space or
                # shell metachar must paste safely.
                "command": (
                    f"{_manage_py()} skill sync {shlex.quote(name)} "
                    f"--cwd {shlex.quote(repo_path)} --dry-run"
                ),
            }
        )
    return actions


def _policy_actions(
    scope_violations: list[dict[str, Any]],
    repo_path: str,
) -> list[dict[str, Any]]:
    """One policy-edit suggestion per scope violation, WITH the rule id.

    A violation is a skill installed where the matching scope rule forbids it.
    There are two valid heals and the operator picks one, so we surface BOTH:

      * ``command``      — prune the offending install (read-only dry-run), and
      * ``policy_edit``  — the rule id + policy file to widen the rule instead.

    Carrying the rule id is the whole point: it turns "this is wrong" into "edit
    rule X in file Y, or prune".
    """
    actions: list[dict[str, Any]] = []
    for item in scope_violations:
        name = str(item.get("name") or "")
        if not name:
            continue
        rule = str(item.get("scope_rule") or "")
        allowed_paths = [str(p) for p in (item.get("allowed_paths") or [])]
        actions.append(
            {
                "class": "policy",
                "skill": name,
                "scope_rule": rule,
                "scope_policy_path": str(item.get("scope_policy_path") or ""),
                "allowed_paths": allowed_paths,
                "reason": str(item.get("reason") or ""),
                "path": str(item.get("path") or ""),
                # Default heal: prune the install that violates policy. The repo
                # path is shell-quoted: this prune command is advertised as the
                # exact command to run, so a path with a space/metachar must not
                # mangle into an unsafe paste.
                "command": (
                    f"{_manage_py()} skill prune --cwd {shlex.quote(repo_path)} "
                    f"--from project --dry-run"
                ),
                # Alternative heal: edit the rule to permit this install.
                "policy_edit": (
                    f"edit rule {rule!r} in {item.get('scope_policy_path') or '<skill-scope.yaml>'} "
                    f"to allow {name!r} at {repo_path}"
                ),
            }
        )
    return actions


def _mcp_actions(
    mcp_payload: dict[str, Any],
    repo_path: str,
) -> list[dict[str, Any]]:
    """MCP parity-fix actions: missing servers and unexplained drift.

    Each surface's ``missing`` (declared/expected but absent) and ``unexpected``
    (present but undeclared) servers become one action; the exact heal is the
    single-source ``mcp sync`` (dry-run) that re-renders BOTH surfaces from the
    one declaration. Servers single-surface-but-declared are intentional and
    are NOT surfaced as drift.
    """
    actions: list[dict[str, Any]] = []
    surfaces = mcp_payload.get("surfaces") or {}
    # Shell-quote the repo path in the advertised exact ``mcp sync`` command.
    sync_command = f"{_manage_py()} mcp sync --cwd {shlex.quote(repo_path)} --dry-run"
    seen: set[tuple[str, str, str]] = set()
    for surface_name in ("claude", "codex"):
        surface = surfaces.get(surface_name) or {}
        for server in sorted(str(s) for s in (surface.get("missing") or [])):
            key = (surface_name, "missing", server)
            if key in seen:
                continue
            seen.add(key)
            actions.append(
                {
                    "class": "mcp",
                    "surface": surface_name,
                    "server": server,
                    "kind": "missing",
                    "reason": f"{server} declared but absent from {surface_name} config",
                    "command": sync_command,
                }
            )
        for server in sorted(str(s) for s in (surface.get("unexpected") or [])):
            key = (surface_name, "unexpected", server)
            if key in seen:
                continue
            seen.add(key)
            actions.append(
                {
                    "class": "mcp",
                    "surface": surface_name,
                    "server": server,
                    "kind": "unexpected",
                    "reason": f"{server} present on {surface_name} but undeclared (unexplained drift)",
                    "command": (
                        f"declare {server} as a kind:mcp service in workspace/runtime.yaml, "
                        f"then {sync_command}"
                    ),
                }
            )
    return actions


# --- per-repo plan ----------------------------------------------------------


def _stable_action_sort_key(action: dict[str, Any]) -> tuple[str, str, str]:
    """Deterministic in-class ordering: by skill/server, then path, then origin.

    Keeps repeated migrations (same origin) clustered and makes the rendered
    plan byte-stable run-to-run.
    """
    return (
        str(action.get("skill") or action.get("server") or ""),
        str(action.get("path") or ""),
        str(action.get("origin") or action.get("kind") or ""),
    )


def _build_repo_plan(
    model: dict[str, Any],
    candidate: dict[str, Any],
    *,
    include_mcp: bool,
    root_dir: Path | None,
    declared_servers: list[str] | None,
) -> dict[str, Any]:
    """Build the heal plan for one candidate repo.

    Returns a row with ``path``, ``state``, ``sources``, per-class ``actions``,
    and per-class counts. A missing/unreadable repo is reported with
    ``state=missing`` and no actions so the fleet total stays honest.
    """
    raw_path = str(candidate.get("path") or "")
    sources = sorted(str(s) for s in (candidate.get("sources") or []))
    path = Path(raw_path)
    row: dict[str, Any] = {
        "path": raw_path,
        "sources": sources,
    }
    if not path.is_dir():
        row["state"] = "missing"
        row["actions"] = {cls: [] for cls in CONVERGE_CLASSES}
        row["counts"] = {cls: 0 for cls in CONVERGE_CLASSES}
        row["total"] = 0
        return row

    visibility = _sv.collect_skill_visibility(
        model,
        cwd=raw_path,
        include_global=False,
        include_project=True,
        include_sources=False,
    )
    issues = visibility.get("issues") or {}

    relink, prune = _relink_and_prune_actions(issues.get("broken_project") or [])
    sync = _sync_actions(issues.get("missing_for_cwd") or [], raw_path)
    policy = _policy_actions(issues.get("scope_violations") or [], raw_path)

    mcp: list[dict[str, Any]] = []
    if include_mcp and root_dir is not None:
        try:
            mcp_payload = collect_mcp_audit(
                root_dir,
                model,
                cwd=raw_path,
                declared_servers=declared_servers,
            )
            mcp = _mcp_actions(mcp_payload, raw_path)
        except Exception:
            # MCP parity is best-effort: a repo with no/invalid MCP config must
            # not sink the whole fleet plan. Skip it rather than raise.
            mcp = []

    actions = {
        "relink": sorted(relink, key=_stable_action_sort_key),
        "prune": sorted(prune, key=_stable_action_sort_key),
        "sync": sorted(sync, key=_stable_action_sort_key),
        "policy": sorted(policy, key=_stable_action_sort_key),
        "mcp": sorted(mcp, key=_stable_action_sort_key),
    }
    counts = {cls: len(actions[cls]) for cls in CONVERGE_CLASSES}
    row["state"] = "ok"
    row["matched_scope_rules"] = [
        str(item.get("id") or "")
        for item in visibility.get("matched_scope_rules") or []
        if str(item.get("id") or "")
    ]
    row["categories"] = [
        str(item.get("id") or "")
        for item in visibility.get("matched_project_categories") or []
        if str(item.get("id") or "")
    ]
    row["actions"] = actions
    row["counts"] = counts
    row["total"] = sum(counts.values())
    return row


def _repo_has_plan(row: dict[str, Any]) -> bool:
    """A repo is in the default plan when it has at least one heal action.

    A ``missing`` candidate (a declared repo path that does not exist on this
    box) carries no action, so it is excluded from the default actionable plan
    and surfaced only under ``--all`` (its existence is still counted in the
    summary ``missing_repos`` so the registry-drift signal is never lost).
    """
    return int(row.get("total") or 0) > 0


# --- fleet plan -------------------------------------------------------------


def build_fleet_converge_plan(
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    scan_roots: list[str] | None = None,
    max_depth: int = 3,
    include_clean: bool = False,
    include_mcp: bool = True,
    root_dir: Path | None = None,
    declared_servers: list[str] | None = None,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Walk the deduped canonical fleet and build a per-repo heal plan.

    The fleet list is exactly ``collect_skill_audit``'s candidate set (deduped
    by realpath, lexicographically sorted) so this plan covers the same repos
    the audit reports drift for. Each repo's plan groups its drift into the five
    triage classes, every action carrying its exact single-repo command.

    PLAN ONLY: nothing here writes. The returned ``repos`` (sorted) and rolled-up
    ``summary`` form a stable, diffable document.
    """
    candidates = _sv._skill_audit_candidate_paths(
        model,
        scan_roots=scan_roots,
        max_depth=max(0, int(max_depth)),
    )

    def _plan_one(candidate: dict[str, Any]) -> dict[str, Any]:
        return _build_repo_plan(
            model,
            candidate,
            include_mcp=include_mcp,
            root_dir=root_dir,
            declared_servers=declared_servers,
        )

    # Per-repo collects are independent and IO-bound: parallelize to stay under
    # the runtime budget on ~45 repos. The memo context shares the repo-
    # independent source-corpus lookups across every repo (and every worker) so
    # the taxonomy does not re-parse the whole SKILL.md corpus per broken link.
    # Results are re-sorted by path afterward so the plan is deterministic
    # regardless of completion order.
    with _memoized_source_lookups():
        if candidates and max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                rows = list(pool.map(_plan_one, candidates))
        else:
            rows = [_plan_one(candidate) for candidate in candidates]

    rows.sort(key=lambda row: str(row.get("path") or ""))
    reported = [row for row in rows if include_clean or _repo_has_plan(row)]

    class_totals = {cls: 0 for cls in CONVERGE_CLASSES}
    missing_repos = 0
    repos_with_plan = 0
    for row in rows:
        if row.get("state") == "missing":
            missing_repos += 1
        if _repo_has_plan(row):
            repos_with_plan += 1
        for cls in CONVERGE_CLASSES:
            class_totals[cls] += int((row.get("counts") or {}).get(cls) or 0)

    scan_roots_used = [
        str(root)
        for root in (
            _sv._configured_skill_audit_scan_roots(model)
            if scan_roots is None
            else _sv._expand_skill_source_patterns(scan_roots)
        )
    ]

    return {
        "kind": "fleet-converge-plan",
        "dry_run": True,
        "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "scan_roots": scan_roots_used,
        "max_depth": max_depth,
        "classes": list(CONVERGE_CLASSES),
        "summary": {
            "candidate_repos": len(candidates),
            "reported_repos": len(reported),
            "repos_with_plan": repos_with_plan,
            "missing_repos": missing_repos,
            "actions_total": sum(class_totals.values()),
            "by_class": class_totals,
        },
        "repos": reported,
        "next_actions": _fleet_next_actions(reported, class_totals),
    }


def _fleet_next_actions(
    reported: list[dict[str, Any]],
    class_totals: dict[str, int],
) -> list[str]:
    """A short, ordered list of the highest-leverage bulk moves.

    Surfaces one representative command per non-empty class so an operator sees
    the shape of the work before reading every row. Read-only/dry-run only.
    """
    actions: list[str] = []
    for cls in CONVERGE_CLASSES:
        if class_totals.get(cls):
            sample = _first_action_of_class(reported, cls)
            if sample and sample.get("command"):
                actions.append(f"[{cls}] {sample['command']}")
    if not actions:
        actions.append("fleet is converged: no per-repo heal actions")
    return actions


def _first_action_of_class(
    reported: list[dict[str, Any]],
    cls: str,
) -> dict[str, Any] | None:
    for row in reported:
        class_actions = (row.get("actions") or {}).get(cls) or []
        if class_actions:
            return class_actions[0]
    return None


# --- text renderer ----------------------------------------------------------

_CLASS_LABELS = {
    "relink": "relink (repoint/migrate broken links)",
    "prune": "prune (remove dead/unreadable links)",
    "sync": "sync (link cwd-expected missing skills)",
    "policy": "policy (scope violations — edit rule or prune)",
    "mcp": "mcp (Claude/Codex parity)",
}


def fleet_converge_text_lines(
    plan: dict[str, Any],
    *,
    limit: int = 40,
) -> list[str]:
    """Render the plan as a stable human table (one section per repo).

    ``limit`` caps the number of repo sections shown (0 = unlimited). The full,
    unabbreviated plan is always available via ``--format json``.
    """
    lines: list[str] = []
    summary = plan.get("summary") or {}
    by_class = summary.get("by_class") or {}
    lines.append("fleet converge plan (DRY-RUN — plan only, nothing written)")
    lines.append(
        f"candidate_repos={summary.get('candidate_repos', 0)} "
        f"repos_with_plan={summary.get('repos_with_plan', 0)} "
        f"missing_repos={summary.get('missing_repos', 0)} "
        f"actions_total={summary.get('actions_total', 0)}"
    )
    class_bits = " ".join(
        f"{cls}={by_class.get(cls, 0)}" for cls in CONVERGE_CLASSES
    )
    lines.append(f"by_class: {class_bits}")
    lines.append("")

    repos = plan.get("repos") or []
    shown = repos if limit <= 0 else repos[:limit]
    for row in shown:
        path = str(row.get("path") or "")
        if row.get("state") == "missing":
            lines.append(f"## {path}  [MISSING — candidate path does not exist]")
            lines.append("")
            continue
        total = int(row.get("total") or 0)
        rules = ",".join(row.get("matched_scope_rules") or []) or "-"
        cats = ",".join(row.get("categories") or []) or "-"
        lines.append(f"## {path}  ({total} actions; rules={rules} categories={cats})")
        actions = row.get("actions") or {}
        for cls in CONVERGE_CLASSES:
            class_actions = actions.get(cls) or []
            if not class_actions:
                continue
            lines.append(f"  {_CLASS_LABELS.get(cls, cls)}:")
            for action in class_actions:
                label = _action_label(action)
                lines.append(f"    - {label}")
                command = str(action.get("command") or "")
                if command:
                    lines.append(f"        $ {command}")
                policy_edit = str(action.get("policy_edit") or "")
                if policy_edit:
                    lines.append(f"        or: {policy_edit}")
        lines.append("")

    if limit > 0 and len(repos) > limit:
        lines.append(f"... {len(repos) - limit} more repos (use --format json for all)")
        lines.append("")

    next_actions = plan.get("next_actions") or []
    if next_actions:
        lines.append("representative bulk moves:")
        for action in next_actions:
            lines.append(f"  {action}")
    return lines


def _action_label(action: dict[str, Any]) -> str:
    """A compact one-line description of a single action for the table."""
    cls = str(action.get("class") or "")
    if cls in {"relink", "prune"}:
        return (
            f"{action.get('skill') or '?'}  "
            f"[{action.get('origin') or '?'} -> {action.get('suggested_action') or '?'}]"
        )
    if cls == "sync":
        rule = action.get("scope_rule") or "-"
        return f"{action.get('skill') or '?'}  [missing for cwd; rule={rule}]"
    if cls == "policy":
        rule = action.get("scope_rule") or "-"
        return f"{action.get('skill') or '?'}  [scope violation; rule={rule}]"
    if cls == "mcp":
        return (
            f"{action.get('server') or '?'}  "
            f"[{action.get('surface') or '?'}:{action.get('kind') or '?'}]"
        )
    return str(action.get("skill") or action.get("server") or "?")
