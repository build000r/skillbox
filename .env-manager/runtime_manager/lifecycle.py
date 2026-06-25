"""Skill-visibility lifecycle.

Single responsibility: building and applying link/unlink/sync/activate plans and
overlay activation -- i.e. *mutating on-disk skill state* to match policy. Sits
at the top of the layering: depends on ._skill_common, .policy_eval, .inventory,
and .audit_report (it consumes ``collect_skill_visibility`` to plan against the
current visibility snapshot).
"""

from __future__ import annotations

import fnmatch
import glob
import hashlib
import os
import shutil
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

from .shared import (
    GLOBAL_HOME_ROOT_ENV,
    GLOBAL_HOME_SURFACES,
    atomic_write_text,
    directory_tree_sha256,
    load_json_file,
    load_yaml,
    load_skill_repos_config,
)
from .errors import PRUNE_SKIPPED_PINNED

from ._skill_common import *
from .policy_eval import *
from .inventory import *
from .audit_report import *

__all__ = [
    'unlink_overlay_scoped_skills',
    '_activations_from_sync_plan',
    'activate_overlay_scoped_skills',
    '_skill_destination_bases',
    '_skill_destinations_for_bases',
    '_install_path_state',
    '_link_skill_action',
    '_installed_occurrences_for_skill',
    '_scope_filter_matches',
    '_unlink_skill_action',
    '_dedupe_actions',
    '_activation_packet',
    '_require_lifecycle_skill_name',
    '_skill_blocked_reason',
    '_skill_link_actions_for_bases',
    '_plan_primary_skill_links',
    '_planned_link_destinations',
    '_plan_skill_removals',
    '_lifecycle_visibility',
    '_plan_skill_prune_actions',
    '_sync_wanted_skill_names',
    '_plan_one_skill_sync',
    '_plan_skill_sync_actions',
    '_lifecycle_needs_visibility',
    '_append_lifecycle_prune_actions',
    '_append_lifecycle_sync_actions',
    '_lifecycle_activation_packet_if_needed',
    '_lifecycle_plan_summary',
    'skill_lifecycle_plan',
    '_apply_lifecycle_link',
    '_prepare_lifecycle_link_destination',
    '_apply_lifecycle_unlink',
    '_apply_lifecycle_action',
    '_summarize_applied_lifecycle_plan',
    'apply_skill_lifecycle_plan',
    'print_skill_lifecycle_text',
]


def unlink_overlay_scoped_skills(
    model: dict[str, Any],
    overlay_name: str,
    cwd: Path | str,
    *,
    scope: str = "all",
) -> list[str]:
    """Remove symlinks for overlay-scoped skills from selected agent surfaces.

    Never touches real directories — only symlinks — so an accidental call
    cannot delete skill sources. scope controls the blast radius:
    project = cwd-local .claude/.codex, global = operator homes, all = both.
    """
    skill_names = overlay_scoped_skill_names(model, overlay_name)
    if not skill_names:
        return []
    if scope not in {"project", "global", "all"}:
        raise RuntimeError("overlay unlink scope must be one of: project, global, all")
    targets: list[Path] = []
    if scope in {"project", "all"}:
        targets.extend([
            Path(cwd) / ".claude" / "skills",
            Path(cwd) / ".codex" / "skills",
        ])
    if scope in {"global", "all"}:
        # Route through the canonical global-home resolution so the managed
        # home (SKILLBOX_HOME_ROOT) is unlinked alongside the OS home; roots
        # that realpath-collapse to one surface are visited once.
        targets.extend(root for _surface, root in _default_global_roots())
    removed: list[str] = []
    for target_dir in targets:
        if not target_dir.is_dir():
            continue
        for name in skill_names:
            link_path = target_dir / name
            if link_path.is_symlink():
                try:
                    link_path.unlink()
                    removed.append(str(link_path))
                except OSError:
                    pass
    return sorted(removed)


