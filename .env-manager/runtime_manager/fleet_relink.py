"""Fleet relink: a machine-migration bulk rewrite for installed skill links.

Why this exists
===============

A machine move is the single biggest drift generator in the skill estate. When
the operator's repos move from one box to another (e.g. a Mac laptop at
``/Users/b/repos`` to a devbox at ``/srv/skillbox/repos``), every installed
skill symlink that pointed at the *source* tree now points at a path that does
not exist on the new box. The broken-link taxonomy already names this:
``origin == "other-machine"`` — a link whose target lives under a root that
``machines.yaml`` maps to a *different* machine profile.

``fleet converge`` reports those links and (by class) suggests *migrate* (drop
the link). But dropping-then-resyncing is the slow path: after a clean machine
move the SAME repo simply lives under the new root, so the link can be
*repointed* in place — ``/Users/b/repos/x/skills/y`` becomes
``/srv/skillbox/repos/x/skills/y`` — IF that translated target exists and is a
valid skill dir. This module first-classes that operation so the next box
migration is a one-liner.

The per-link decision
=====================

For each *other-machine* installed link found across the fleet:

* **rewrite** — translate the link target from the source root to the
  destination root via :meth:`MachinesConfig.translate_path`. If the translated
  target EXISTS and is a valid skill dir (has ``SKILL.md``), repoint the link at
  it (``ln -sfn``). This is the migration heal.
* **reclassify** — otherwise (no source root match, no destination root, or the
  translated target is missing / not a skill dir) we do NOT guess. We leave the
  link untouched and hand it back as ``moved`` or ``dangling`` for ``fleet
  converge`` to triage. Relink never prunes and never drops.

Links that are already healthy are NEVER touched: only ``other-machine``
broken links are candidates. A healthy link (or a ``moved`` / ``dangling`` link
whose target is not foreign) is left exactly as-is.

Roots default from machines.yaml
================================

``--from-root`` / ``--to-root`` are optional. When omitted, the roots are
derived from the machine profiles:

* ``to-root``   = the CURRENT machine's canonical repo root, and
* ``from-root`` = every OTHER machine's repo roots.

So ``fleet relink`` with no roots means "relink everything foreign to *this*
box back onto this box's tree" — the exact shape of a just-completed migration.
An explicit ``--from-root`` / ``--to-root`` scopes the rewrite to one root pair
(useful for a partial-overlap or alias migration), translated by simple prefix
swap rather than the full machine table.

Dry-run is symmetric with apply
===============================

``build_relink_plan`` computes ONE plan. ``--dry-run`` (the DEFAULT) prints
that plan and writes nothing. ``--yes`` runs the SAME plan and applies the
``rewrite`` actions (and only those). The plan an apply executes is
byte-for-byte the plan a dry-run prints — the only difference is whether the
``ln -sfn`` runs. This module is import-safe and performs NO writes unless
``apply_relink_plan`` is called explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from . import machines as _machines
from . import skill_visibility as _sv


# Stable decision vocabulary. ``rewrite`` is the only action an apply executes.
RELINK_DECISIONS = ("rewrite", "reclassify")

# The reclassify reasons mirror the broken-link taxonomy origins a relink hands
# back to converge when it declines to rewrite.
RECLASSIFY_MOVED = "moved"
RECLASSIFY_DANGLING = "dangling"


# --- root resolution --------------------------------------------------------


def resolve_relink_roots(
    config: Any,
    machine_id: str | None,
    *,
    from_root: str | None = None,
    to_root: str | None = None,
) -> dict[str, Any]:
    """Resolve the ``(from_roots, to_root)`` pair the relink rewrites between.

    Two modes:

    * **explicit** — both ``from_root`` and ``to_root`` given: a single root
      pair, used for a literal prefix swap (partial-overlap / alias migration).
      ``machines.yaml`` is still consulted for alias canonicalization but the
      machine table is not needed.
    * **default** — roots omitted: derive from machine profiles. ``to_root`` is
      the CURRENT machine's canonical repo root; ``from_roots`` is every OTHER
      machine's declared repo roots. This is the "relink everything foreign to
      this box" default.

    Returns a dict with ``mode`` (``"explicit"`` | ``"default"``),
    ``from_roots`` (list, possibly several in default mode), ``to_root`` (str or
    None), and ``error`` (str or None when the roots could not be resolved).
    """
    if from_root and to_root:
        return {
            "mode": "explicit",
            "from_roots": [str(from_root)],
            "to_root": str(to_root),
            "error": None,
        }
    if bool(from_root) != bool(to_root):
        return {
            "mode": "explicit",
            "from_roots": [str(from_root)] if from_root else [],
            "to_root": str(to_root) if to_root else None,
            "error": "both --from-root and --to-root are required when either is given",
        }

    # Default mode: derive from machine profiles.
    if config is None or not machine_id:
        return {
            "mode": "default",
            "from_roots": [],
            "to_root": None,
            "error": (
                "cannot derive default relink roots: machines.yaml unavailable or "
                "current machine undetected (pass --from-root/--to-root explicitly)"
            ),
        }
    current = config.get(machine_id)
    if current is None:
        return {
            "mode": "default",
            "from_roots": [],
            "to_root": None,
            "error": f"current machine {machine_id!r} not declared in machines.yaml",
        }
    dest = current.canonical_repo_root
    from_roots: list[str] = []
    for other_id, profile in config.machines.items():
        if other_id == machine_id:
            continue
        for root in profile.repo_roots:
            if root not in from_roots:
                from_roots.append(root)
    error = None
    if dest is None:
        error = f"current machine {machine_id!r} declares no repo_roots"
    elif not from_roots:
        error = "no other-machine repo roots declared; nothing foreign to relink"
    return {
        "mode": "default",
        "from_roots": from_roots,
        "to_root": dest,
        "error": error,
    }


def _translate_target(
    target: str,
    roots: dict[str, Any],
    config: Any,
    machine_id: str | None,
) -> str | None:
    """Map a foreign link ``target`` onto the destination tree, or None.

    In **default** mode the machine table does the work: a foreign target under
    some other machine's repo root translates to the current machine's
    canonical root via :meth:`MachinesConfig.translate_path` (alias-aware,
    symmetric). In **explicit** mode we prefix-swap ``from_root`` -> ``to_root``
    directly (after alias canonicalization) so a partial-overlap or alias pair
    the machine table does not model still relinks.
    """
    canon = target
    if config is not None:
        try:
            canon = config.canonicalize_alias(target)
        except Exception:
            canon = target

    if roots.get("mode") == "default" and config is not None and machine_id:
        # Try translating from each other machine into the current one.
        for other_id, profile in config.machines.items():
            if other_id == machine_id:
                continue
            try:
                translated = config.translate_path(canon, other_id, machine_id, category="repos")
            except Exception:
                translated = None
            if translated:
                return translated
        return None

    # Explicit mode: literal prefix swap on the single declared pair.
    from_roots = roots.get("from_roots") or []
    to_root = roots.get("to_root")
    if not to_root:
        return None
    for from_root in from_roots:
        remainder = _relative_under(from_root, canon, config)
        if remainder is None:
            continue
        return _join_under(to_root, remainder)
    return None


def _relative_under(root: str, candidate: str, config: Any) -> str | None:
    """Remainder of ``candidate`` under ``root`` (alias-aware), or None.

    Reuses the machines module's pure POSIX path helpers so explicit-mode prefix
    matching has identical semantics (segment boundaries, ``~`` expansion, alias
    folding) to the default-mode machine translation.
    """
    root_n = _machines._expand(str(root))
    cand_n = _machines._normalize(candidate)
    return _machines._is_under(root_n, cand_n)


def _join_under(root: str, remainder: str) -> str:
    return _machines._join_under(str(root), remainder)


# --- per-link decision ------------------------------------------------------


def decide_link(
    link: dict[str, Any],
    roots: dict[str, Any],
    config: Any,
    machine_id: str | None,
) -> dict[str, Any]:
    """Decide rewrite-or-reclassify for ONE other-machine broken link.

    ``link`` is a classified ``broken_project`` occurrence dict (carrying
    ``name`` / ``path`` / ``link_target`` / ``link_target_abs`` / ``origin``).
    Only ``origin == "other-machine"`` links reach a rewrite decision; the
    caller is responsible for filtering, but we re-check defensively.

    Returns an action dict with ``decision`` (``rewrite`` | ``reclassify``),
    the link identity, the resolved ``translated_target`` (when any), the
    ``command`` an apply runs, and — for reclassify — the ``reclassify_as``
    origin handed back to converge.
    """
    name = str(link.get("name") or "")
    path = str(link.get("path") or "")
    target = str(link.get("link_target_abs") or link.get("link_target") or "")
    origin = str(link.get("origin") or "")

    base: dict[str, Any] = {
        "skill": name,
        "path": path,
        "link_target": target,
        "origin": origin,
    }

    translated = _translate_target(target, roots, config, machine_id)
    if translated and _sv._path_is_skill_dir(Path(translated)):
        base.update(
            {
                "decision": "rewrite",
                "translated_target": translated,
                "command": f"ln -sfn {translated} {path}",
            }
        )
        return base

    # Reclassify: never guess, never prune. Hand the link back to converge.
    # If a same-named live source exists under a current root the link is a
    # local "moved" relink converge can repoint; otherwise it is dangling.
    reclassify_as = RECLASSIFY_DANGLING
    if translated:
        # Translated target resolved but is not a valid skill dir on this box:
        # the repo moved but the skill is gone -> dangling, leave for converge.
        reclassify_as = RECLASSIFY_DANGLING
    base.update(
        {
            "decision": "reclassify",
            "translated_target": translated or "",
            "reclassify_as": reclassify_as,
            "reason": (
                "no destination root match"
                if not translated
                else "translated target is missing or not a valid skill dir"
            ),
            "command": "",
        }
    )
    return base


# --- per-repo + fleet plan --------------------------------------------------


def _other_machine_links(visibility: dict[str, Any]) -> list[dict[str, Any]]:
    """Other-machine broken installed links from a visibility payload.

    The taxonomy enrichment already ran inside ``collect_skill_visibility`` so
    each ``broken_project`` occurrence carries ``origin``. We filter to the
    ``other-machine`` class — the only links a machine-migration relink owns.
    Healthy links never appear in ``broken_project`` at all, so they are
    structurally untouchable here.
    """
    issues = visibility.get("issues") or {}
    broken = issues.get("broken_project") or []
    return [
        link for link in broken if str(link.get("origin") or "") == "other-machine"
    ]


def _build_repo_relink(
    model: dict[str, Any],
    repo_path: str,
    roots: dict[str, Any],
    config: Any,
    machine_id: str | None,
) -> dict[str, Any]:
    """Build the relink plan for one repo (missing repos report no actions)."""
    row: dict[str, Any] = {"path": repo_path}
    path = Path(repo_path)
    if not path.is_dir():
        row["state"] = "missing"
        row["actions"] = []
        row["counts"] = {decision: 0 for decision in RELINK_DECISIONS}
        row["total"] = 0
        return row

    visibility = _sv.collect_skill_visibility(
        model,
        cwd=repo_path,
        include_global=False,
        include_project=True,
        include_sources=False,
    )
    actions = [
        decide_link(link, roots, config, machine_id)
        for link in _other_machine_links(visibility)
    ]
    actions.sort(key=lambda action: (str(action.get("skill") or ""), str(action.get("path") or "")))
    counts = {decision: 0 for decision in RELINK_DECISIONS}
    for action in actions:
        decision = str(action.get("decision") or "")
        if decision in counts:
            counts[decision] += 1
    row["state"] = "ok"
    row["actions"] = actions
    row["counts"] = counts
    row["total"] = len(actions)
    return row


def build_relink_plan(
    model: dict[str, Any],
    *,
    from_root: str | None = None,
    to_root: str | None = None,
    cwd: str | None = None,
    scan_roots: list[str] | None = None,
    max_depth: int = 3,
    include_clean: bool = False,
    config: Any = None,
    machine_id: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Build the machine-migration relink plan over the fleet (or one repo).

    The plan is identical whether ``apply`` is True or False — that flag only
    annotates the returned ``dry_run`` field so a caller can prove dry-run and
    apply compute the SAME plan. Applying is done separately via
    :func:`apply_relink_plan`, which executes only the ``rewrite`` actions this
    plan already enumerates.

    When ``cwd`` names a single repo dir (and no explicit ``scan_roots``) the
    plan scopes to that one repo; otherwise it walks the deduped canonical fleet
    (the same candidate set ``collect_skill_audit`` scans).
    """
    if config is None or machine_id is None:
        resolved_config, resolved_machine = _resolve_machines()
        config = config if config is not None else resolved_config
        machine_id = machine_id if machine_id is not None else resolved_machine

    roots = resolve_relink_roots(
        config, machine_id, from_root=from_root, to_root=to_root
    )

    repo_paths = _relink_repo_paths(
        model, cwd=cwd, scan_roots=scan_roots, max_depth=max_depth
    )

    rows = [
        _build_repo_relink(model, repo_path, roots, config, machine_id)
        for repo_path in repo_paths
    ]
    rows.sort(key=lambda row: str(row.get("path") or ""))
    reported = [row for row in rows if include_clean or int(row.get("total") or 0) > 0]

    totals = {decision: 0 for decision in RELINK_DECISIONS}
    missing_repos = 0
    repos_with_plan = 0
    for row in rows:
        if row.get("state") == "missing":
            missing_repos += 1
        if int(row.get("total") or 0) > 0:
            repos_with_plan += 1
        for decision in RELINK_DECISIONS:
            totals[decision] += int((row.get("counts") or {}).get(decision) or 0)

    return {
        "kind": "fleet-relink-plan",
        "dry_run": not apply,
        "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "roots": {
            "mode": roots.get("mode"),
            "from_roots": roots.get("from_roots") or [],
            "to_root": roots.get("to_root"),
            "error": roots.get("error"),
        },
        "machine_id": machine_id,
        "decisions": list(RELINK_DECISIONS),
        "summary": {
            "candidate_repos": len(repo_paths),
            "reported_repos": len(reported),
            "repos_with_plan": repos_with_plan,
            "missing_repos": missing_repos,
            "rewrite": totals["rewrite"],
            "reclassify": totals["reclassify"],
            "actions_total": sum(totals.values()),
        },
        "repos": reported,
        "next_actions": _relink_next_actions(roots, totals),
    }


