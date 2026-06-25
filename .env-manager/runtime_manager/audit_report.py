"""Skill-visibility audit and reporting.

Single responsibility: assembling the visibility snapshot
(``collect_skill_visibility``), issue groups, fleet/audit rows, broken-link
taxonomy, evidence annotation, the explain view, serialization/compaction, and
all text renderers. Depends on ._skill_common, .policy_eval, and .inventory.
"""

from __future__ import annotations

import fnmatch
import glob
import hashlib
import os
import shutil
from pathlib import Path
from typing import Any, Callable

# NOTE: ``shlex`` is imported function-locally (not here) on purpose. This module
# is re-executed into ``skill_visibility``'s shared namespace by a facade that
# STRIPS every top-level import and replays only a FIXED header import set
# (fnmatch/glob/hashlib/os/shutil/Path/Any/Callable + .shared). A top-level
# ``import shlex`` would therefore be dropped on the facade path, leaving the
# fix_command builders with an undefined ``shlex`` at runtime. Importing shlex
# inside the functions that need it survives the strip and works on both the
# direct-import and facade paths without editing the facade.

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

from ._skill_common import *
from .policy_eval import *
from .inventory import *

__all__ = [
    '_add_skill_visibility_recommendation',
    '_recommendation_provenance',
    '_skill_visibility_recommendations',
    '_visibility_installed_by_layer',
    '_broken_visibility_items',
    '_global_not_allowed_items',
    '_extra_global_items',
    '_archive_source_items',
    '_visibility_issue_groups',
    '_BROKEN_ISSUE_TYPES',
    '_issue_row_fix_command',
    '_enrich_issue_rows',
    '_visibility_name_count',
    '_skill_visibility_summary',
    'BROKEN_LINK_CLASSES',
    'BROKEN_LINK_ACTIONS',
    '_broken_link_fix_command',
    '_classify_broken_link',
    '_enrich_broken_links',
    'broken_link_class_counts',
    'attach_skill_evidence',
    'collect_skill_visibility',
    'SKILL_AUDIT_REPO_ISSUE_KEYS',
    'SKILL_AUDIT_GLOBAL_ISSUE_KEYS',
    '_skill_audit_candidate_from_path',
    '_skill_audit_client_paths',
    '_skill_audit_category_paths',
    '_git_repo_paths_under',
    '_skill_audit_scan_root_paths',
    '_skill_audit_candidate_paths',
    '_skill_names',
    '_skill_audit_issue_counts',
    '_skill_audit_has_repo_issues',
    '_classified_broken_rows',
    '_classified_issue_rows',
    '_skill_audit_repo_row',
    '_skill_audit_global_row',
    '_available_skill_overlays',
    '_skill_audit_next_actions',
    'collect_skill_audit',
    'SKILL_VISIBILITY_COMPACT_ISSUE_KEYS',
    '_compact_skill_visibility_skill',
    '_compact_skill_visibility_issues',
    'compact_skill_visibility_payload',
    'EXPLAIN_SCHEMA_VERSION',
    '_layer_family',
    '_explain_occurrence_view',
    '_explain_lost_reason',
    '_explain_inactive_overlay_rules',
    '_explain_scope_rules',
    '_explain_machine_profile',
    '_explain_source_options',
    '_explain_remediation',
    'explain_skill_visibility',
    'skill_visibility_next_actions',
    '_print_visibility_header',
    '_layer_detail',
    '_print_visibility_layers',
    '_issue_count_total',
    '_print_visibility_issues',
    '_print_visibility_effective',
    '_print_visibility_shadowed',
    '_print_limited_issue_list',
    '_format_scope_violation',
    '_format_global_not_allowed',
    '_format_missing_for_cwd',
    '_print_visibility_undefined',
    '_print_visibility_next_actions',
    '_print_visibility_recommendations',
    'print_skill_visibility_text',
    '_print_skill_audit_global',
    '_repo_audit_problem_summary',
    'print_skill_audit_text',
]


def _add_skill_visibility_recommendation(
    recommendations: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    item: dict[str, Any],
) -> None:
    key = (
        str(item.get("action") or ""),
        str(item.get("skill") or ""),
        str(item.get("scope_rule") or ""),
    )
    if key in seen:
        return
    seen.add(key)
    recommendations.append(item)


def _recommendation_provenance(item: dict[str, Any], issue_type: str) -> dict[str, Any]:
    """Pull the four canonical provenance fields off an (enriched) issue row.

    The issue rows have already been stamped by ``_enrich_issue_rows`` with
    ``type``/``rule_id``/``policy_path``/``origin``/``fix_command``; this mirrors
    them onto the suggestion so a recommendation is actionable on its own,
    falling back to the upstream ``scope_rule``/``scope_policy_path`` if an
    unenriched row is passed in.
    """
    return {
        "issue_type": str(item.get("type") or issue_type),
        "rule_id": item.get("rule_id") if item.get("rule_id") is not None else item.get("scope_rule"),
        "policy_path": item.get("policy_path") if item.get("policy_path") is not None else item.get("scope_policy_path"),
        "origin": item.get("origin"),
        "fix_command": str(
            item.get("fix_command") or _issue_row_fix_command(issue_type, item)
        ),
    }


def _skill_visibility_recommendations(issues: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in issues.get("missing_for_cwd") or []:
        _add_skill_visibility_recommendation(recommendations, seen, {
            "action": "add_project_skill",
            "skill": item.get("name"),
            "scope_rule": item.get("scope_rule"),
            "target": "project_or_client_skill_repos",
            "allowed_paths": item.get("allowed_paths") or [],
            **_recommendation_provenance(item, "missing_for_cwd"),
            "hint": (
                "Add this skill to the active client's skill-repos.yaml, or activate it "
                "for this cwd ephemerally with `sbp skill activate <skill> --cwd <repo>`. "
                "Use `sbp overlay activate <name> --cwd <repo>` for a one-session/cwd "
                "policy-evaluated flip, or `sbp overlay on <name>` to PERSIST the overlay "
                "across sessions until `overlay off`."
            ),
        })
    for item in issues.get("scope_violations") or []:
        _add_skill_visibility_recommendation(recommendations, seen, {
            "action": "move_or_unlink_skill",
            "skill": item.get("name"),
            "scope_rule": item.get("scope_rule"),
            "source_path": item.get("path"),
            "allowed_paths": item.get("allowed_paths") or [],
            **_recommendation_provenance(item, "scope_violations"),
            "hint": "Move this project-local install under an allowed repo path, or unlink it here.",
        })
    for item in issues.get("global_not_allowed") or []:
        _add_skill_visibility_recommendation(recommendations, seen, {
            "action": "move_global_to_project",
            "skill": item.get("name"),
            "source_path": item.get("path"),
            **_recommendation_provenance(item, "global_not_allowed"),
            "hint": (
                "Remove this user-global install or add an explicit allow_global rule "
                "if it really should be available in every repo."
            ),
        })
    for item in issues.get("extra_global") or []:
        _add_skill_visibility_recommendation(recommendations, seen, {
            "action": "declare_or_unlink_global",
            "skill": item.get("name"),
            "source_path": item.get("path"),
            **_recommendation_provenance(item, "extra_global"),
            "hint": "Declare this skill in the global policy or remove the user-global link.",
        })
    return recommendations


def _visibility_installed_by_layer(
    occurrences: list[dict[str, Any]],
    layer_prefix: str,
) -> list[dict[str, Any]]:
    return [
        item for item in occurrences
        if str(item.get("layer", "")).startswith(layer_prefix)
    ]


def _broken_visibility_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item.get("state") == "broken"]


def _global_not_allowed_items(
    model: dict[str, Any],
    global_installed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        item for item in global_installed
        if item.get("state") != "broken"
        and not _global_install_allowed(model, str(item.get("name") or ""))
    ]


def _extra_global_items(
    model: dict[str, Any],
    global_installed: list[dict[str, Any]],
    declared_names: set[str],
) -> list[dict[str, Any]]:
    extras: list[dict[str, Any]] = []
    for item in global_installed:
        name = str(item.get("name") or "")
        if item.get("state") == "broken" or name in declared_names:
            continue
        if _global_install_allowed(model, name) or _scope_allows_global(model, name):
            continue
        extras.append(item)
    return extras


def _archive_source_items(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in occurrences
        if item.get("source_bucket") == "archive"
    ]


