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
import shlex
from pathlib import Path
from typing import Any, Iterable

from . import machines as _machines
from . import skill_visibility as _sv


# Stable decision vocabulary. ``rewrite`` is the only action an apply executes.
RELINK_DECISIONS = ("rewrite", "reclassify")

# The reclassify origin a relink hands back to converge when it declines to
# rewrite. Relink itself never re-derives moved-vs-dangling (it has no
# source-corpus lookup), so it always reclassifies as ``dangling`` and lets
# converge's own taxonomy re-classify the link (moved/dangling/etc.) from the
# full source corpus. There is intentionally no ``RECLASSIFY_MOVED``: relink
# cannot distinguish a moved link, so claiming it could would be a lie in the
# plan text.
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
        # Try translating from each other machine into the current one. A target
        # can be under several machines' roots when those roots prefix-overlap
        # (e.g. /Users/b and /Users/b/repos belong to different profiles); the
        # first machine in machines.yaml order is NOT necessarily the correct
        # owner. Collect EVERY candidate and pick the translation whose matched
        # SOURCE root is the longest (most specific), with a deterministic
        # tiebreak (translated target, then machine id) on equal length so the
        # same link always relinks to the same target run-to-run.
        best: tuple[int, str, str] | None = None  # (-match_len, translated, other_id)
        for other_id, profile in config.machines.items():
            if other_id == machine_id:
                continue
            try:
                translated = config.translate_path(canon, other_id, machine_id, category="repos")
            except Exception:
                translated = None
            if not translated:
                continue
            match_len = _matched_source_root_len(canon, profile)
            candidate = (-match_len, str(translated), str(other_id))
            if best is None or candidate < best:
                best = candidate
        return best[1] if best is not None else None

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


def _matched_source_root_len(canon: str, profile: Any) -> int:
    """Length of the longest of ``profile``'s repo roots that ``canon`` is under.

    Drives the default-mode longest-match tiebreak in :func:`_translate_target`:
    when a foreign target is under several machines' (prefix-overlapping) repo
    roots, the machine whose matched SOURCE root is the most specific (longest)
    is the correct owner. Returns 0 when ``canon`` is under none of the roots
    (the caller only consults this for machines that already translated, so a
    match always exists, but we degrade to 0 rather than raise).
    """
    best = 0
    for root in getattr(profile, "repo_roots", ()) or ():
        expanded = _machines._expand(str(root))
        if _machines._is_under(expanded, _machines._normalize(canon)) is not None:
            best = max(best, len(expanded))
    return best


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
    CALLER (:func:`_other_machine_links`) is responsible for that filtering, and
    this function does NOT re-check the origin here (it trusts the filtered
    input). The load-bearing safety re-check happens at APPLY time, not build
    time: :func:`_repoint_symlink` re-verifies the link is still broken and the
    translated target is still a valid skill dir before it rewrites anything, so
    a stale plan never clobbers a now-real link or a now-deleted target.

    Returns an action dict with ``decision`` (``rewrite`` | ``reclassify``),
    the link identity, the resolved ``translated_target`` (when any), the
    ``command`` an apply runs, and — for reclassify — the ``reclassify_as``
    origin handed back to converge. Relink always reclassifies as ``dangling``
    (it has no source-corpus lookup to detect ``moved``); converge re-derives the
    real taxonomy from the full corpus, so this is plan-text only.
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
                "command": f"ln -sfn {shlex.quote(translated)} {shlex.quote(path)}",
            }
        )
        return base

    # Reclassify: never guess, never prune. Hand the link back to converge,
    # ALWAYS as ``dangling`` — relink has no source-corpus lookup, so it cannot
    # tell a genuinely-moved link (a same-named live source still exists) from a
    # dead one. Converge re-derives the real moved-vs-dangling taxonomy from the
    # full source corpus; this ``reclassify_as`` is plan text only, so emitting
    # the honest ``dangling`` rather than a guessed ``moved`` keeps it truthful.
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