def _activations_from_sync_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Group a sync plan's link actions per skill and build activation packets.

    The plan (built by ``skill_lifecycle_plan(model, "sync", ...)``) is the
    contract: whatever it plans to link is exactly what activate reports. We
    derive one activation entry per linked skill so the requesting agent can
    use the SKILL.md content immediately, while the on-disk symlinks make the
    skill visible to future sessions. Because dry-run and apply build the same
    plan, the activation list is identical in both modes — zero surprises.
    """
    by_skill: dict[str, list[dict[str, Any]]] = {}
    for action in plan.get("actions") or []:
        if action.get("op") != "link":
            continue
        skill_name = str(action.get("skill") or "")
        if not skill_name:
            continue
        by_skill.setdefault(skill_name, []).append(action)

    activations: list[dict[str, Any]] = []
    for skill_name in sorted(by_skill):
        actions = by_skill[skill_name]
        source = str(actions[0].get("source") or "")
        selected_source = {
            "source": source,
            "source_bucket": actions[0].get("source_bucket"),
        }
        packet, packet_warning = _activation_packet(skill_name, selected_source, actions)
        activations.append({
            "skill": skill_name,
            "summary": {
                "actions": len(actions),
                "link": sum(1 for item in actions if item.get("op") == "link"),
                "blocked": sum(1 for item in actions if item.get("blocked_reason")),
            },
            "warnings": [packet_warning] if packet_warning else [],
            "actions": actions,
            "activation_packet": packet,
        })
    return activations


def activate_overlay_scoped_skills(
    model: dict[str, Any],
    overlay_name: str,
    cwd: Path | str,
    *,
    to: str = "project",
    categories: list[str] | None = None,
    source: str | None = None,
    dry_run: bool = False,
    allow_directories: bool = False,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Policy-evaluate one overlay for THIS invocation, scoped to ``cwd``.

    This is equivalent to ``SKILLBOX_CLI_OVERLAYS=<overlay_name> skill sync``
    narrowed to ``cwd`` — it runs the SAME policy evaluation as ``skill sync``
    rather than blindly linking every literal overlay-tagged skill. The named
    overlay is treated as active only for the duration of this call (the
    ``SKILLBOX_CLI_OVERLAYS`` env var that ``active_overlays`` reads is patched and
    restored), so NO overlay state is persisted.

    The sync plan it builds is the contract: ``--dry-run`` previews exactly the
    set ``apply`` would link, so activating an overlay in a cwd that the policy
    does not match links the policy-correct set (often zero), never all of the
    overlay's literal skills.
    """
    target = (overlay_name or "").strip()
    if not target:
        return []

    previous = os.environ.get(OVERLAY_CLI_ENV_VAR)
    forced = [item for item in (previous or "").split(",") if item.strip()]
    if target not in forced:
        forced.append(target)
    os.environ[OVERLAY_CLI_ENV_VAR] = ",".join(forced)
    try:
        plan = skill_lifecycle_plan(
            model,
            "sync",
            skill_name=None,
            cwd=str(cwd),
            to=to,
            categories=categories or [],
            source=source,
            force=force,
        )
        plan = apply_skill_lifecycle_plan(
            plan,
            dry_run=dry_run,
            allow_directories=allow_directories,
            force=force,
        )
    finally:
        if previous is None:
            os.environ.pop(OVERLAY_CLI_ENV_VAR, None)
        else:
            os.environ[OVERLAY_CLI_ENV_VAR] = previous

    return _activations_from_sync_plan(plan)