def _resolve_machines() -> tuple[Any, str | None]:
    """Best-effort ``(MachinesConfig, current_machine_id)`` for the live box.

    Mirrors ``skill_visibility._machines_classifier`` so relink and the
    taxonomy agree on machine identity. Degrades to ``(None, None)`` rather than
    raising, so an explicit ``--from-root``/``--to-root`` still works on a
    profile-less box.
    """
    try:
        config = _machines.load_machines_config()
    except Exception:
        return None, None
    try:
        machine_id = config.detect_machine_id()
    except Exception:
        machine_id = None
    return config, machine_id


def _relink_repo_paths(
    model: dict[str, Any],
    *,
    cwd: str | None,
    scan_roots: list[str] | None,
    max_depth: int,
) -> list[str]:
    """The repo set the relink plan covers.

    When ``cwd`` names a single git repo (a dir carrying ``.git``) and no
    explicit ``scan_roots`` were given, scope to that one repo — this is the
    ``--cwd scopes to one repo`` contract. Requiring ``.git`` (not merely a dir)
    means the wrapper auto-injecting ``--cwd`` for a non-repo anchor (e.g. the
    skillbox repo root invocation) still walks the FULL fleet rather than
    silently collapsing to a single arbitrary directory. Otherwise enumerate the
    deduped canonical fleet via the audit's candidate walk so relink covers
    exactly the repos converge does.
    """
    if cwd and scan_roots is None:
        cwd_path = Path(cwd)
        if cwd_path.is_dir() and (cwd_path / ".git").exists():
            return [str(cwd_path)]

    candidates = _sv._skill_audit_candidate_paths(
        model,
        scan_roots=scan_roots,
        max_depth=max(0, int(max_depth)),
    )
    return [str(candidate.get("path") or "") for candidate in candidates if candidate.get("path")]