class RelinkSkip(Exception):
    """A rewrite was declined at APPLY time because the plan went stale.

    Raised by :func:`_repoint_symlink` when an apply-time re-validation finds the
    link is no longer a broken ``other-machine`` symlink, or the translated
    target is no longer a valid skill dir. Carries a human reason; the apply loop
    records it as ``skipped_stale`` (NOT ``failed`` — the on-disk state is fine,
    we simply declined to touch it).
    """


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

    A relink plan can be emitted as JSON and applied LATER, so at apply time the
    plan may be stale: a link that was a broken foreign symlink at build time may
    now be a real directory, and a translated target that existed may now be
    deleted. Build-time guarantees ("only touch broken links / target must
    exist+valid") do NOT survive serialization, so :func:`_repoint_symlink`
    RE-VERIFIES both invariants immediately before each rewrite and refuses
    (records ``skipped_stale``) rather than clobber a now-real link or point at a
    now-missing target.

    Returns a result dict with per-action ``applied`` / ``error`` /
    ``skipped`` and a rolled-up summary. Refuses to apply when the plan's root
    resolution carries an error.
    """
    roots = plan.get("roots") or {}
    results: list[dict[str, Any]] = []
    rewritten = 0
    failed = 0
    skipped_reclassify = 0
    skipped_stale = 0

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
                "skipped": False,
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
            except RelinkSkip as skip:
                # Stale plan: the link is no longer broken-foreign, or the target
                # vanished. Leave the on-disk state alone and record the skip.
                entry["skipped"] = True
                entry["error"] = f"skipped (stale plan): {skip}"
                skipped_stale += 1
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
            "skipped_stale": skipped_stale,
            "planned_rewrites": int((plan.get("summary") or {}).get("rewrite") or 0),
        },
        "results": results,
    }


def _stale_relink_reason(link: Path, target: str) -> str | None:
    """Apply-time re-validation: why this rewrite must be SKIPPED, or None.

    A relink plan can be applied long after it was built, so the two build-time
    guarantees must be re-checked against the live filesystem before any rewrite:

    * ``link`` must STILL be a symlink whose target does NOT exist — i.e. still a
      broken link. If it is now a real directory (or a healthy link, or absent)
      we must not clobber it.
    * ``target`` must STILL be a valid skill dir (``SKILL.md`` present). If the
      translated target was deleted since the plan was built, repointing at it
      would just manufacture a new broken link.

    Returns a short reason string when the rewrite is unsafe, or None when both
    invariants still hold and the rewrite may proceed.
    """
    if not link.is_symlink():
        # Not a symlink anymore: a real dir/file now lives here (or it is gone).
        # Never overwrite a materialized path with our link.
        return "link is no longer a symlink (now a real path or removed)"
    # ``os.path.exists`` follows the symlink: True means the target resolves, so
    # the link is no longer broken and must be left alone.
    if os.path.exists(str(link)):
        return "link is no longer broken (its target now exists)"
    if not _sv._path_is_skill_dir(Path(target)):
        return "translated target is missing or no longer a valid skill dir"
    return None


def _repoint_symlink(link_path: str, target: str) -> None:
    """Atomically repoint ``link_path`` at ``target`` (``ln -sfn`` semantics).

    Re-validates the plan against the live filesystem first (see
    :func:`_stale_relink_reason`) and raises :class:`RelinkSkip` rather than
    rewrite a now-real link or point at a now-deleted target. When the
    re-validation passes, it writes the new link beside the old one and
    ``os.replace``s it into place so a crash never leaves the install link
    absent. The temp ``<name>.relink-tmp`` symlink is ALWAYS cleaned up — even
    when ``os.replace`` raises (e.g. the path is now a real directory) — so a
    failed/stale apply never leaves a stray ``.relink-tmp`` link that the next
    inventory scan would mistake for an installed skill. Only ever called by an
    explicit non-dry-run apply.
    """
    if not link_path or not target:
        raise ValueError("relink requires both a link path and a target")
    link = Path(link_path)

    stale = _stale_relink_reason(link, target)
    if stale is not None:
        raise RelinkSkip(stale)

    tmp = link.parent / (link.name + ".relink-tmp")
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(target, str(tmp))
        os.replace(str(tmp), str(link))
    except BaseException:
        # On ANY failure (e.g. ``link`` is now a real directory -> os.replace
        # raises IsADirectoryError) leave nothing behind: a leftover
        # ``<name>.relink-tmp`` symlink does not start with '.' and would be
        # scanned as an installed skill, permanently polluting the inventory.
        tmp.unlink(missing_ok=True)
        raise


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