def _skill_destination_bases(
    model: dict[str, Any],
    skill_name: str,
    *,
    cwd: Path,
    to: str,
    categories: list[str],
) -> tuple[str, list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    requested = to
    if requested == "auto":
        if categories:
            requested = "category"
        elif _global_install_allowed(model, skill_name):
            requested = "global"
        else:
            requested = "project"

    if requested == "global":
        return requested, [{"scope": "global", "path": None, "category": None}], warnings

    if requested == "category":
        category_ids = categories
        if not category_ids:
            rule = _matching_scope_rule(skill_name, _scope_rules(model, cwd=cwd), cwd=cwd)
            category_ids = list(rule.get("categories") or []) if rule else []
        if not category_ids:
            warnings.append("No project category was supplied or inferred; falling back to the current repo.")
            return "project", [{"scope": "project", "path": str(_repo_root_for_skill_install(cwd)), "category": None}], warnings

        bases: list[dict[str, Any]] = []
        for category_id in category_ids:
            category = _category_by_id(model, category_id)
            if not category:
                warnings.append(f"Unknown project category: {category_id}")
                continue
            for raw_path in category.get("paths") or []:
                bases.append({
                    "scope": "project",
                    "path": str(raw_path),
                    "category": category_id,
                })
        return requested, bases, warnings

    rule = _matching_scope_rule(skill_name, _scope_rules(model, cwd=cwd), cwd=cwd)
    matched_paths: list[str] = []
    if rule:
        matched_paths = [
            path for path in rule.get("paths") or []
            if _path_prefix_matches(cwd, path)
        ]
    if matched_paths:
        repo_root = _repo_root_for_skill_install(cwd)
        if _path_is_under(str(repo_root), matched_paths):
            base = str(repo_root)
        else:
            base = max(matched_paths, key=len)
    else:
        base = str(_repo_root_for_skill_install(cwd))
    return "project", [{"scope": "project", "path": base, "category": None}], warnings


def _skill_destinations_for_bases(
    skill_name: str,
    bases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    destinations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for base in bases:
        if base.get("scope") == "global":
            for surface, root in _default_global_roots():
                destination = root / skill_name
                key = str(destination)
                if key in seen:
                    continue
                seen.add(key)
                destinations.append({
                    "scope": "global",
                    "surface": surface,
                    "root": str(root),
                    "path": key,
                    "category": None,
                    "repo_path": None,
                })
            continue

        repo_path = Path(str(base.get("path") or "")).resolve()
        for surface in ("claude", "codex"):
            root = repo_path / f".{surface}" / "skills"
            destination = root / skill_name
            key = str(destination)
            if key in seen:
                continue
            seen.add(key)
            destinations.append({
                "scope": "project",
                "surface": surface,
                "root": str(root),
                "path": key,
                "category": base.get("category"),
                "repo_path": str(repo_path),
            })
    return destinations


def _install_path_state(path: Path, source: str | None = None) -> dict[str, Any]:
    if not os.path.lexists(path):
        return {"state": "missing"}
    if path.is_symlink():
        link_target = os.readlink(path)
        resolved = os.path.realpath(path)
        state = "same_link" if source and resolved == os.path.realpath(source) else "different_link"
        return {"state": state, "link_target": link_target, "resolved": resolved}
    if path.is_dir():
        return {
            "state": "directory",
            "has_skill_md": (path / "SKILL.md").is_file(),
            "resolved": str(path.resolve()),
        }
    if path.is_file():
        return {"state": "file", "resolved": str(path.resolve())}
    return {"state": "other"}


def _link_skill_action(
    skill_name: str,
    source: dict[str, Any],
    destination: dict[str, Any],
    *,
    blocked_reason: str = "",
) -> dict[str, Any]:
    path = Path(str(destination["path"]))
    return {
        "op": "link",
        "skill": skill_name,
        "source": source.get("source"),
        "source_bucket": source.get("source_bucket"),
        "destination": destination["path"],
        "root": destination["root"],
        "scope": destination["scope"],
        "surface": destination["surface"],
        "category": destination.get("category"),
        "repo_path": destination.get("repo_path"),
        "existing": _install_path_state(path, str(source.get("source") or "")),
        "blocked_reason": blocked_reason,
    }


def _installed_occurrences_for_skill(
    model: dict[str, Any],
    skill_name: str,
    *,
    cwd: Path,
    include_global: bool = True,
    include_project: bool = True,
) -> list[dict[str, Any]]:
    payload = collect_skill_visibility(
        model,
        cwd=str(cwd),
        include_global=include_global,
        include_project=include_project,
        include_sources=False,
    )
    return [
        item for item in payload.get("occurrences") or []
        if item.get("availability") == "installed" and str(item.get("name") or "") == skill_name
    ]


def _scope_filter_matches(occurrence: dict[str, Any], from_scope: str) -> bool:
    layer = str(occurrence.get("layer") or "")
    if from_scope == "all":
        return layer.startswith("global:") or layer.startswith("project:")
    if from_scope == "global":
        return layer.startswith("global:")
    if from_scope == "project":
        return layer.startswith("project:")
    return layer.startswith("global:") or layer.startswith("project:")


def _unlink_skill_action(occurrence: dict[str, Any], *, reason: str) -> dict[str, Any]:
    path = str(occurrence.get("path") or "")
    return {
        "op": "unlink",
        "skill": occurrence.get("name"),
        "destination": path,
        "scope": "global" if str(occurrence.get("layer") or "").startswith("global:") else "project",
        "surface": "claude" if ":claude" in str(occurrence.get("layer") or "") else "codex",
        "source": occurrence.get("source"),
        "layer": occurrence.get("layer"),
        "reason": reason,
        "existing": _install_path_state(Path(path)) if path else {"state": "missing"},
    }


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for action in actions:
        key = (str(action.get("op") or ""), str(action.get("destination") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _activation_packet(
    skill_name: str,
    selected_source: dict[str, Any],
    actions: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    source_path = Path(str(selected_source.get("source") or "")).resolve()
    skill_md_path = source_path / "SKILL.md"
    try:
        skill_md_bytes = skill_md_path.read_bytes()
        skill_md = skill_md_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"Could not read activation packet for {skill_name!r}: {exc}"

    surface_targets: dict[str, list[str]] = {}
    for action in actions:
        if action.get("op") != "link":
            continue
        surface = str(action.get("surface") or "")
        destination = str(action.get("destination") or "")
        if surface and destination:
            surface_targets.setdefault(surface, []).append(destination)

    return {
        "name": skill_name,
        "source": str(source_path),
        "source_bucket": selected_source.get("source_bucket"),
        "skill_md_path": str(skill_md_path),
        "skill_md_sha256": hashlib.sha256(skill_md_bytes).hexdigest(),
        "skill_md": skill_md,
        "surface_targets": {
            surface: sorted(targets)
            for surface, targets in sorted(surface_targets.items())
        },
        "instructions": (
            "Use this SKILL.md content immediately in the current agent session. "
            "The filesystem links make the skill visible to future Claude and Codex sessions."
        ),
    }, None


def _require_lifecycle_skill_name(action: str, skill_name: str | None) -> str:
    if not skill_name:
        raise RuntimeError(f"`skill {action}` requires a skill name.")
    return skill_name


def _skill_blocked_reason(
    model: dict[str, Any],
    skill_name: str,
    resolved_to: str,
    cwd: Path,
    force: bool,
) -> str:
    if resolved_to == "global" and not _global_install_allowed(model, skill_name) and not force:
        return "global install is not allowed by skill-scope policy"
    if resolved_to == "project" and not force:
        rule = _matching_scope_rule(skill_name, _scope_rules(model, cwd=cwd), cwd=cwd)
        allowed_paths = list(rule.get("paths") or []) if rule else []
        if allowed_paths and not any(_path_prefix_matches(cwd, path) for path in allowed_paths):
            return "project install is outside allowed skill-scope paths"
    return ""


def _skill_link_actions_for_bases(
    skill_name: str,
    source_record: dict[str, Any],
    bases: list[dict[str, Any]],
    blocked_reason: str,
) -> list[dict[str, Any]]:
    return [
        _link_skill_action(skill_name, source_record, destination, blocked_reason=blocked_reason)
        for destination in _skill_destinations_for_bases(skill_name, bases)
    ]


def _plan_primary_skill_links(
    model: dict[str, Any],
    action: str,
    skill_name: str | None,
    cwd_path: Path,
    to: str,
    categories: list[str],
    source: str | None,
    force: bool,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], dict[str, Any] | None, str]:
    if action not in {"plan", "add", "move", "activate"}:
        return [], [], [], None, to
    name = _require_lifecycle_skill_name(action, skill_name)
    source_options = _skill_source_options(model, name, explicit_source=source)
    selected_source = source_options[0] if source_options else None
    if not selected_source:
        return [], [f"No source directory found for skill {name!r}."], source_options, None, to

    resolved_to, bases, warnings = _skill_destination_bases(
        model,
        name,
        cwd=cwd_path,
        to=to,
        categories=categories,
    )
    blocked_reason = _skill_blocked_reason(model, name, resolved_to, cwd_path, force)
    if blocked_reason:
        warnings.append(blocked_reason)
    actions = _skill_link_actions_for_bases(name, selected_source, bases, blocked_reason)
    return actions, warnings, source_options, selected_source, resolved_to


def _planned_link_destinations(actions: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("destination") or "")
        for item in actions
        if item.get("op") == "link"
    }


def _plan_skill_removals(
    model: dict[str, Any],
    action: str,
    skill_name: str | None,
    cwd_path: Path,
    from_scope: str,
    existing_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if action not in {"remove", "move"}:
        return []
    name = _require_lifecycle_skill_name(action, skill_name)
    destination_paths = _planned_link_destinations(existing_actions)
    actions: list[dict[str, Any]] = []
    for occurrence in _installed_occurrences_for_skill(model, name, cwd=cwd_path):
        if not _scope_filter_matches(occurrence, from_scope):
            continue
        if action == "move" and str(occurrence.get("path") or "") in destination_paths:
            continue
        actions.append(_unlink_skill_action(
            occurrence,
            reason="move source cleanup" if action == "move" else "requested removal",
        ))
    return actions


def _lifecycle_visibility(
    model: dict[str, Any],
    cwd_path: Path,
    *,
    include_global: bool = True,
) -> dict[str, Any]:
    return collect_skill_visibility(
        model,
        cwd=str(cwd_path),
        include_global=include_global,
        include_project=True,
        include_sources=False,
    )


def _plan_skill_prune_actions(
    visibility: dict[str, Any],
    skill_name: str | None,
    *,
    from_scope: str = "all",
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    issue_keys = ("scope_violations", "global_not_allowed", "extra_global", "broken_global", "broken_project")
    for issue_key in issue_keys:
        for item in (visibility.get("issues") or {}).get(issue_key) or []:
            if skill_name and str(item.get("name") or "") != skill_name:
                continue
            if not _scope_filter_matches(item, from_scope):
                continue
            if item.get("path"):
                actions.append(_unlink_skill_action(item, reason=issue_key))
    return actions


def _sync_wanted_skill_names(visibility: dict[str, Any], skill_name: str | None) -> list[str]:
    if skill_name:
        return [skill_name]
    return [
        str(item.get("name") or "")
        for item in (visibility.get("issues") or {}).get("missing_for_cwd") or []
        if str(item.get("name") or "")
    ]


def _plan_one_skill_sync(
    model: dict[str, Any],
    wanted: str,
    explicit_source: str | None,
    cwd_path: Path,
    to: str,
    categories: list[str],
    force: bool,
) -> tuple[list[dict[str, Any]], list[str], str]:
    options = _skill_source_options(model, wanted, explicit_source=explicit_source)
    if not options:
        return [], [f"No source directory found for skill {wanted!r}."], to
    chosen = options[0]
    sync_to, bases, warnings = _skill_destination_bases(
        model,
        wanted,
        cwd=cwd_path,
        to=to,
        categories=categories,
    )
    blocked_reason = _skill_blocked_reason(model, wanted, sync_to, cwd_path, force)
    if blocked_reason:
        warnings.append(f"{wanted}: {blocked_reason}")
    actions = _skill_link_actions_for_bases(wanted, chosen, bases, blocked_reason)
    return actions, warnings, sync_to


def _plan_skill_sync_actions(
    model: dict[str, Any],
    visibility: dict[str, Any],
    skill_name: str | None,
    cwd_path: Path,
    to: str,
    categories: list[str],
    source: str | None,
    force: bool,
) -> tuple[list[dict[str, Any]], list[str], str]:
    actions: list[dict[str, Any]] = []
    warnings: list[str] = []
    resolved_to = to
    for wanted in _sync_wanted_skill_names(visibility, skill_name):
        explicit_source = source if wanted == skill_name else None
        link_actions, link_warnings, sync_to = _plan_one_skill_sync(
            model,
            wanted,
            explicit_source,
            cwd_path,
            to,
            categories,
            force,
        )
        actions.extend(link_actions)
        warnings.extend(link_warnings)
        resolved_to = sync_to if resolved_to == to else resolved_to
    return actions, warnings, resolved_to


def _lifecycle_needs_visibility(action: str, prune: bool) -> bool:
    return action in {"sync", "prune"} or prune


def _append_lifecycle_prune_actions(
    actions: list[dict[str, Any]],
    visibility: dict[str, Any],
    *,
    action: str,
    skill_name: str | None,
    from_scope: str,
    prune: bool,
) -> None:
    if action == "prune" or (action == "sync" and prune):
        actions.extend(_plan_skill_prune_actions(visibility, skill_name, from_scope=from_scope))


def _append_lifecycle_sync_actions(
    model: dict[str, Any],
    actions: list[dict[str, Any]],
    warnings: list[str],
    visibility: dict[str, Any],
    *,
    action: str,
    skill_name: str | None,
    cwd_path: Path,
    to: str,
    categories: list[str],
    source: str | None,
    force: bool,
    resolved_to: str,
) -> str:
    if action != "sync":
        return resolved_to
    sync_actions, sync_warnings, sync_to = _plan_skill_sync_actions(
        model,
        visibility,
        skill_name,
        cwd_path,
        to,
        categories,
        source,
        force,
    )
    actions.extend(sync_actions)
    warnings.extend(sync_warnings)
    return sync_to if resolved_to == to else resolved_to


def _lifecycle_activation_packet_if_needed(
    *,
    action: str,
    skill_name: str | None,
    selected_source: dict[str, Any] | None,
    actions: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    if action == "activate" and skill_name and selected_source:
        return _activation_packet(skill_name, selected_source, actions)
    return None, None


def _lifecycle_plan_summary(actions: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "actions": len(actions),
        "link": sum(1 for item in actions if item.get("op") == "link"),
        "unlink": sum(1 for item in actions if item.get("op") == "unlink"),
        "blocked": sum(1 for item in actions if item.get("blocked_reason")),
    }


def skill_lifecycle_plan(
    model: dict[str, Any],
    action: str,
    *,
    skill_name: str | None = None,
    cwd: str | None = None,
    to: str = "auto",
    categories: list[str] | None = None,
    source: str | None = None,
    from_scope: str = "all",
    prune: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    cwd_path = Path(cwd or os.getcwd()).resolve()
    categories = [item for item in (categories or []) if item]
    actions, warnings, source_options, selected_source, resolved_to = _plan_primary_skill_links(
        model,
        action,
        skill_name,
        cwd_path,
        to,
        categories,
        source,
        force,
    )
    actions.extend(_plan_skill_removals(model, action, skill_name, cwd_path, from_scope, actions))

    needs_prune_visibility = action == "prune" or (action == "sync" and prune)
    needs_sync_visibility = action == "sync"
    visibility = _lifecycle_visibility(model, cwd_path) if needs_prune_visibility else {}
    sync_visibility = (
        _lifecycle_visibility(model, cwd_path, include_global=False)
        if needs_sync_visibility
        else visibility
    )
    _append_lifecycle_prune_actions(
        actions,
        visibility,
        action=action,
        skill_name=skill_name,
        from_scope=from_scope,
        prune=prune,
    )
    resolved_to = _append_lifecycle_sync_actions(
        model,
        actions,
        warnings,
        sync_visibility,
        action=action,
        skill_name=skill_name,
        cwd_path=cwd_path,
        to=to,
        categories=categories,
        source=source,
        force=force,
        resolved_to=resolved_to,
    )

    actions = _dedupe_actions(actions)
    activation_packet, packet_warning = _lifecycle_activation_packet_if_needed(
        action=action,
        skill_name=skill_name,
        selected_source=selected_source,
        actions=actions,
    )
    if packet_warning:
        warnings.append(packet_warning)
    return {
        "action": action,
        "skill": skill_name,
        "cwd": str(cwd_path),
        "requested_to": to,
        "resolved_to": resolved_to,
        "categories": categories,
        "from_scope": from_scope,
        "source_options": source_options,
        "selected_source": selected_source,
        "activation_packet": activation_packet,
        "warnings": warnings,
        "actions": actions,
        "summary": _lifecycle_plan_summary(actions),
    }


def _apply_lifecycle_link(
    action: dict[str, Any],
    destination: Path,
    *,
    dry_run: bool,
    allow_directories: bool,
    force: bool,
) -> None:
    repo_path = action.get("repo_path")
    if repo_path and not Path(str(repo_path)).is_dir():
        action["status"] = "would_skip_missing_repo" if dry_run else "skipped_missing_repo"
        return
    source = Path(str(action.get("source") or "")).resolve()
    if dry_run:
        action["status"] = "ok" if action.get("existing", {}).get("state") == "same_link" else "would_link"
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(destination) and not _prepare_lifecycle_link_destination(
        action,
        destination,
        source,
        allow_directories=allow_directories,
        force=force,
    ):
        return
    destination.symlink_to(source, target_is_directory=True)
    action["status"] = "linked"


def _prepare_lifecycle_link_destination(
    action: dict[str, Any],
    destination: Path,
    source: Path,
    *,
    allow_directories: bool,
    force: bool,
) -> bool:
    if destination.is_symlink() or destination.is_file():
        if destination.is_symlink() and os.path.realpath(destination) == str(source):
            action["status"] = "ok"
            return False
        if not force and not destination.is_symlink():
            action["status"] = "conflict_file"
            return False
        destination.unlink()
        return True
    if destination.is_dir():
        if not (force and allow_directories):
            action["status"] = "conflict_directory"
            return False
        shutil.rmtree(destination)
    return True


def _apply_lifecycle_unlink(
    action: dict[str, Any],
    destination: Path,
    *,
    dry_run: bool,
    allow_directories: bool,
) -> None:
    if action.get("pinned"):
        action["status"] = "skipped_pinned"
        action["code"] = PRUNE_SKIPPED_PINNED
        return
    if dry_run:
        action["status"] = "would_unlink" if os.path.lexists(destination) else "missing"
        return
    if not os.path.lexists(destination):
        action["status"] = "missing"
    elif destination.is_symlink() or destination.is_file():
        destination.unlink()
        action["status"] = "unlinked"
    elif destination.is_dir():
        if not allow_directories:
            action["status"] = "skipped_directory"
        else:
            shutil.rmtree(destination)
            action["status"] = "removed_directory"
    else:
        action["status"] = "skipped_unknown"


def _apply_lifecycle_action(
    action: dict[str, Any],
    *,
    dry_run: bool,
    allow_directories: bool,
    force: bool,
) -> None:
    destination = Path(str(action.get("destination") or ""))
    if action.get("blocked_reason"):
        action["status"] = "blocked"
    elif action.get("op") == "link":
        _apply_lifecycle_link(
            action,
            destination,
            dry_run=dry_run,
            allow_directories=allow_directories,
            force=force,
        )
    elif action.get("op") == "unlink":
        _apply_lifecycle_unlink(
            action,
            destination,
            dry_run=dry_run,
            allow_directories=allow_directories,
        )


def _summarize_applied_lifecycle_plan(plan: dict[str, Any], dry_run: bool) -> None:
    actions = plan.get("actions") or []

    plan["dry_run"] = dry_run
    plan["summary"]["applied"] = 0 if dry_run else sum(
        1 for item in actions
        if item.get("status") in {"linked", "unlinked", "removed_directory"}
    )
    plan["summary"]["unchanged"] = sum(
        1 for item in actions
        if item.get("status") == "ok"
    )
    plan["summary"]["skipped"] = sum(
        1 for item in actions
        if str(item.get("status") or "").startswith(("skipped", "conflict", "blocked"))
    )


def apply_skill_lifecycle_plan(
    plan: dict[str, Any],
    *,
    dry_run: bool,
    allow_directories: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    for action in plan.get("actions") or []:
        _apply_lifecycle_action(
            action,
            dry_run=dry_run,
            allow_directories=allow_directories,
            force=force,
        )
    _summarize_applied_lifecycle_plan(plan, dry_run)
    return plan


def print_skill_lifecycle_text(payload: dict[str, Any]) -> None:
    dry_run = payload.get("dry_run")
    mode = "dry-run" if dry_run else "apply"
    print(f"skill {payload.get('action')}: {payload.get('skill') or '(policy)'} ({mode})")
    print(f"cwd: {payload.get('cwd')}")
    print(f"target: {payload.get('resolved_to')}")
    if payload.get("selected_source"):
        print(f"source: {payload['selected_source'].get('source')}")
    for warning in payload.get("warnings") or []:
        print(f"warning: {warning}")
    if not payload.get("actions"):
        print("actions: none")
        return
    print("actions:")
    for action in payload.get("actions") or []:
        op = action.get("op")
        status = action.get("status") or "planned"
        dest = action.get("destination")
        skill = action.get("skill")
        print(f"  - {status}: {op} {skill} -> {dest}")
    packet = payload.get("activation_packet")
    if packet:
        print("activation packet:")
        print(f"name: {packet.get('name')}")
        print(f"source: {packet.get('source')}")
        print(f"skill_md_sha256: {packet.get('skill_md_sha256')}")
        print("skill_md:")
        print(str(packet.get("skill_md") or "").rstrip())