def _relink_next_actions(
    roots: dict[str, Any],
    totals: dict[str, int],
) -> list[str]:
    """A short ordered list of the highest-leverage relink moves."""
    actions: list[str] = []
    if roots.get("error"):
        actions.append(f"blocked: {roots['error']}")
        return actions
    if totals.get("rewrite"):
        from_label = ",".join(roots.get("from_roots") or []) or "<other-machine roots>"
        to_label = roots.get("to_root") or "<this machine>"
        actions.append(
            f"apply {totals['rewrite']} rewrite(s): "
            f"manage.py fleet relink --from-root {from_label} --to-root {to_label} --yes"
        )
    if totals.get("reclassify"):
        actions.append(
            f"{totals['reclassify']} link(s) reclassified for converge "
            f"(run: manage.py fleet converge --dry-run)"
        )
    if not actions:
        actions.append("no other-machine links to relink: fleet already migrated")
    return actions


# --- apply ------------------------------------------------------------------


def apply_relink_plan(
    plan: dict[str, Any],
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Execute the ``rewrite`` actions of a relink plan (and ONLY those).

    The plan passed in is exactly the one ``build_relink_plan`` produced, so an
    apply runs the same decisions a dry-run printed. ``dry_run=True`` (the
    default) is a no-op that reports what WOULD be written; ``dry_run=False``
    repoints each rewrite link with an atomic ``os.symlink`` replace
    (``ln -sfn`` semantics). Reclassify actions are never executed: they are
    left for ``fleet converge``.

    Returns a result dict with per-action ``applied`` / ``error`` and a rolled-up
    summary. Refuses to apply when the plan's root resolution carries an error.
    """
    roots = plan.get("roots") or {}
    results: list[dict[str, Any]] = []
    rewritten = 0
    failed = 0
    skipped_reclassify = 0

    root_error = roots.get("error")
    for row in plan.get("repos") or []:
        for action in row.get("actions") or []:
            decision = str(action.get("decision") or "")
            if decision != "rewrite":
                skipped_reclassify += 1
                continue
            entry = {
                "skill": action.get("skill"),
                "path": action.get("path"),
                "translated_target": action.get("translated_target"),
                "applied": False,
                "error": None,
            }
            if root_error:
                entry["error"] = f"root resolution error: {root_error}"
                failed += 1
                results.append(entry)
                continue
            if dry_run:
                results.append(entry)
                continue
            try:
                _repoint_symlink(str(action.get("path") or ""), str(action.get("translated_target") or ""))
                entry["applied"] = True
                rewritten += 1
            except Exception as exc:  # pragma: no cover - exercised via tmp-tree tests
                entry["error"] = str(exc)
                failed += 1
            results.append(entry)

    return {
        "kind": "fleet-relink-apply",
        "dry_run": dry_run,
        "summary": {
            "rewritten": rewritten,
            "failed": failed,
            "skipped_reclassify": skipped_reclassify,
            "planned_rewrites": int((plan.get("summary") or {}).get("rewrite") or 0),
        },
        "results": results,
    }


def _repoint_symlink(link_path: str, target: str) -> None:
    """Atomically repoint ``link_path`` at ``target`` (``ln -sfn`` semantics).

    Writes the new link beside the old one and ``os.replace``s it into place so a
    crash never leaves the install link absent. Only ever called by an explicit
    non-dry-run apply.
    """
    if not link_path or not target:
        raise ValueError("relink requires both a link path and a target")
    link = Path(link_path)
    tmp = link.parent / (link.name + ".relink-tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, str(tmp))
    os.replace(str(tmp), str(link))


# --- text renderer ----------------------------------------------------------


def relink_text_lines(plan: dict[str, Any], *, limit: int = 40) -> list[str]:
    """Render the relink plan as a stable human table.

    ``limit`` caps the repo sections shown (0 = unlimited). The full plan is
    always available via ``--format json``.
    """
    lines: list[str] = []
    summary = plan.get("summary") or {}
    roots = plan.get("roots") or {}
    mode = "APPLY" if not plan.get("dry_run") else "DRY-RUN"
    lines.append(f"fleet relink plan ({mode} — machine-migration link rewrite)")
    if roots.get("error"):
        lines.append(f"  roots: BLOCKED — {roots['error']}")
    else:
        from_label = ",".join(roots.get("from_roots") or []) or "-"
        lines.append(
            f"  roots: {from_label} -> {roots.get('to_root') or '-'} "
            f"(mode={roots.get('mode') or '-'}, machine={plan.get('machine_id') or '-'})"
        )
    lines.append(
        f"candidate_repos={summary.get('candidate_repos', 0)} "
        f"repos_with_plan={summary.get('repos_with_plan', 0)} "
        f"missing_repos={summary.get('missing_repos', 0)} "
        f"rewrite={summary.get('rewrite', 0)} reclassify={summary.get('reclassify', 0)}"
    )
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
        lines.append(f"## {path}  ({total} relink actions)")
        for action in row.get("actions") or []:
            decision = str(action.get("decision") or "")
            skill = action.get("skill") or "?"
            if decision == "rewrite":
                lines.append(
                    f"  rewrite  {skill}: {action.get('link_target')} -> {action.get('translated_target')}"
                )
                command = str(action.get("command") or "")
                if command:
                    lines.append(f"      $ {command}")
            else:
                lines.append(
                    f"  reclassify {skill}: -> {action.get('reclassify_as')} "
                    f"({action.get('reason')}) [left for converge]"
                )
        lines.append("")

    if limit > 0 and len(repos) > limit:
        lines.append(f"... {len(repos) - limit} more repos (use --format json for all)")
        lines.append("")

    next_actions = plan.get("next_actions") or []
    if next_actions:
        lines.append("next moves:")
        for action in next_actions:
            lines.append(f"  {action}")
    return lines