def _visibility_issue_groups(
    model: dict[str, Any],
    cwd_path: Path,
    occurrences: list[dict[str, Any]],
    declared_occurrences: list[dict[str, Any]],
    effective: list[dict[str, Any]],
    shadowed: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    declared_names = {str(item.get("name")) for item in declared_occurrences}
    global_installed = _visibility_installed_by_layer(occurrences, "global:")
    project_installed = _visibility_installed_by_layer(occurrences, "project:")
    return {
        "broken_global": _broken_visibility_items(global_installed),
        "broken_project": _broken_visibility_items(project_installed),
        "global_not_allowed": _global_not_allowed_items(model, global_installed),
        "extra_global": _extra_global_items(model, global_installed, declared_names),
        "shadowed": shadowed,
        "archive_sources": _archive_source_items(occurrences),
        "scope_violations": _skill_scope_violations(model, occurrences),
        "missing_for_cwd": _missing_for_cwd(model, cwd_path, effective),
    }


# Issue groups whose rows describe a non-broken installed/global skill link
# (broken_* rows are already classified by the broken-link taxonomy and get
# their fix_command/origin from ``_enrich_broken_links``).
_BROKEN_ISSUE_TYPES = ("broken_global", "broken_project")


def _issue_row_fix_command(issue_type: str, row: dict[str, Any]) -> str:
    """The EXACT copy-pasteable command that resolves one issue row.

    Broken rows already carry a taxonomy ``fix_command`` (relink/prune/migrate/
    investigate) from ``_enrich_broken_links``; reuse it verbatim. Every other
    issue type maps to the one narrowest manage.py command an agent can run
    without re-deriving anything from the policy.
    """
    import shlex  # facade-safe local import (see module header note)

    name = str(row.get("name") or "")
    path = str(row.get("path") or "")
    if issue_type in _BROKEN_ISSUE_TYPES:
        existing = str(row.get("fix_command") or "")
        if existing:
            return existing
        # Defensive: an unenriched broken row defaults to prune (mirrors
        # ``broken_link_class_counts`` / ``_classified_broken_rows``).
        return f"rm {shlex.quote(path)}  # prune dead link {name!r}" if path else ""
    if issue_type == "missing_for_cwd":
        return f"sbp skill activate {name} --cwd <repo>"
    if issue_type == "scope_violations":
        return f"sbp skill remove {name} --from project --cwd {path or '<repo>'} --yes"
    if issue_type == "global_not_allowed":
        return f"sbp skill remove {name} --from global --yes"
    if issue_type == "extra_global":
        return f"sbp skill remove {name} --from global --yes"
    if issue_type == "archive_sources":
        return (
            f"copy {name!r} into skills-private, then repoint its source root "
            f"(stale archive copy at {row.get('source') or path})"
        )
    if issue_type == "shadowed":
        return (
            f"review the {name!r} declarations; unlink the lower-precedence layer "
            "if the shadow is unintended"
        )
    return ""


def _enrich_issue_rows(
    issues: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Stamp every issue row with the four act-without-re-derivation fields.

    EVERY row in every group gains, in place:
      * ``type``        — the issue group it belongs to (the dict key).
      * ``rule_id``     — the matched/violated skill-scope rule id, when the row
                          carries one (``scope_rule`` for scope/missing rows).
      * ``policy_path`` — the policy file that rule came from (``scope_policy_path``).
      * ``origin``      — broken-link triage class (other-machine/moved/dangling/
                          unreadable) for broken rows; ``None`` otherwise.
      * ``fix_command`` — the exact copy-pasteable command to resolve the row.

    The data already exists internally (scope rows carry ``scope_rule`` /
    ``scope_policy_path``; broken rows are pre-classified by
    ``_enrich_broken_links``); this surfaces it at the serialization boundary so
    an audit row is actionable on its own.

    Each group's rows are REPLACED with shallow copies before stamping. A single
    occurrence dict can belong to more than one group (``global_not_allowed`` and
    ``extra_global`` filter the same installed-link list), so mutating the shared
    dict in place would make ``type``/``fix_command`` last-writer-wins and wrong
    for one of the groups. Copying per group keeps every row's provenance
    self-consistent without disturbing the shared ``occurrences`` list. Mutates
    and returns ``issues``.
    """
    for issue_type, rows in issues.items():
        enriched: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                enriched.append(row)
                continue
            new_row = dict(row)
            new_row["type"] = issue_type
            # rule provenance: prefer an already-resolved scope_rule/policy_path,
            # falling back to whatever the row already declared so we never blank
            # a value that was set upstream.
            new_row["rule_id"] = (
                row.get("rule_id")
                if row.get("rule_id") is not None
                else row.get("scope_rule")
            )
            new_row["policy_path"] = (
                row.get("policy_path")
                if row.get("policy_path") is not None
                else row.get("scope_policy_path")
            )
            # origin only applies to the broken-link taxonomy; non-broken rows
            # carry an explicit None so the key is always present (stable schema).
            if issue_type in _BROKEN_ISSUE_TYPES:
                new_row["origin"] = row.get("origin") or "dangling"
            else:
                new_row.setdefault("origin", None)
            new_row["fix_command"] = row.get("fix_command") or _issue_row_fix_command(
                issue_type, new_row
            )
            enriched.append(new_row)
        issues[issue_type] = enriched
    return issues


def _visibility_name_count(items: list[dict[str, Any]]) -> int:
    return len({str(item.get("name")) for item in items})


def _skill_visibility_summary(
    *,
    effective: list[dict[str, Any]],
    occurrences: list[dict[str, Any]],
    layers: list[dict[str, Any]],
    issues: dict[str, list[dict[str, Any]]],
    undefined_sources: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
) -> dict[str, int]:
    scope_violations = issues["scope_violations"]
    missing_for_cwd = issues["missing_for_cwd"]
    broken_all = [*issues["broken_global"], *issues["broken_project"]]
    broken_by_class = broken_link_class_counts(broken_all)
    return {
        "effective": len(effective),
        "occurrences": len(occurrences),
        "layers": len(layers),
        "broken_global": len(issues["broken_global"]),
        "broken_global_skills": _visibility_name_count(issues["broken_global"]),
        "broken_project": len(issues["broken_project"]),
        "broken_project_skills": _visibility_name_count(issues["broken_project"]),
        # Counts-by-class over all broken installed links (global + project), so
        # every surface that reads this summary sees the taxonomy breakdown.
        "broken_by_class": broken_by_class,
        "global_not_allowed": len(issues["global_not_allowed"]),
        "global_not_allowed_skills": _visibility_name_count(issues["global_not_allowed"]),
        "extra_global": len(issues["extra_global"]),
        "extra_global_skills": _visibility_name_count(issues["extra_global"]),
        "shadowed": len(issues["shadowed"]),
        "archive_sources": len(issues["archive_sources"]),
        "archive_source_skills": _visibility_name_count(issues["archive_sources"]),
        "scope_violations": len(scope_violations),
        "scope_violation_skills": _visibility_name_count(scope_violations),
        "missing_for_cwd": len(missing_for_cwd),
        "missing_for_cwd_skills": _visibility_name_count(missing_for_cwd),
        "undefined_sources": len(undefined_sources),
        "undefined_source_skills": _visibility_name_count(undefined_sources),
        "recommendations": len(recommendations),
    }


# --- broken-link taxonomy ---------------------------------------------------
#
# A broken installed skill symlink is not a mystery: it is one of exactly four
# things. Classifying it turns "317 broken links" (mostly the same migration
# repeated N times) into "~3 decisions". The four classes and how each is
# detected:
#
#   other-machine  the link target lives under a root that machines.yaml maps to
#                  a DIFFERENT machine profile (e.g. /Users/operator/repos/... seen from
#                  the devbox). Detected via runtime_manager.machines:
#                  ``is_foreign_path(target, current_machine)``. Action: migrate.
#   moved          a skill with the SAME name still exists under some current
#                  skill_source_roots, so the link can simply be re-pointed.
#                  Detected via ``_skill_source_options(model, name)``. Action:
#                  relink.
#   dangling       no source for that name exists anywhere on this box and the
#                  target is not foreign -> the link is dead weight. Action:
#                  prune.
#   unreadable     the link itself cannot be read (permission error / symlink
#                  loop). Detected from the scanner's resolve error. Action:
#                  investigate.
BROKEN_LINK_CLASSES = ("other-machine", "moved", "dangling", "unreadable")

# Stable per-class suggested action verbs surfaced in each audit row.
BROKEN_LINK_ACTIONS = {
    "other-machine": "migrate",
    "moved": "relink",
    "dangling": "prune",
    "unreadable": "investigate",
}


def _broken_link_fix_command(
    origin: str,
    occurrence: dict[str, Any],
    source_options: list[dict[str, Any]],
    *,
    machine_id: str | None,
) -> str:
    """The EXACT command an operator runs to heal one broken link.

    Each class has one narrowest heal:
      relink   -> repoint the link at the live source we found.
      prune    -> remove the dead link.
      migrate  -> remove a link that belongs to another machine's tree.
      investigate -> surface the unreadable link for a human.
    """
    import shlex  # facade-safe local import (see module header note)

    path = str(occurrence.get("path") or "")
    name = str(occurrence.get("name") or "")
    # Every interpolated filesystem path is shell-quoted: these strings are
    # advertised as the EXACT command to run, so a path with a space / glob /
    # ``;`` / ``$`` / quote must paste safely (the ``rm`` prune commands are the
    # dangerous ones). The trailing ``# ...`` comments are descriptive only.
    if origin == "moved" and source_options:
        source = str(source_options[0].get("source") or "")
        return f"ln -sfn {shlex.quote(source)} {shlex.quote(path)}"
    if origin == "dangling":
        return f"rm {shlex.quote(path)}  # prune dead link {name!r}"
    if origin == "other-machine":
        target = str(occurrence.get("link_target") or occurrence.get("link_target_abs") or "")
        suffix = f" (target {target} belongs to another machine"
        suffix += f", not {machine_id!r})" if machine_id else ")"
        return f"rm {shlex.quote(path)}  # migrate: drop foreign-machine link{suffix}"
    # unreadable: do not guess; show the operator what to inspect.
    return f"ls -ld {shlex.quote(path)}  # investigate unreadable link (permission/loop?)"


def _classify_broken_link(
    occurrence: dict[str, Any],
    model: dict[str, Any],
    *,
    machines_config: Any,
    machine_id: str | None,
) -> dict[str, str]:
    """Return ``{origin, suggested_action, fix_command}`` for one broken link.

    Precedence is deliberate:
      1. unreadable  — if we could not even read the link, classify nothing else.
      2. other-machine — a foreign target is a migration, never a mystery (this
         is the migration case: 47 links all under /Users/operator are ONE decision).
      3. moved        — a same-named live source means a one-symlink relink.
      4. dangling     — otherwise the link is dead and should be pruned.
    """
    name = str(occurrence.get("name") or "")
    reason = str(occurrence.get("broken_reason") or "")

    # 1) unreadable wins: a link we cannot read tells us nothing about its target.
    if reason == "unreadable":
        origin = "unreadable"
        source_options: list[dict[str, Any]] = []
    else:
        target = str(
            occurrence.get("link_target_abs") or occurrence.get("link_target") or ""
        )
        # 2) other-machine: target under a DIFFERENT machine's declared roots.
        foreign = False
        if machines_config is not None and machine_id and target:
            try:
                foreign = bool(machines_config.is_foreign_path(target, machine_id))
            except Exception:
                foreign = False
        # Resolve relink candidates once; reused for moved + fix command.
        try:
            source_options = _skill_source_options(model, name)
        except Exception:
            source_options = []
        if foreign:
            origin = "other-machine"
        elif source_options:
            # 3) moved: a live same-named source exists under a current root.
            origin = "moved"
        else:
            # 4) dangling: no source anywhere, target not foreign.
            origin = "dangling"

    return {
        "origin": origin,
        "suggested_action": BROKEN_LINK_ACTIONS[origin],
        "fix_command": _broken_link_fix_command(
            origin, occurrence, source_options, machine_id=machine_id
        ),
    }


def _enrich_broken_links(
    model: dict[str, Any],
    occurrences: list[dict[str, Any]],
) -> None:
    """Attach origin/suggested_action/fix_command to every broken installed link.

    Mutates the occurrence dicts in place. Because ``issues['broken_global']`` /
    ``issues['broken_project']`` reference these same dicts, the audit rows they
    feed gain the taxonomy fields for free. The machines config + current machine
    id are resolved once per call (best-effort) and reused across all links.
    """
    broken = [
        occurrence
        for occurrence in occurrences
        if occurrence.get("state") == "broken"
        and str(occurrence.get("availability")) == "installed"
    ]
    if not broken:
        return
    machines_config, machine_id = _machines_classifier()
    for occurrence in broken:
        occurrence.update(
            _classify_broken_link(
                occurrence,
                model,
                machines_config=machines_config,
                machine_id=machine_id,
            )
        )


def broken_link_class_counts(
    broken_items: list[dict[str, Any]],
) -> dict[str, int]:
    """Counts-by-class over already-classified broken-link occurrences.

    Returns a dict with every key in :data:`BROKEN_LINK_CLASSES` present (zero
    when absent) so the fleet summary shape is stable. An unclassified item (one
    that never went through ``_enrich_broken_links``) is bucketed as dangling so
    it is never silently dropped from the totals.
    """
    counts = {origin: 0 for origin in BROKEN_LINK_CLASSES}
    for item in broken_items:
        origin = str(item.get("origin") or "dangling")
        if origin not in counts:
            origin = "dangling"
        counts[origin] += 1
    return counts


def attach_skill_evidence(
    payload: dict[str, Any],
    evidence_index: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach OPTIONAL per-candidate ``evidence`` onto the candidate rows.

    ``evidence_index`` maps a skill name to a small evidence dict (produced by the
    skillbox-config evidence backend) of the shape::

        {<skill>: {invocations_in_repo, last_used, fleet_wide_count}}

    The candidate/effective and source-backed (``undefined_sources``) rows get an
    ``evidence`` field WHEN — and only when — the backend has data for that skill.
    When ``evidence_index`` is falsy (Cass unavailable, no provider) NOTHING is
    attached: rows keep their existing shape and the absence means "unknown". This
    is mutate-in-place on ``payload`` and returns it for convenience. It never
    raises and never blocks a recalibrate.
    """
    if not evidence_index:
        return payload
    if not isinstance(evidence_index, dict):
        return payload
    for key in ("effective", "undefined_sources"):
        for row in payload.get(key) or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            facts = evidence_index.get(name)
            if isinstance(facts, dict) and facts:
                # Copy so the caller's index is never mutated through the payload.
                row["evidence"] = dict(facts)
    return payload


def _override_source_record(
    model: dict[str, Any],
    skill_name: str,
    occurrences: list[dict[str, Any]],
) -> dict[str, Any]:
    for occurrence in occurrences:
        if str(occurrence.get("name") or "") != skill_name:
            continue
        source = str(occurrence.get("source") or "").strip()
        if source:
            return {
                "source": source,
                "source_bucket": occurrence.get("source_bucket"),
                "path": occurrence.get("path"),
            }
    try:
        options = _skill_source_options(model, skill_name)
    except RuntimeError:
        options = []
    if options:
        return dict(options[0])
    return {}


def _repo_override_visibility(
    model: dict[str, Any],
    cwd_path: Path,
    occurrences: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Repo override occurrences folded into the canonical effective merge."""
    policy = _repo_override_policy(cwd_path)
    policy_path = str(policy.get("_policy_path") or "")
    if not Path(policy_path).is_file():
        return [], []

    layer = {
        "id": "repo-override-file",
        "label": "repo override file",
        "rank": REPO_OVERRIDE_LAYER_RANK,
        "scope": "repo",
        "kind": "override",
        "path": policy_path,
        "present": True,
        "skill_count": 0,
        "vetoed_floor": [],
    }
    if not policy.get("ok"):
        layer["config_error"] = "; ".join(
            str(error.get("message") or error) for error in policy.get("errors") or []
        )
        return [], [layer]

    override_occurrences: list[dict[str, Any]] = []

    def _base_row(skill_name: str, action: str, state: str, rank: int) -> dict[str, Any]:
        source_record = _override_source_record(model, skill_name, occurrences)
        return {
            "name": skill_name,
            "availability": "override",
            "state": state,
            "layer": "repo-override-file",
            "layer_label": "repo override file",
            "layer_rank": rank,
            "scope": "repo",
            "source": source_record.get("source"),
            "source_bucket": source_record.get("source_bucket"),
            "path": source_record.get("path") or str(cwd_path),
            "override_action": action,
            "policy_path": policy_path,
        }

    for skill_name in policy.get("pin_on") or []:
        row = _base_row(str(skill_name), "pin_on", "pinned", REPO_OVERRIDE_LAYER_RANK)
        if not str(row.get("source") or "").strip():
            row["state"] = "broken"
            row["broken_reason"] = "override_source_missing"
        override_occurrences.append(row)

    vetoed_floor: list[str] = []
    for skill_name in policy.get("pin_off") or []:
        name = str(skill_name)
        if name in DISPATCHER_CORE:
            vetoed_floor.append(name)
            continue
        override_occurrences.append(
            _base_row(name, "pin_off", "disabled", REPO_OVERRIDE_LAYER_RANK)
        )

    for skill_name in policy.get("opt_out_global") or []:
        name = str(skill_name)
        if name in DISPATCHER_CORE:
            vetoed_floor.append(name)
            continue
        row = _base_row(name, "opt_out_global", "disabled", GLOBAL_LAYER_RANK + 5)
        row["layer"] = "repo-override-file:global-opt-out"
        row["layer_label"] = "repo override global opt-out"
        override_occurrences.append(row)

    layer["skill_count"] = len(override_occurrences)
    layer["vetoed_floor"] = sorted(set(vetoed_floor))
    return override_occurrences, [layer]


def collect_skill_visibility(
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    include_global: bool = True,
    include_project: bool = True,
    include_sources: bool = False,
    evidence_provider: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Collect a conflict-aware skill availability view for a model.

    ``evidence_provider`` (optional) is a zero-arg callable returning a per-skill
    evidence index (see ``attach_skill_evidence``). When supplied AND it yields
    data, candidate rows gain an OPTIONAL ``evidence`` field. The provider is
    called defensively — any failure is swallowed so missing/unreachable evidence
    never blocks or crashes the visibility view (and a recalibrate that uses it).
    """
    cwd_path = Path(cwd or os.getcwd()).resolve()
    declared_occurrences, declared_layers = _declared_skill_occurrences(model)
    installed_occurrences, installed_layers = _collect_installed_visibility_layers(
        cwd_path,
        include_global=include_global,
        include_project=include_project,
    )
    base_occurrences = [*declared_occurrences, *installed_occurrences]
    override_occurrences, override_layers = _repo_override_visibility(
        model,
        cwd_path,
        base_occurrences,
    )
    occurrences = [*base_occurrences, *override_occurrences]
    layers = [*declared_layers, *installed_layers, *override_layers]
    # Classify broken installed links up front so the taxonomy fields (origin,
    # suggested_action, fix_command) flow into both occurrences and the issue
    # groups that reference the same dicts.
    _enrich_broken_links(model, occurrences)

    visibility_decisions, shadowed = _effective_occurrences(occurrences)
    for decision in visibility_decisions:
        decision["winning_layer"] = decision.get("layer")
    effective = [
        decision for decision in visibility_decisions
        if decision.get("state") not in {"broken", "disabled"}
    ]
    issues = _visibility_issue_groups(
        model,
        cwd_path,
        occurrences,
        declared_occurrences,
        visibility_decisions,
        shadowed,
    )
    # Surface rule provenance + origin + an exact fix_command on EVERY issue row
    # so each row is actionable without re-deriving anything from the policy.
    _enrich_issue_rows(issues)
    if include_sources:
        undefined_sources, source_roots = _undefined_source_skills(model, occurrences)
    else:
        undefined_sources, source_roots = [], []

    recommendations = _skill_visibility_recommendations(issues)
    beads = _beads_status_for_cwd(effective, cwd_path)
    policy_files = sorted({
        str(policy.get("_policy_path") or "")
        for policy in _operator_scope_policies(model)
        if str(policy.get("_policy_path") or "")
    })
    summary = _skill_visibility_summary(
        effective=effective,
        occurrences=occurrences,
        layers=layers,
        issues=issues,
        undefined_sources=undefined_sources,
        recommendations=recommendations,
    )
    summary["beads_required_skills"] = len(beads.get("required_skills") or [])
    summary["beads_issues"] = len(beads.get("issues") or [])
    # Claude<->Codex global skill-surface parity: the skill analogue of the MCP
    # audit's parity block. Only meaningful when global surfaces are in scope.
    parity = collect_skill_parity() if include_global else {}
    summary["parity_divergent"] = int((parity.get("summary") or {}).get("divergent") or 0)

    # Overlay-registry audit: overlay-state entries naming an UNDECLARED overlay
    # filter nothing and so fail silent. Surface them as AUDIT WARNINGS (never a
    # hard fail) with the declared registry so the operator can fix the typo or
    # declare the overlay. This is the skill-visibility analogue of the
    # overlay-declaration doctor lint (which guards rule `overlay:` tags).
    declared_overlay_names = sorted(declared_overlays(model))
    active_overlay_rows = active_overlay_records(cwd_path)
    undeclared_state = undeclared_active_overlays(model, cwd_path)
    overlay_audit = {
        "declared": declared_overlay_names,
        "active": sorted(active_overlays(cwd_path)),
        "active_layers": active_overlay_rows,
        "undeclared_active": undeclared_state,
        "warnings": [
            (
                f"overlay-state entry '{name}' is not a declared overlay; it "
                "filters nothing (no rule can match it). Declare it in "
                "skill-scope.yaml `overlays:` or remove it from the overlay state. "
                f"Declared overlays: {', '.join(declared_overlay_names) or '(none)'}."
            )
            for name in undeclared_state
        ],
    }
    summary["undeclared_active_overlays"] = len(undeclared_state)

    next_actions = skill_visibility_next_actions(issues)
    for action in beads.get("next_actions") or []:
        if action not in next_actions:
            next_actions.append(action)
    for action in skill_parity_next_actions(parity):
        if action not in next_actions:
            next_actions.append(action)
    for warning in overlay_audit["warnings"]:
        if warning not in next_actions:
            next_actions.append(warning)

    payload = {
        "cwd": str(cwd_path),
        "matched_clients": matched_skill_clients(model, cwd_path),
        "matched_project_categories": _matched_project_categories(model, cwd_path),
        "matched_scope_rules": _matched_scope_rules_for_cwd(model, cwd_path),
        "active_clients": model.get("active_clients") or [],
        "active_profiles": model.get("active_profiles") or [],
        "global_surfaces": global_home_surfaces_report() if include_global else [],
        "parity": parity,
        "layers": sorted(layers, key=lambda item: int(item.get("rank", 0))),
        "source_roots": sorted(source_roots, key=lambda item: str(item.get("path") or "")),
        "visibility_decisions": visibility_decisions,
        "effective": effective,
        "occurrences": occurrences,
        "undefined_sources": undefined_sources,
        "beads": beads,
        "issues": issues,
        "policy": {
            "files": policy_files,
            "project_categories": _project_categories(model),
        },
        "overlay_audit": overlay_audit,
        "recommendations": recommendations,
        "summary": summary,
        "next_actions": next_actions,
    }

    # OPTIONAL per-candidate evidence. The provider is best-effort: any failure
    # (Cass down, import error, timeout) is swallowed so candidate rows simply
    # carry no `evidence` and the recalibrate is never blocked.
    if evidence_provider is not None:
        try:
            evidence_index = evidence_provider()
        except Exception:
            evidence_index = None
        attach_skill_evidence(payload, evidence_index)

    return payload


SKILL_AUDIT_REPO_ISSUE_KEYS = (
    "broken_project",
    "scope_violations",
    "missing_for_cwd",
)

SKILL_AUDIT_GLOBAL_ISSUE_KEYS = (
    "broken_global",
    "global_not_allowed",
    "extra_global",
)


def _skill_audit_candidate_from_path(
    candidates: dict[str, dict[str, Any]],
    path: Any,
    *,
    source: str,
) -> None:
    raw = str(path or "").strip()
    if not raw:
        return
    # ``_expand_policy_path`` expands ~/$VARS and resolves links that EXIST on
    # this box. We additionally fold declared aliases (``/srv/repos`` ->
    # ``/srv/skillbox/repos``) by string prefix so the dedup is robust even when
    # the alias symlink is not resolvable here -- otherwise the same repo is
    # reported twice under its two names (the historic 2x-inflated fleet count).
    expanded = _expand_policy_path(raw)
    canonical = _canonicalize_repo_path(expanded)
    item = candidates.setdefault(canonical, {"path": canonical, "sources": [], "aliases": []})
    if source not in item["sources"]:
        item["sources"].append(source)
    # Record any alias spelling of this repo (the expanded-but-pre-canonical path
    # and the originally-declared ``raw``) so a triager can see every name the
    # repo answers to under its single canonical row.
    for alias in (expanded, raw):
        if alias and alias != canonical and alias not in item["aliases"]:
            item["aliases"].append(alias)


def _skill_audit_client_paths(
    candidates: dict[str, dict[str, Any]],
    model: dict[str, Any],
) -> None:
    for client in model.get("clients") or []:
        client_id = str(client.get("id") or "client")
        _skill_audit_candidate_from_path(
            candidates,
            client.get("default_cwd"),
            source=f"client:{client_id}:default_cwd",
        )
        context = client.get("context") or {}
        raw_matches = context.get("cwd_match") or []
        if isinstance(raw_matches, str):
            raw_matches = [raw_matches]
        for raw_match in raw_matches:
            _skill_audit_candidate_from_path(
                candidates,
                raw_match,
                source=f"client:{client_id}:cwd_match",
            )
        for repo in (client.get("repo_roots") or []) + (client.get("repos") or []):
            if isinstance(repo, dict):
                _skill_audit_candidate_from_path(
                    candidates,
                    repo.get("path"),
                    source=f"client:{client_id}:repo",
                )


def _skill_audit_category_paths(
    candidates: dict[str, dict[str, Any]],
    model: dict[str, Any],
) -> None:
    for category in _project_categories(model):
        category_id = str(category.get("id") or "category")
        for raw_path in category.get("paths") or []:
            _skill_audit_candidate_from_path(
                candidates,
                raw_path,
                source=f"category:{category_id}",
            )


def _git_repo_paths_under(root: Path, *, max_depth: int) -> list[Path]:
    if not root.is_dir():
        return []
    repos: list[Path] = []
    root = root.resolve()
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            dirname for dirname in dirnames
            if dirname not in SKILL_SOURCE_SCAN_SKIP_DIRS
        )
        current_path = Path(current)
        try:
            rel = current_path.relative_to(root)
        except ValueError:
            rel = Path()
        if len(rel.parts) > max_depth:
            dirnames[:] = []
            continue
        if ".git" in dirnames or ".git" in filenames:
            repos.append(current_path)
            dirnames[:] = []
    return repos


def _skill_audit_scan_root_paths(
    candidates: dict[str, dict[str, Any]],
    scan_roots: list[str] | None,
    model: dict[str, Any],
    *,
    max_depth: int,
) -> None:
    if scan_roots is None:
        roots = _configured_skill_audit_scan_roots(model)
    else:
        roots = _expand_skill_source_patterns(scan_roots)
    for root in roots:
        for repo_path in _git_repo_paths_under(root, max_depth=max(0, max_depth)):
            _skill_audit_candidate_from_path(
                candidates,
                str(repo_path),
                source=f"scan_root:{root}",
            )


def _skill_audit_candidate_paths(
    model: dict[str, Any],
    *,
    scan_roots: list[str] | None,
    max_depth: int,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    _skill_audit_category_paths(candidates, model)
    _skill_audit_client_paths(candidates, model)
    _skill_audit_scan_root_paths(candidates, scan_roots, model, max_depth=max_depth)
    return sorted(candidates.values(), key=lambda item: str(item.get("path") or ""))


def _skill_names(items: list[dict[str, Any]]) -> list[str]:
    return sorted({
        str(item.get("name") or "")
        for item in items
        if str(item.get("name") or "")
    })


def _skill_audit_issue_counts(
    issues: dict[str, list[dict[str, Any]]],
    keys: tuple[str, ...],
) -> dict[str, int]:
    return {key: len(issues.get(key) or []) for key in keys}


def _skill_audit_has_repo_issues(repo: dict[str, Any]) -> bool:
    return any(int(value) > 0 for value in (repo.get("issues") or {}).values())


def _classified_broken_rows(
    items: list[dict[str, Any]],
    *,
    issue_type: str = "broken_project",
) -> list[dict[str, Any]]:
    """Compact per-link taxonomy rows for the audit (one row per broken link).

    Each row carries the classification a triager acts on plus the rule
    provenance every audit row now guarantees: ``type``, ``name``, ``path``,
    ``rule_id``, ``policy_path``, ``origin``, ``suggested_action`` and the exact
    ``fix_command``. ``origin`` defaults to ``dangling`` if an item was never
    enriched, mirroring :func:`broken_link_class_counts`. ``rule_id`` /
    ``policy_path`` are usually ``None`` for broken links (a dead symlink has no
    matched scope rule) but are emitted for a stable, self-describing schema.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                # Use the caller's group as the authoritative type: a single
                # occurrence dict can be a member of more than one issue group
                # (e.g. global_not_allowed AND extra_global share the same dict),
                # so the in-place ``type`` stamp is last-writer-wins and unsafe
                # to trust here.
                "type": issue_type,
                "name": str(item.get("name") or ""),
                "path": str(item.get("path") or ""),
                "link_target": str(item.get("link_target") or ""),
                "rule_id": item.get("rule_id") if item.get("rule_id") is not None else item.get("scope_rule"),
                "policy_path": item.get("policy_path") if item.get("policy_path") is not None else item.get("scope_policy_path"),
                "origin": str(item.get("origin") or "dangling"),
                "suggested_action": str(
                    item.get("suggested_action")
                    or BROKEN_LINK_ACTIONS.get(str(item.get("origin") or "dangling"), "prune")
                ),
                "fix_command": str(item.get("fix_command") or ""),
            }
        )
    # Stable order: group by class, then by path, so repeated migrations cluster.
    rows.sort(key=lambda row: (row["origin"], row["path"]))
    return rows


def _classified_issue_rows(
    items: list[dict[str, Any]],
    issue_type: str,
) -> list[dict[str, Any]]:
    """Provenance rows for a non-broken issue group in the fleet audit.

    The fleet audit historically flattened ``missing_for_cwd`` /
    ``scope_violations`` to a bare list of skill names (``_skill_names``), which
    threw away the rule id, policy path, and fix command — exactly the data a
    downstream agent needs. This keeps the compact name list AND emits a parallel
    rows list so ``--format json`` carries the full provenance for each row.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        # The caller's group is authoritative for both ``type`` and the derived
        # ``fix_command``: a shared occurrence dict (global_not_allowed AND
        # extra_global) has a last-writer-wins in-place ``type``/``fix_command``,
        # so re-derive from ``issue_type`` rather than trusting the stamp.
        rows.append(
            {
                "type": issue_type,
                "name": str(item.get("name") or ""),
                "path": str(item.get("path") or ""),
                "rule_id": item.get("rule_id") if item.get("rule_id") is not None else item.get("scope_rule"),
                "policy_path": item.get("policy_path") if item.get("policy_path") is not None else item.get("scope_policy_path"),
                "origin": item.get("origin"),
                "fix_command": _issue_row_fix_command(issue_type, item),
            }
        )
    rows.sort(key=lambda row: (row["name"], row["path"]))
    return rows


def _skill_audit_repo_row(
    candidate: dict[str, Any],
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "path": candidate["path"],
        # Alias spellings the canonical repo also answers to (e.g.
        # ``/srv/repos/<name>`` for ``/srv/skillbox/repos/<name>``). Empty when
        # the repo was only ever named by its canonical path.
        "aliases": sorted(candidate.get("aliases") or []),
        "sources": sorted(candidate.get("sources") or []),
    }
    path = Path(str(candidate["path"]))
    if not path.is_dir():
        row["state"] = "missing"
        row["issues"] = {"missing_repo": 1}
        return row
    if payload is None:
        row["state"] = "error"
        row["issues"] = {"error": 1}
        return row

    issues = payload.get("issues") or {}
    row.update({
        "state": "ok",
        "matched_clients": [
            str(item.get("id") or "")
            for item in payload.get("matched_clients") or []
            if str(item.get("id") or "")
        ],
        "categories": [
            str(item.get("id") or "")
            for item in payload.get("matched_project_categories") or []
            if str(item.get("id") or "")
        ],
        "matched_scope_rules": [
            str(item.get("id") or "")
            for item in payload.get("matched_scope_rules") or []
            if str(item.get("id") or "")
        ],
        "issues": _skill_audit_issue_counts(issues, SKILL_AUDIT_REPO_ISSUE_KEYS),
        # Compact name lists (back-compat for human/summary surfaces) ...
        "missing_for_cwd": _skill_names(issues.get("missing_for_cwd") or []),
        "scope_violations": _skill_names(issues.get("scope_violations") or []),
        "broken_project": _skill_names(issues.get("broken_project") or []),
        # ... plus the full provenance rows (rule_id/policy_path/origin/fix_command)
        # so every audit row is actionable without re-deriving from the policy.
        "missing_for_cwd_rows": _classified_issue_rows(
            issues.get("missing_for_cwd") or [], "missing_for_cwd"
        ),
        "scope_violation_rows": _classified_issue_rows(
            issues.get("scope_violations") or [], "scope_violations"
        ),
        # Per-link triage taxonomy + counts-by-class so the audit answers
        # "what kind of broken is this" (relink / prune / migrate / investigate)
        # rather than re-listing N undifferentiated names.
        "broken_project_links": _classified_broken_rows(
            issues.get("broken_project") or [], issue_type="broken_project"
        ),
        "broken_project_by_class": broken_link_class_counts(issues.get("broken_project") or []),
    })
    return row


def _skill_audit_global_row(model: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    payload = collect_skill_visibility(
        model,
        cwd=cwd,
        include_global=True,
        include_project=False,
        include_sources=False,
    )
    issues = payload.get("issues") or {}
    return {
        "issues": _skill_audit_issue_counts(issues, SKILL_AUDIT_GLOBAL_ISSUE_KEYS),
        "broken_global": _skill_names(issues.get("broken_global") or []),
        "global_not_allowed": _skill_names(issues.get("global_not_allowed") or []),
        "extra_global": _skill_names(issues.get("extra_global") or []),
        # Full provenance rows mirror the per-repo audit so the global block also
        # carries rule_id/policy_path/origin/fix_command on every issue row.
        "global_not_allowed_rows": _classified_issue_rows(
            issues.get("global_not_allowed") or [], "global_not_allowed"
        ),
        "extra_global_rows": _classified_issue_rows(
            issues.get("extra_global") or [], "extra_global"
        ),
        "broken_global_links": _classified_broken_rows(
            issues.get("broken_global") or [], issue_type="broken_global"
        ),
        "broken_global_by_class": broken_link_class_counts(issues.get("broken_global") or []),
    }


def _available_skill_overlays(model: dict[str, Any]) -> list[str]:
    overlays: set[str] = set()
    for policy in _operator_scope_policies(model):
        for raw_rule in policy.get("rules") or []:
            if not isinstance(raw_rule, dict):
                continue
            overlay = str(raw_rule.get("overlay") or "").strip()
            if overlay:
                overlays.add(overlay)
    return sorted(overlays)


def _skill_audit_next_actions(
    repos_with_issues: list[dict[str, Any]],
    global_row: dict[str, Any] | None,
    overlays: list[str],
    active: list[str],
) -> list[str]:
    actions: list[str] = []
    first_missing = next(
        (repo for repo in repos_with_issues if repo.get("missing_for_cwd")),
        None,
    )
    if first_missing:
        actions.append(f"manage.py skill sync --cwd {first_missing['path']} --dry-run")
    first_prune = next(
        (
            repo for repo in repos_with_issues
            if repo.get("scope_violations") or repo.get("broken_project")
        ),
        None,
    )
    if first_prune:
        actions.append(f"manage.py skill prune --cwd {first_prune['path']} --from project --dry-run")
    if global_row and any(int(value) > 0 for value in (global_row.get("issues") or {}).values()):
        actions.append("manage.py skill prune --from global --dry-run")
    inactive_overlays = [overlay for overlay in overlays if overlay not in active]
    if inactive_overlays:
        overlay = inactive_overlays[0]
        actions.append(f"manage.py overlay activate {overlay} --cwd <repo>")
        actions.append(f"manage.py overlay on {overlay}")
    if not actions:
        actions.append("manage.py skills --issues-only")
    return actions


def collect_skill_audit(
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    scan_roots: list[str] | None = None,
    max_depth: int = 3,
    include_global: bool = True,
    include_clean: bool = False,
) -> dict[str, Any]:
    """Collect a compact cross-repo skill policy audit."""
    candidates = _skill_audit_candidate_paths(
        model,
        scan_roots=scan_roots,
        max_depth=max_depth,
    )
    repos: list[dict[str, Any]] = []
    for candidate in candidates:
        path = Path(str(candidate["path"]))
        payload = None
        if path.is_dir():
            payload = collect_skill_visibility(
                model,
                cwd=str(path),
                include_global=False,
                include_project=True,
                include_sources=False,
            )
        row = _skill_audit_repo_row(candidate, payload)
        if include_clean or _skill_audit_has_repo_issues(row):
            repos.append(row)

    global_row = _skill_audit_global_row(model, cwd) if include_global else None
    # Claude<->Codex global skill-surface parity for the cross-repo audit, mirrored
    # from the per-cwd visibility payload so `sbp skills audit --format json` shows
    # the same parity block as `sbp skills`.
    parity = collect_skill_parity() if include_global else {}
    overlays = _available_skill_overlays(model)
    active = sorted(active_overlays())
    repos_with_issues = [repo for repo in repos if _skill_audit_has_repo_issues(repo)]

    issue_totals = {key: 0 for key in SKILL_AUDIT_REPO_ISSUE_KEYS}
    missing_repos = 0
    # Fleet-wide broken-link counts-by-class: turns "N broken links" into the
    # ~3 decisions they actually are (relink / prune / migrate / investigate).
    broken_by_class = {origin: 0 for origin in BROKEN_LINK_CLASSES}
    for repo in repos:
        if repo.get("state") == "missing":
            missing_repos += 1
        for key in SKILL_AUDIT_REPO_ISSUE_KEYS:
            issue_totals[key] += int((repo.get("issues") or {}).get(key) or 0)
        for origin, count in (repo.get("broken_project_by_class") or {}).items():
            if origin in broken_by_class:
                broken_by_class[origin] += int(count or 0)
    for origin, count in ((global_row or {}).get("broken_global_by_class") or {}).items():
        if origin in broken_by_class:
            broken_by_class[origin] += int(count or 0)

    return {
        "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "scan_roots": [
            str(root)
            for root in (
                _configured_skill_audit_scan_roots(model)
                if scan_roots is None
                else _expand_skill_source_patterns(scan_roots)
            )
        ],
        "max_depth": max_depth,
        "active_clients": model.get("active_clients") or [],
        "active_profiles": model.get("active_profiles") or [],
        "overlays": {"available": overlays, "active": active},
        "summary": {
            "candidate_repos": len(candidates),
            "reported_repos": len(repos),
            "repos_with_issues": len(repos_with_issues),
            "missing_repos": missing_repos,
            **issue_totals,
            "global_not_allowed": int((global_row or {}).get("issues", {}).get("global_not_allowed") or 0),
            "extra_global": int((global_row or {}).get("issues", {}).get("extra_global") or 0),
            "broken_links": sum(broken_by_class.values()),
            "broken_by_class": broken_by_class,
            "parity_divergent": int((parity.get("summary") or {}).get("divergent") or 0),
        },
        "global": global_row,
        "parity": parity,
        "repos": repos,
        "next_actions": [
            *_skill_audit_next_actions(repos_with_issues, global_row, overlays, active),
            *skill_parity_next_actions(parity),
        ],
    }


SKILL_VISIBILITY_COMPACT_ISSUE_KEYS = (
    "broken_global",
    "broken_project",
    "global_not_allowed",
    "extra_global",
    "shadowed",
    "archive_sources",
    "scope_violations",
    "missing_for_cwd",
)


def _compact_skill_visibility_skill(item: dict[str, Any]) -> dict[str, Any]:
    result = {
        "name": item.get("name"),
        "availability": item.get("availability"),
        "layer": item.get("layer"),
        "winning_layer": item.get("winning_layer"),
        "state": item.get("state"),
        "source_bucket": item.get("source_bucket"),
        "source": item.get("source"),
        "shadowed_count": item.get("shadowed_count", 0),
    }
    if item.get("path"):
        result["path"] = item.get("path")
    compacted = {key: value for key, value in result.items() if value not in (None, "")}
    # Preserve the OPTIONAL evidence annotation through compaction so
    # `sbp candidates --json` (which may not pass --full) still carries it.
    if isinstance(item.get("evidence"), dict) and item.get("evidence"):
        compacted["evidence"] = item["evidence"]
    return compacted


def _compact_skill_visibility_issues(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    issues = payload.get("issues") or {}
    return {key: issues.get(key) or [] for key in SKILL_VISIBILITY_COMPACT_ISSUE_KEYS}


def compact_skill_visibility_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the agent-facing subset of a full skill visibility payload."""

    return {
        "cwd": payload.get("cwd"),
        "active_clients": payload.get("active_clients") or [],
        "active_profiles": payload.get("active_profiles") or [],
        "matched_clients": payload.get("matched_clients") or [],
        "matched_project_categories": payload.get("matched_project_categories") or [],
        "matched_scope_rules": payload.get("matched_scope_rules") or [],
        "summary": payload.get("summary") or {},
        # Claude<->Codex global skill-surface parity travels in the agent-facing
        # compact payload too, so `sbp skills --format json | jq '.parity'`
        # surfaces drift without needing --full.
        "parity": payload.get("parity") or {},
        "visibility_decisions": [
            _compact_skill_visibility_skill(item)
            for item in payload.get("visibility_decisions") or []
        ],
        "effective": [_compact_skill_visibility_skill(item) for item in payload.get("effective") or []],
        "issues": _compact_skill_visibility_issues(payload),
        "beads": payload.get("beads") or {},
        "recommendations": payload.get("recommendations") or [],
        "policy": payload.get("policy") or {},
        "source_roots": payload.get("source_roots") or [],
        "undefined_sources": payload.get("undefined_sources") or [],
        "next_actions": payload.get("next_actions") or [],
    }


EXPLAIN_SCHEMA_VERSION = "2026-06-25+skill_explain_layers"

# Maps an occurrence ``layer`` id (e.g. ``default``, ``client:foo``,
# ``global:claude``, ``project:codex:/repo``) to one of the four ranking
# families the policy ladder is built on (DEFAULT_LAYER_RANK ..
# PROJECT_LAYER_RANK, near the top of this module). PROJECT wins over GLOBAL
# wins over CLIENT wins over DEFAULT.
def _layer_family(occurrence: dict[str, Any]) -> str:
    layer = str(occurrence.get("layer") or "")
    rank = int(occurrence.get("layer_rank") or 0)
    if layer.startswith("repo-override-file") or rank == REPO_OVERRIDE_LAYER_RANK:
        return "OVERRIDE"
    if layer.startswith("project:") or rank >= PROJECT_LAYER_RANK:
        return "PROJECT"
    if layer.startswith("global:") or rank == GLOBAL_LAYER_RANK:
        return "GLOBAL"
    if layer.startswith("client:") or layer.startswith("skillset:") or rank == CLIENT_LAYER_RANK or rank == CLIENT_LAYER_RANK - 1:
        return "CLIENT"
    return "DEFAULT"


def _explain_occurrence_view(occurrence: dict[str, Any], *, won: bool) -> dict[str, Any]:
    """Trim a raw occurrence to the provenance-relevant fields plus a verdict."""
    view = {
        "layer": occurrence.get("layer"),
        "layer_label": occurrence.get("layer_label"),
        "layer_rank": occurrence.get("layer_rank"),
        "layer_family": _layer_family(occurrence),
        "availability": occurrence.get("availability"),
        "state": occurrence.get("state"),
        "source": occurrence.get("source"),
        "source_bucket": occurrence.get("source_bucket"),
        "path": occurrence.get("path"),
        "override_action": occurrence.get("override_action"),
        "policy_path": occurrence.get("policy_path"),
        "won": won,
        "wins": won,
    }
    return (
        {key: value for key, value in view.items() if value not in (None, "")}
        | {"won": won, "wins": won}
    )


def _explain_vetoed_floor_views(payload: dict[str, Any], skill_name: str) -> list[dict[str, Any]]:
    """Repo override attempts vetoed by the dispatcher floor.

    Floor vetoes intentionally do not become occurrences, because the canonical
    resolver must ignore them. The explain trace still needs to show the
    touched layer so agents can see why a local pin_off did not win.
    """
    rows: list[dict[str, Any]] = []
    for layer in payload.get("layers") or []:
        if str(layer.get("id") or "") != "repo-override-file":
            continue
        if skill_name not in {str(name) for name in layer.get("vetoed_floor") or []}:
            continue
        rows.append({
            "layer": layer.get("id"),
            "layer_label": layer.get("label"),
            "layer_rank": layer.get("rank"),
            "layer_family": "OVERRIDE",
            "availability": "override",
            "state": "vetoed_floor",
            "path": layer.get("path"),
            "override_action": "pin_off_or_opt_out_global",
            "lost_reason": "dispatcher floor skills cannot be disabled by repo overrides",
            "won": False,
            "wins": False,
        })
    return rows


def _explain_lost_reason(
    winner: dict[str, Any] | None,
    loser: dict[str, Any],
) -> str:
    """Why this occurrence is NOT the effective one."""
    if loser.get("state") == "broken":
        return "broken link (source target does not resolve here)"
    if winner is None:
        return "no effective occurrence for this skill"
    if winner.get("state") == "disabled":
        return f"disabled by higher-precedence override layer ({winner.get('layer')})"
    winner_rank = int(winner.get("layer_rank") or 0)
    loser_rank = int(loser.get("layer_rank") or 0)
    if _same_source(winner, loser):
        return "same source as the effective occurrence (duplicate, not a material shadow)"
    if loser_rank < winner_rank:
        return (
            f"shadowed: lower layer ({_layer_family(loser)}, rank {loser_rank}) "
            f"loses to {_layer_family(winner)} (rank {winner_rank})"
        )
    if loser_rank == winner_rank:
        return "same layer rank; lost the surface tie-break (claude/codex ordering)"
    return "lower precedence than the effective occurrence"


def _explain_inactive_overlay_rules(
    model: dict[str, Any],
    skill_name: str,
    cwd_path: Path,
) -> list[dict[str, Any]]:
    """Overlay-gated rules that WOULD match this skill+cwd if the overlay were on.

    ``_scope_rules`` (and therefore ``_explain_scope_rules``) filters out rules
    whose ``overlay`` is not in ``active_overlays`` — so an invisible skill that
    is only gated by an inactive overlay would otherwise show "no rule matches".
    This walks the raw policies and re-materializes each overlay-gated rule with
    its own overlay forced on, so the explanation can point at the exact overlay
    to flip. The active set is never mutated.
    """
    found: list[dict[str, Any]] = []
    overlays_on = active_overlays(cwd_path)
    for policy in _operator_scope_policies(model):
        categories = _policy_categories_by_id(policy)
        for index, raw_rule in enumerate(policy.get("rules") or []):
            if not isinstance(raw_rule, dict):
                continue
            overlay = str(raw_rule.get("overlay") or "").strip()
            if not overlay or overlay in overlays_on:
                continue
            rule = _scope_rule_from_raw(
                raw_rule,
                index=index,
                policy=policy,
                categories=categories,
                overlays_on=overlays_on | {overlay},
            )
            if rule is None:
                continue
            if not any(
                fnmatch.fnmatchcase(skill_name, str(pattern))
                for pattern in rule.get("patterns") or []
            ):
                continue
            paths = list(rule.get("paths") or [])
            matched_paths = [path for path in paths if _path_prefix_matches(cwd_path, path)]
            if not matched_paths and paths:
                continue
            found.append({
                "id": rule.get("id"),
                "policy_path": rule.get("policy_path"),
                "overlay": overlay,
                "matched_paths": matched_paths,
            })
    return found


def _explain_scope_rules(model: dict[str, Any], skill_name: str, cwd_path: Path) -> list[dict[str, Any]]:
    """Every scope rule whose pattern matches this skill, with cwd verdict.

    Reuses the same ``_scope_rules`` / pattern-matching machinery the resolver
    uses, so the explanation never drifts from the evaluator. Each entry carries
    the rule id, source policy path, the pattern that matched, whether the rule
    is overlay-gated, and whether the rule actually matches ``cwd``.
    """
    rules: list[dict[str, Any]] = []
    for rule in _scope_rules(model, cwd=cwd_path):
        matched_pattern = next(
            (
                pattern
                for pattern in rule.get("patterns") or []
                if fnmatch.fnmatchcase(skill_name, str(pattern))
            ),
            None,
        )
        if matched_pattern is None:
            continue
        paths = list(rule.get("paths") or [])
        matched_paths = [path for path in paths if _path_prefix_matches(cwd_path, path)]
        rules.append({
            "id": rule.get("id"),
            "policy_path": rule.get("policy_path"),
            "matched_pattern": matched_pattern,
            "overlay": rule.get("overlay") or None,
            "allow_global": bool(rule.get("allow_global")),
            "categories": list(rule.get("categories") or []),
            "allowed_paths": paths,
            "matched_paths": matched_paths,
            "matches_cwd": bool(matched_paths),
            "expected_by_default": _scope_rule_is_expected_by_default(rule),
        })
    # cwd-matching rules first, then by id for stable output.
    return sorted(rules, key=lambda item: (not item["matches_cwd"], str(item.get("id") or "")))


def _explain_machine_profile() -> dict[str, Any]:
    """Forward-compatible machine-profile resolution for the explanation.

    Resolution flows through ``runtime_manager.machines`` (the same profile API
    used elsewhere) so a path like ``/srv/repos/...`` vs ``/Users/operator/repos/...``
    can be reasoned about. Best-effort: a missing/unparseable machines.yaml
    yields a ``resolved: false`` stub rather than raising, because skill
    provenance must answer even on boxes that have not declared a profile.
    """
    stub: dict[str, Any] = {"resolved": False, "machine_id": None, "source_path": None}
    try:
        from . import machines as _machines
    except Exception:  # pragma: no cover - import guard
        return stub
    try:
        config = _machines.load_machines_config()
    except Exception:
        return stub
    machine_id = None
    try:
        machine_id = config.detect_machine_id()
    except Exception:
        machine_id = None
    return {
        "resolved": machine_id is not None,
        "machine_id": machine_id,
        "source_path": config.source_path,
        "declared_machines": sorted(config.machines),
    }


def _explain_source_options(model: dict[str, Any], skill_name: str) -> list[dict[str, Any]]:
    """Source dirs that could supply this skill (drives the activate command)."""
    try:
        options = _skill_source_options(model, skill_name)
    except RuntimeError:
        return []
    return [
        {
            "source": option.get("source"),
            "source_bucket": option.get("source_bucket"),
        }
        for option in options
    ]


def _explain_remediation(
    model: dict[str, Any],
    skill_name: str,
    cwd_path: Path,
    *,
    scope_rules: list[dict[str, Any]],
    inactive_overlay_rules: list[dict[str, Any]],
    source_options: list[dict[str, Any]],
    occurrences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ranked narrowest-path-to-visibility steps, each with an EXACT command.

    Ranking is narrowest-first:
      1. activate (a source exists -> one symlink makes it visible now),
      2. overlay flip (an overlay-gated rule matches this cwd),
      3. rule edit (the skill has no cwd-matching rule -> scope edit needed),
      4. source restore (no source anywhere -> declare/restore the skill first).
    """
    remediation: list[dict[str, Any]] = []
    cwd = str(cwd_path)

    overlay_rules = [rule for rule in scope_rules if rule.get("overlay")] + list(
        inactive_overlay_rules
    )
    cwd_rules = [rule for rule in scope_rules if rule.get("matches_cwd")]
    cwd_rules.extend(rule for rule in inactive_overlay_rules if rule.get("matched_paths"))
    broken_here = [
        item for item in occurrences
        if item.get("state") == "broken" and str(item.get("availability")) == "installed"
    ]

    if source_options:
        # A real source exists: the narrowest durable fix is a scoped `on`,
        # which pins it for this repo, links it, and returns the SKILL.md packet.
        remediation.append({
            "rank": 1,
            "kind": "on",
            "command": f"sbp skill on {skill_name} --cwd $PWD",
            "resolved_command": f"sbp skill on {skill_name} --cwd {cwd}",
            "manage_command": (
                f"python3 .env-manager/manage.py skill on {skill_name} --cwd {cwd}"
            ),
            "why": (
                f"a source for {skill_name!r} exists ({source_options[0].get('source')}); "
                "turning it on pins it for this repo, links it, and returns the SKILL.md packet"
            ),
        })

    seen_overlays: set[str] = set()
    for rule in overlay_rules:
        overlay = str(rule.get("overlay") or "")
        if not overlay or overlay in seen_overlays:
            continue
        seen_overlays.add(overlay)
        remediation.append({
            "rank": 2,
            "kind": "overlay_flip",
            "command": f"sbp overlay activate {overlay} --cwd {cwd}",
            "manage_command": (
                f"python3 .env-manager/manage.py overlay activate {overlay} --cwd {cwd}"
            ),
            "why": (
                f"rule {rule.get('id')!r} that would expect {skill_name!r} here is gated by "
                f"overlay {overlay!r}; activate it (ephemerally) to apply the rule for this cwd"
            ),
        })

    if not cwd_rules:
        # No scope rule pins this skill to this cwd: a policy edit is required
        # before the resolver will treat it as expected here.
        policy_files = sorted({
            str(policy.get("_policy_path") or "")
            for policy in _operator_scope_policies(model)
            if str(policy.get("_policy_path") or "")
        })
        remediation.append({
            "rank": 3,
            "kind": "rule_edit",
            "command": (
                f"edit skill-scope.yaml: add a rule with skills:[{skill_name}] "
                f"and a path/category covering {cwd}"
            ),
            "policy_files": policy_files,
            "why": (
                f"no skill-scope rule currently matches {skill_name!r} for this cwd, so the "
                "resolver does not consider it in-scope here"
            ),
        })

    if not source_options and not broken_here:
        remediation.append({
            "rank": 4,
            "kind": "source_restore",
            "command": (
                f"restore or declare a source for {skill_name!r} (e.g. add it to the active "
                "client's skill-repos.yaml or create skills-private/<name>/SKILL.md)"
            ),
            "why": (
                f"no source directory for {skill_name!r} was found under any configured source "
                "root; there is nothing to link until a source exists"
            ),
        })
    elif broken_here:
        for item in broken_here:
            remediation.append({
                "rank": 4,
                "kind": "source_restore",
                "command": (
                    f"sbp skill prune --cwd {cwd}  # then re-activate; broken link at "
                    f"{item.get('path')}"
                ),
                "manage_command": (
                    f"python3 .env-manager/manage.py skill prune --cwd {cwd}"
                ),
                "why": (
                    f"an installed link for {skill_name!r} at {item.get('path')} is broken "
                    f"(target {item.get('source')} does not resolve here); prune it, then activate"
                ),
            })

    return sorted(remediation, key=lambda item: (int(item.get("rank", 9)), str(item.get("kind"))))


def explain_skill_visibility(
    model: dict[str, Any],
    skill_name: str,
    *,
    cwd: str | None = None,
    include_global: bool = True,
    include_project: bool = True,
) -> dict[str, Any]:
    """Full provenance for ONE skill at ONE cwd.

    Answers, reusing the same machinery ``collect_skill_visibility`` uses (no
    parallel evaluator):

    * IS it visible here, via which occurrence / layer family?
    * Which scope rule(s) matched (rule id + policy source + matched pattern)?
    * Which occurrence(s) LOST and why (lower layer / broken / not in sources)?
    * When NOT visible: the ranked, narrowest path to visibility with the EXACT
      command to run for each option.

    Returns a structured dict. Forward-compatible ``machine`` and ``registry``
    blocks are always present so registry-id and machine-routing consumers can
    grow without a schema break.
    """
    skill_name = str(skill_name or "").strip()
    cwd_path = Path(cwd or os.getcwd()).resolve()
    payload = collect_skill_visibility(
        model,
        cwd=str(cwd_path),
        include_global=include_global,
        include_project=include_project,
        include_sources=True,
    )

    occurrences = [
        item for item in payload.get("occurrences") or []
        if str(item.get("name") or "") == skill_name
    ]
    winner = next(
        (
            item for item in payload.get("visibility_decisions") or []
            if str(item.get("name") or "") == skill_name
        ),
        None,
    )
    visible = bool(winner) and winner.get("state") not in {"broken", "disabled"}

    scope_rules = _explain_scope_rules(model, skill_name, cwd_path)
    inactive_overlay_rules = _explain_inactive_overlay_rules(model, skill_name, cwd_path)
    source_options = _explain_source_options(model, skill_name)

    occurrence_views: list[dict[str, Any]] = []
    losers: list[dict[str, Any]] = []
    winner_path = str(winner.get("path") or "") if winner else ""
    winner_layer = str(winner.get("layer") or "") if winner else ""
    for item in sorted(
        occurrences,
        key=lambda occ: (-int(occ.get("layer_rank") or 0), str(occ.get("layer") or "")),
    ):
        is_winner = bool(
            winner
            and str(item.get("layer") or "") == winner_layer
            and str(item.get("path") or "") == winner_path
            and item.get("availability") == winner.get("availability")
        )
        view = _explain_occurrence_view(item, won=is_winner)
        if not is_winner:
            view["lost_reason"] = _explain_lost_reason(winner, item)
            losers.append(view)
        occurrence_views.append(view)
    layer_trace = [
        *occurrence_views,
        *_explain_vetoed_floor_views(payload, skill_name),
    ]

    if visible:
        reason = (
            f"{skill_name!r} IS visible at cwd via the {_layer_family(winner)} layer "
            f"({winner.get('layer')})"
        )
        remediation: list[dict[str, Any]] = []
    else:
        if not occurrences and not source_options:
            reason = (
                f"{skill_name!r} is NOT visible: no occurrence and no source found under any "
                "configured root (unknown skill or removed source)"
            )
        elif winner and winner.get("broken_reason") == "override_source_missing":
            reason = (
                f"{skill_name!r} is NOT visible: repo override pins it on, "
                "but no installed occurrence or source was found"
            )
        elif winner and winner.get("state") == "broken":
            reason = (
                f"{skill_name!r} is NOT visible: the only occurrence is a broken link "
                f"({winner.get('path')})"
            )
        elif winner and winner.get("state") == "disabled":
            reason = (
                f"{skill_name!r} is NOT visible: disabled by the "
                f"{_layer_family(winner)} layer ({winner.get('layer')})"
            )
        elif source_options:
            reason = (
                f"{skill_name!r} is NOT visible here, but a source exists and it can be activated"
            )
        else:
            reason = f"{skill_name!r} is NOT visible at this cwd"
        remediation = _explain_remediation(
            model,
            skill_name,
            cwd_path,
            scope_rules=scope_rules,
            inactive_overlay_rules=inactive_overlay_rules,
            source_options=source_options,
            occurrences=occurrences,
        )

    return {
        "schema_version": EXPLAIN_SCHEMA_VERSION,
        "skill": skill_name,
        "cwd": str(cwd_path),
        "visible": visible,
        "reason": reason,
        "layer": winner.get("layer") if winner else None,
        "winning_layer": winner.get("winning_layer") if winner else None,
        "layer_family": _layer_family(winner) if winner else None,
        "layer_label": winner.get("layer_label") if winner else None,
        "layer_rank": winner.get("layer_rank") if winner else None,
        "winner": _explain_occurrence_view(winner, won=True) if winner else None,
        "layers": layer_trace,
        "occurrences": occurrence_views,
        "lost": losers,
        "scope_rules": scope_rules,
        "inactive_overlay_rules": inactive_overlay_rules,
        "source_options": source_options,
        "active_overlays": sorted(active_overlays(cwd_path)),
        "active_clients": payload.get("active_clients") or [],
        "matched_clients": payload.get("matched_clients") or [],
        "matched_project_categories": payload.get("matched_project_categories") or [],
        "remediation": remediation,
        # Forward-compatible blocks: present (and stable-keyed) even when empty
        # so machine-routing and registry-id consumers can extend without a
        # schema break.
        "machine": _explain_machine_profile(),
        "registry": {"skill_id": None, "registry_ids": []},
        "next_actions": [
            str(step.get("command"))
            for step in remediation
            if step.get("command")
        ] or (["already visible; no action needed"] if visible else []),
    }


def skill_visibility_next_actions(issues: dict[str, list[dict[str, Any]]]) -> list[str]:
    actions: list[str] = []
    if issues.get("broken_global"):
        actions.append("review broken global links, then prune intentionally")
    if issues.get("broken_project"):
        actions.append("repair or unlink broken project-local skill links")
    if issues.get("global_not_allowed"):
        actions.append("prune user-global skills down to the configured allowlist")
    if issues.get("extra_global"):
        actions.append("declare extra global skills in skill-repos.yaml or unlink them")
    if issues.get("archive_sources"):
        actions.append("copy useful archive-sourced skills into skills-private, then repoint")
    if issues.get("scope_violations"):
        actions.append("unlink or move skills installed outside their declared scope")
    if issues.get("missing_for_cwd"):
        actions.append("add missing cwd-scoped skills to the active client or project skill-repos.yaml")
    if not actions:
        actions.append("doctor --format json")
    return actions


def _print_visibility_header(payload: dict[str, Any], summary: dict[str, Any]) -> None:
    active_clients = ", ".join(payload.get("active_clients") or []) or "(none)"
    active_profiles = ", ".join(payload.get("active_profiles") or []) or "(none)"
    matched = ", ".join(
        f"{item['id']}@{item['match']}" for item in payload.get("matched_clients") or []
    ) or "(none)"
    undefined_count = summary.get("undefined_sources", 0)
    undefined_detail = f", {undefined_count} undefined/not synced" if undefined_count else ""
    print(
        f"skills: {summary.get('effective', 0)} effective, "
        f"{summary.get('occurrences', 0)} occurrences{undefined_detail}"
    )
    print(f"cwd: {payload.get('cwd')}")
    print(f"active: clients={active_clients} profiles={active_profiles}")
    print(f"pwd match: {matched}")
    categories = ", ".join(
        str(item.get("id") or "") for item in payload.get("matched_project_categories") or []
    ) or "(none)"
    print(f"project categories: {categories}")
    parity = payload.get("parity") or {}
    if parity.get("claude_only") or parity.get("codex_only"):
        print(
            "skill parity: "
            f"claude_only={_join_or_none(parity.get('claude_only') or [])} "
            f"codex_only={_join_or_none(parity.get('codex_only') or [])}"
        )
    elif parity:
        print("skill parity: in sync (claude == codex global surfaces)")
    beads = payload.get("beads") or {}
    if beads.get("required"):
        required = ", ".join(
            str(item.get("name") or "") for item in beads.get("required_skills") or []
        ) or "(none)"
        initialized = "yes" if beads.get("initialized") else "no"
        br_ready = "yes" if beads.get("br") else "no"
        repo_root = beads.get("repo_root") or "(no git repo)"
        print(
            f"beads: required by {required}; repo={repo_root}; "
            f"initialized={initialized}; br={br_ready}"
        )
        for issue in beads.get("issues") or []:
            print(f"  - {issue.get('message')}")
            if issue.get("hint"):
                print(f"    next: {issue.get('hint')}")


def _layer_detail(layer: dict[str, Any]) -> str:
    detail = f"{layer.get('skill_count', 0)} skills"
    if layer.get("kind") == "declared":
        detail += f", {layer.get('healthy_targets', 0)}/{layer.get('target_count', 0)} targets healthy"
        if layer.get("config_error"):
            detail += ", config error"
        if layer.get("lock_error"):
            detail += ", lock error"
        return detail
    if not layer.get("present"):
        detail += ", missing"
    if layer.get("broken_count"):
        detail += f", {layer.get('broken_count')} broken"
    return detail


def _print_visibility_layers(payload: dict[str, Any]) -> None:
    print("layers:")
    for layer in payload.get("layers") or []:
        print(f"  - {layer.get('id')}: {_layer_detail(layer)}")


def _issue_count_total(summary: dict[str, Any]) -> int:
    return (
        summary.get("broken_global", 0)
        + summary.get("broken_project", 0)
        + summary.get("global_not_allowed", 0)
        + summary.get("extra_global", 0)
        + summary.get("archive_sources", 0)
        + summary.get("scope_violations", 0)
        + summary.get("missing_for_cwd", 0)
    )


def _print_visibility_issues(summary: dict[str, Any]) -> None:
    if not (_issue_count_total(summary) or summary.get("shadowed", 0)):
        return
    print("issues:")
    print(
        "  - broken_global: "
        f"{summary.get('broken_global', 0)} links / {summary.get('broken_global_skills', 0)} skills"
    )
    if summary.get("broken_project", 0):
        print(
            "  - broken_project: "
            f"{summary.get('broken_project', 0)} links / {summary.get('broken_project_skills', 0)} skills"
        )
    if summary.get("global_not_allowed", 0):
        print(
            "  - global_not_allowed: "
            f"{summary.get('global_not_allowed', 0)} installs / "
            f"{summary.get('global_not_allowed_skills', 0)} skills"
        )
    print(
        "  - extra_global: "
        f"{summary.get('extra_global', 0)} links / {summary.get('extra_global_skills', 0)} skills"
    )
    print(f"  - shadowed: {summary.get('shadowed', 0)}")
    print(
        "  - archive_sources: "
        f"{summary.get('archive_sources', 0)} occurrences / {summary.get('archive_source_skills', 0)} skills"
    )
    if summary.get("scope_violations", 0):
        print(
            "  - scope_violations: "
            f"{summary.get('scope_violations', 0)} installs / "
            f"{summary.get('scope_violation_skills', 0)} skills"
        )
    if summary.get("missing_for_cwd", 0):
        print(
            "  - missing_for_cwd: "
            f"{summary.get('missing_for_cwd', 0)} rules / "
            f"{summary.get('missing_for_cwd_skills', 0)} skills"
        )


def _print_visibility_effective(payload: dict[str, Any], full: bool, limit: int) -> None:
    all_items = payload.get("effective") or []
    effective = all_items if full else all_items[: max(0, limit)]
    print("effective:")
    for item in effective:
        shadow = f" shadows={item.get('shadowed_count')}" if item.get("shadowed_count") else ""
        state = item.get("state") or item.get("availability")
        bucket = item.get("source_bucket") or "-"
        print(f"  - {item.get('name')}: {item.get('layer')} {state} {bucket}{shadow}")
    remaining = len(all_items) - len(effective)
    if remaining > 0:
        print(f"  ... {remaining} more (rerun with --full)")


def _print_visibility_shadowed(payload: dict[str, Any]) -> None:
    shadowed = payload.get("issues", {}).get("shadowed") or []
    if not shadowed:
        return
    print("shadowed:")
    for item in shadowed:
        layers = ", ".join(str(layer) for layer in item.get("shadowed_layers") or [])
        print(f"  - {item.get('name')}: winner={item.get('winner_layer')} hidden={layers}")


def _print_limited_issue_list(
    items: list[dict[str, Any]],
    header: str,
    formatter: Callable[[dict[str, Any]], str],
    limit: int,
    overflow_suffix: str,
) -> None:
    if not items:
        return
    print(f"{header}:")
    visible_count = min(len(items), max(0, limit))
    for item in items[:visible_count]:
        print(f"  - {formatter(item)}")
    remaining = len(items) - visible_count
    if remaining > 0:
        print(f"  ... {remaining} more {overflow_suffix}")


def _format_scope_violation(item: dict[str, Any]) -> str:
    allowed = ", ".join(str(path) for path in item.get("allowed_paths") or []) or "(none)"
    return (
        f"{item.get('name')}: {item.get('layer')} at {item.get('path')} "
        f"rule={item.get('scope_rule')} allowed={allowed}"
    )


def _format_global_not_allowed(item: dict[str, Any]) -> str:
    return f"{item.get('name')}: {item.get('layer')} at {item.get('path')}"


def _format_missing_for_cwd(item: dict[str, Any]) -> str:
    allowed = ", ".join(str(path) for path in item.get("allowed_paths") or []) or "(none)"
    categories = ", ".join(str(category) for category in item.get("categories") or []) or "(none)"
    return (
        f"{item.get('name')}: rule={item.get('scope_rule')} "
        f"categories={categories} allowed={allowed}"
    )


def _print_visibility_undefined(payload: dict[str, Any], full: bool, limit: int) -> None:
    undefined = payload.get("undefined_sources") or []
    if not undefined:
        return
    roots_count = len(payload.get("source_roots") or [])
    print(f"undefined / not synced ({len(undefined)} from {roots_count} source roots):")
    visible = undefined if full else undefined[: max(0, limit)]
    for item in visible:
        print(f"  - {item.get('name')}: {item.get('source_bucket')} {item.get('source')}")
    remaining = len(undefined) - len(visible)
    if remaining > 0:
        print(f"  ... {remaining} more undefined source skills (rerun with --full)")


def _print_visibility_next_actions(payload: dict[str, Any]) -> None:
    next_actions = payload.get("next_actions") or []
    if not next_actions:
        return
    print("next_actions:")
    for action in next_actions:
        print(f"  - {action}")


def _print_visibility_recommendations(payload: dict[str, Any], full: bool, limit: int) -> None:
    recommendations = payload.get("recommendations") or []
    if not recommendations:
        return
    print("recommendations:")
    visible = recommendations if full else recommendations[: max(0, limit)]
    for item in visible:
        skill = item.get("skill") or "-"
        print(f"  - {item.get('action')}: {skill} ({item.get('hint')})")
    remaining = len(recommendations) - len(visible)
    if remaining > 0:
        print(f"  ... {remaining} more recommendations")


def print_skill_visibility_text(
    payload: dict[str, Any],
    *,
    full: bool = False,
    show_shadowed: bool = False,
    issues_only: bool = False,
    limit: int = 80,
) -> None:
    summary = payload.get("summary") or {}
    _print_visibility_header(payload, summary)

    if not issues_only:
        _print_visibility_layers(payload)

    _print_visibility_issues(summary)

    if not issues_only:
        _print_visibility_effective(payload, full, limit)

    if show_shadowed:
        _print_visibility_shadowed(payload)

    issues = payload.get("issues", {})
    if show_shadowed or full:
        _print_limited_issue_list(
            issues.get("scope_violations") or [],
            "scope_violations",
            _format_scope_violation,
            limit,
            "scope violations",
        )
        _print_limited_issue_list(
            issues.get("global_not_allowed") or [],
            "global_not_allowed",
            _format_global_not_allowed,
            limit,
            "global installs outside allowlist",
        )

    if show_shadowed or full or issues_only:
        _print_limited_issue_list(
            issues.get("missing_for_cwd") or [],
            "missing_for_cwd",
            _format_missing_for_cwd,
            limit,
            "missing cwd-scoped skills",
        )

    _print_visibility_undefined(payload, full, limit)
    _print_visibility_next_actions(payload)
    _print_visibility_recommendations(payload, full, limit)


def _print_skill_audit_global(payload: dict[str, Any], limit: int) -> None:
    global_row = payload.get("global")
    if not global_row:
        return
    issues = global_row.get("issues") or {}
    print(
        "global: "
        f"broken={issues.get('broken_global', 0)} "
        f"not_allowed={issues.get('global_not_allowed', 0)} "
        f"extra={issues.get('extra_global', 0)}"
    )
    if global_row.get("global_not_allowed"):
        print("  not_allowed:", _truncate_names(global_row["global_not_allowed"], limit))
    if global_row.get("extra_global"):
        print("  extra:", _truncate_names(global_row["extra_global"], limit))


def _repo_audit_problem_summary(repo: dict[str, Any], limit: int) -> list[str]:
    if repo.get("state") == "missing":
        return ["missing repo path"]
    problems: list[str] = []
    if repo.get("missing_for_cwd"):
        problems.append(f"missing={_truncate_names(repo['missing_for_cwd'], limit)}")
    if repo.get("scope_violations"):
        problems.append(f"scope={_truncate_names(repo['scope_violations'], limit)}")
    if repo.get("broken_project"):
        problems.append(f"broken={_truncate_names(repo['broken_project'], limit)}")
    return problems or ["clean"]


def print_skill_audit_text(payload: dict[str, Any], *, limit: int = 40) -> None:
    summary = payload.get("summary") or {}
    overlays = payload.get("overlays") or {}
    print(
        "skill audit: "
        f"{summary.get('candidate_repos', 0)} candidate repos, "
        f"{summary.get('reported_repos', 0)} reported, "
        f"{summary.get('repos_with_issues', 0)} with issues"
    )
    print(f"cwd: {payload.get('cwd')}")
    print(
        "active: "
        f"clients={_join_or_none(payload.get('active_clients') or [])} "
        f"profiles={_join_or_none(payload.get('active_profiles') or [])}"
    )
    print(
        "overlays: "
        f"active={_join_or_none(overlays.get('active') or [])} "
        f"available={_join_or_none(overlays.get('available') or [])}"
    )
    _print_skill_audit_global(payload, max(0, min(limit, 20)))

    repos = payload.get("repos") or []
    if repos:
        print("repos:")
    for repo in repos[: max(0, limit)]:
        categories = _join_or_none(repo.get("categories") or [])
        clients = _join_or_none(repo.get("matched_clients") or [])
        problems = "; ".join(_repo_audit_problem_summary(repo, max(1, min(limit, 8))))
        print(f"  - {repo.get('path')}: clients={clients} categories={categories} {problems}")
    remaining = len(repos) - min(len(repos), max(0, limit))
    if remaining > 0:
        print(f"  ... {remaining} more repos (rerun with --limit {len(repos)} or --format json)")

    next_actions = payload.get("next_actions") or []
    if next_actions:
        print("next_actions:")
        for action in next_actions:
            print(f"  - {action}")
