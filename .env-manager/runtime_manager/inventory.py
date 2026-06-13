"""Skill-visibility inventory.

Single responsibility: enumerating skill sources, candidates, declared and
installed occurrences, global-home resolution and parity, and effective/shadow
resolution -- i.e. *what skills exist and where they actually are on disk*.
Depends on ._skill_common and .policy_eval.
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

from ._skill_common import *
from .policy_eval import *

__all__ = [
    'SKILL_INSTALL_SCAN_MAX_DEPTH',
    'DEFAULT_SKILL_SOURCE_ROOT_PATTERNS',
    'DEFAULT_SKILL_INSTALL_SCAN_ROOT_PATTERNS',
    'SKILL_SOURCE_SCAN_SKIP_DIRS',
    'SOURCE_BUCKET_ORDER',
    '_skill_source_options',
    '_declared_source_bucket',
    '_declared_entry_from_lock',
    '_declared_skill_occurrence',
    '_declared_target_counts',
    '_declared_layer_summary',
    '_declared_skill_occurrences',
    '_scan_installed_root',
    'resolve_global_homes',
    '_default_global_roots',
    'global_home_surfaces_report',
    'SKILL_PARITY_IGNORED_NAMES',
    '_global_surface_entry_sets',
    'collect_skill_parity',
    'skill_parity_next_actions',
    'SKILL_HOME_CANONICAL_LAYOUT',
    '_classify_surface_layout',
    'relink_global_homes_to_symmetric_layout',
    '_global_root_entry_names',
    '_project_skill_roots',
    '_path_is_skill_dir',
    '_skill_source_root_from_path',
    '_declared_skill_source_roots',
    '_installed_skill_source_roots',
    '_operator_install_scan_roots',
    '_configured_skill_audit_scan_roots',
    '_all_skill_install_roots',
    '_all_installed_skill_names',
    '_skill_source_roots',
    '_has_skipped_source_part',
    '_skill_source_candidates',
    '_undefined_source_skills',
    '_layer_surface_preference',
    '_normalized_source',
    '_same_source',
    '_is_material_shadow',
    '_effective_occurrences',
    '_collect_installed_visibility_layers',
]


SKILL_INSTALL_SCAN_MAX_DEPTH = 4
DEFAULT_SKILL_SOURCE_ROOT_PATTERNS = (
    "~/repos/opensource/skills",
    "~/repos/opensource/skillbox/skills",
    "~/repos/skills-private",
    "~/repos/skills/skills",
    "~/repos/marketingskills/skills",
    "~/projects/jsm-skill-archive-*",
)
DEFAULT_SKILL_INSTALL_SCAN_ROOT_PATTERNS: tuple[str, ...] = ()
SKILL_SOURCE_SCAN_SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    "target",
    "DerivedData",
    ".next",
    "dist",
    "build",
    "coverage",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}
SOURCE_BUCKET_ORDER = {
    "opensource/skills": 0,
    "skills-private": 1,
    "marketingskills": 2,
    "sweet-potato": 3,
    "local": 4,
    "archive": 9,
    "external": 10,
}


def _skill_source_options(
    model: dict[str, Any],
    skill_name: str,
    *,
    explicit_source: str | None = None,
) -> list[dict[str, Any]]:
    candidates_by_source: dict[str, dict[str, Any]] = {}

    if explicit_source:
        source_path = Path(os.path.expandvars(os.path.expanduser(explicit_source))).resolve()
        if not (source_path / "SKILL.md").is_file() and (source_path / skill_name / "SKILL.md").is_file():
            source_path = source_path / skill_name
        if not (source_path / "SKILL.md").is_file():
            raise RuntimeError(f"Skill source does not contain SKILL.md: {source_path}")
        candidates_by_source[str(source_path)] = {
            "name": skill_name,
            "source": str(source_path),
            "source_bucket": _source_bucket(str(source_path)),
            "root": str(source_path.parent),
            "explicit": True,
        }

    declared_occurrences, _ = _declared_skill_occurrences(model)
    for root in _skill_source_roots(model, declared_occurrences):
        for candidate in _skill_source_candidates(root):
            if str(candidate.get("name") or "") != skill_name:
                continue
            candidates_by_source.setdefault(str(candidate["source"]), {
                "name": skill_name,
                "source": candidate["source"],
                "source_bucket": candidate.get("source_bucket"),
                "root": candidate.get("root"),
                "explicit": False,
            })

    return sorted(
        candidates_by_source.values(),
        key=lambda item: (
            not bool(item.get("explicit")),
            SOURCE_BUCKET_ORDER.get(str(item.get("source_bucket") or ""), 8),
            str(item.get("source") or ""),
        ),
    )


def _declared_source_bucket(source_kind: str, source: str) -> str:
    if source_kind == "repo":
        return "repo"
    if source_kind == "distributor":
        return "distributor"
    return _source_bucket(source)


def _declared_entry_from_lock(name: str, lock_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "source_kind": "repo" if lock_record.get("repo") else "path",
        "source": str(lock_record.get("repo") or lock_record.get("source_path") or ""),
        "declared_ref": lock_record.get("declared_ref"),
        "config_index": None,
    }


def _declared_skill_occurrence(
    skillset: dict[str, Any],
    layer: dict[str, Any],
    name: str,
    declared_entry: dict[str, Any],
    lock_record: dict[str, Any],
) -> dict[str, Any]:
    source = str(
        declared_entry.get("source")
        or lock_record.get("repo")
        or lock_record.get("source_path")
        or ""
    )
    source_kind = str(declared_entry.get("source_kind") or "unknown")
    return {
        "name": name,
        "availability": "declared",
        "layer": layer["id"],
        "layer_label": layer["label"],
        "layer_rank": layer["rank"],
        "scope": layer["scope"],
        "skillset_id": str(skillset.get("id", "")),
        "source_kind": source_kind,
        "source": source,
        "source_bucket": _declared_source_bucket(source_kind, source),
        "declared_ref": declared_entry.get("declared_ref") or lock_record.get("declared_ref"),
        "resolved_commit": lock_record.get("resolved_commit"),
        "targets": _target_states_for_skill(skillset, name, lock_record),
        "state": "declared",
    }


def _declared_target_counts(
    occurrences: list[dict[str, Any]],
    skillset_id: str,
) -> tuple[int, int]:
    target_count = 0
    healthy_targets = 0
    for occurrence in occurrences:
        if occurrence.get("skillset_id") != skillset_id:
            continue
        for target in occurrence.get("targets") or []:
            target_count += 1
            if target.get("state") in {"ok", "present"}:
                healthy_targets += 1
    return target_count, healthy_targets


def _declared_layer_summary(
    skillset: dict[str, Any],
    layer: dict[str, Any],
    lock_path: Path,
    lock_error: str | None,
    config_error: str | None,
    declared_by_name: dict[str, dict[str, Any]],
    occurrences: list[dict[str, Any]],
) -> dict[str, Any]:
    skillset_id = str(skillset.get("id", ""))
    target_count, healthy_targets = _declared_target_counts(occurrences, skillset_id)
    return {
        "id": layer["id"],
        "label": layer["label"],
        "rank": layer["rank"],
        "scope": layer["scope"],
        "kind": "declared",
        "skillset_id": skillset_id,
        "config_path": str(skillset.get("skill_repos_config_host_path", "")),
        "lock_path": str(lock_path),
        "lock_present": lock_path.is_file(),
        "lock_error": lock_error,
        "config_error": config_error,
        "skill_count": len(declared_by_name),
        "healthy_targets": healthy_targets,
        "target_count": target_count,
    }


def _declared_skill_occurrences(model: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    occurrences: list[dict[str, Any]] = []
    layer_summaries: list[dict[str, Any]] = []

    for skillset in model.get("skills") or []:
        if skillset.get("kind") != "skill-repo-set":
            continue
        layer = _skillset_layer(skillset, model)
        lock_path = Path(str(skillset.get("lock_path_host_path", "")))
        lock_records, lock_error = _load_lock_records(lock_path)
        declared, config_error = _declared_entries_from_config(skillset)
        declared_by_name = {entry["name"]: entry for entry in declared}

        for name, lock_record in lock_records.items():
            declared_by_name.setdefault(name, _declared_entry_from_lock(name, lock_record))

        for name, declared_entry in sorted(declared_by_name.items()):
            lock_record = lock_records.get(name, {})
            occurrences.append(
                _declared_skill_occurrence(skillset, layer, name, declared_entry, lock_record)
            )

        layer_summaries.append(
            _declared_layer_summary(
                skillset,
                layer,
                lock_path,
                lock_error,
                config_error,
                declared_by_name,
                occurrences,
            )
        )

    return occurrences, layer_summaries


def _scan_installed_root(root: Path, *, layer: str, label: str, rank: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    broken = 0
    non_skill = 0
    if not root.is_dir():
        return occurrences, {
            "id": layer,
            "label": label,
            "rank": rank,
            "kind": "installed",
            "path": str(root),
            "present": False,
            "skill_count": 0,
            "broken_count": 0,
            "non_skill_count": 0,
        }

    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.startswith("."):
            continue
        is_link = entry.is_symlink()
        link_target = os.readlink(entry) if is_link else ""
        resolve_error = ""
        try:
            resolved = entry.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            resolved = entry
            resolve_error = str(exc)
        name = _installed_skill_name(entry)
        broken_reason = ""

        if resolve_error:
            state = "broken"
            broken += 1
            kind = "symlink" if is_link else "file"
            has_skill_md = False
            # Permission error / symlink loop / other resolution failure: the
            # link cannot even be read, so taxonomy must treat it as unreadable
            # rather than guessing where the target lives.
            broken_reason = "unreadable"
        elif is_link and not _path_exists(entry):
            state = "broken"
            broken += 1
            kind = "symlink"
            has_skill_md = False
            # The link reads fine but its target does not exist on this box.
            broken_reason = "missing-target"
        elif entry.is_dir():
            kind = "directory"
            has_skill_md = (entry / "SKILL.md").is_file()
            state = "ok" if has_skill_md else "non-skill"
        elif entry.is_file() and entry.suffix == ".skill":
            kind = "package"
            has_skill_md = False
            state = "ok"
        else:
            kind = "file"
            has_skill_md = False
            state = "non-skill"

        if state == "non-skill":
            non_skill += 1
            continue

        source_path = str(resolved)
        occurrence = {
            "name": name,
            "availability": "installed",
            "layer": layer,
            "layer_label": label,
            "layer_rank": rank,
            "scope": "installed",
            "source_kind": kind,
            "source": source_path,
            "source_bucket": _source_bucket(source_path),
            "path": str(entry),
            "link_target": link_target,
            "has_skill_md": has_skill_md,
            "state": state,
        }
        if state == "broken":
            occurrence["broken_reason"] = broken_reason
            # The absolute target a classifier should reason about: prefer the
            # raw readlink resolved against the link's parent (so relative links
            # become absolute), falling back to the realpath for non-link breaks.
            if is_link and link_target:
                abs_target = os.path.normpath(
                    os.path.join(str(entry.parent), os.path.expanduser(link_target))
                )
            else:
                abs_target = source_path
            occurrence["link_target_abs"] = abs_target
        occurrences.append(occurrence)

    summary = {
        "id": layer,
        "label": label,
        "rank": rank,
        "kind": "installed",
        "path": str(root),
        "present": True,
        "skill_count": len(occurrences),
        "broken_count": broken,
        "non_skill_count": non_skill,
    }
    return occurrences, summary


def resolve_global_homes(
    *,
    home_root_env: str | None = None,
) -> list[dict[str, Any]]:
    """Canonical resolution of every distinct *global home* surface.

    This is the single source of truth for "which homes count as a global
    skill surface". The OS home (``Path.home()``) is always a surface. When
    ``SKILLBOX_HOME_ROOT`` (the *managed* home, e.g. ``/srv/skillbox/home``) is
    set, it is **also** a global surface — both are scanned. If the managed
    home resolves (via ``realpath``) to the same directory as the OS home (for
    example because the managed home symlinks back into the OS home), the two
    collapse into a single surface so installs are never double-counted.

    Returns an ordered list of ``{"origin", "home", "realpath"}`` dicts where
    ``origin`` is ``"os-home"``, ``"managed-home"``, or ``"both"`` (when the OS
    and managed homes are realpath-equivalent). The OS home, when distinct,
    always sorts first.
    """
    raw_env = os.environ.get(GLOBAL_HOME_ROOT_ENV, "") if home_root_env is None else home_root_env
    managed_raw = str(raw_env or "").strip()

    os_home = Path.home()
    os_real = _realpath(os_home)

    surfaces: list[dict[str, Any]] = [
        {"origin": "os-home", "home": os_home, "realpath": os_real},
    ]

    if managed_raw:
        managed_home = Path(os.path.expandvars(os.path.expanduser(managed_raw)))
        managed_real = _realpath(managed_home)
        if managed_real == os_real:
            # Symlinked-equivalent: one surface, attributed to both origins.
            surfaces[0]["origin"] = "both"
        else:
            surfaces.append(
                {"origin": "managed-home", "home": managed_home, "realpath": managed_real}
            )
    return surfaces


def _default_global_roots() -> list[tuple[str, Path]]:
    """Every distinct global skill root across all resolved global homes.

    Routes through :func:`resolve_global_homes`, so the managed home declared
    by ``SKILLBOX_HOME_ROOT`` is now scanned alongside the OS home. Roots whose
    ``realpath`` collapses to the same directory (e.g. an OS-home
    ``.claude/skills`` symlinked to the managed home's) are emitted once.
    """
    roots: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for home in resolve_global_homes():
        for surface in GLOBAL_HOME_SURFACES:
            root = Path(home["home"]) / f".{surface}" / "skills"
            key = _realpath(root)
            if key in seen:
                continue
            seen.add(key)
            roots.append((surface, root))
    return roots


def global_home_surfaces_report() -> list[dict[str, Any]]:
    """Audit-facing view: each distinct global *home* surface with realpath.

    For every resolved global home, list its per-surface skill roots with the
    realpath used to de-duplicate them and which installed skill entries are
    visible there. ``os_only`` / ``managed_only`` partition entries by where
    they live so the audit can show that the MANAGED home's installs are no
    longer invisible.
    """
    homes = resolve_global_homes()
    os_entries: dict[str, set[str]] = {}
    managed_entries: dict[str, set[str]] = {}
    report: list[dict[str, Any]] = []
    seen_roots: set[str] = set()

    for home in homes:
        origin = str(home["origin"])
        surfaces: list[dict[str, Any]] = []
        for surface in GLOBAL_HOME_SURFACES:
            root = Path(home["home"]) / f".{surface}" / "skills"
            real = _realpath(root)
            names = _global_root_entry_names(root)
            collapsed = real in seen_roots
            seen_roots.add(real)
            surfaces.append(
                {
                    "surface": surface,
                    "root": str(root),
                    "realpath": real,
                    "present": root.is_dir(),
                    "entries": names,
                    "collapsed": collapsed,
                }
            )
            bucket = os_entries if origin in {"os-home", "both"} else managed_entries
            bucket.setdefault(surface, set()).update(names)
            if origin == "both":
                managed_entries.setdefault(surface, set()).update(names)
        report.append(
            {
                "origin": origin,
                "home": str(home["home"]),
                "realpath": str(home["realpath"]),
                "surfaces": surfaces,
            }
        )

    os_only: list[dict[str, str]] = []
    managed_only: list[dict[str, str]] = []
    for surface in GLOBAL_HOME_SURFACES:
        os_names = os_entries.get(surface, set())
        managed_names = managed_entries.get(surface, set())
        for name in sorted(os_names - managed_names):
            os_only.append({"surface": surface, "name": name})
        for name in sorted(managed_names - os_names):
            managed_only.append({"surface": surface, "name": name})

    return [
        {
            "homes": report,
            "os_only": os_only,
            "managed_only": managed_only,
        }
    ]


# ---------------------------------------------------------------------------
# Claude <-> Codex skill-surface parity
# ---------------------------------------------------------------------------
#
# sbp's core promise is that BOTH agents see the same world. That promise was
# only ever audited for MCP (mcp_visibility.collect_mcp_audit / _parity_payload).
# The functions below extend the identical parity-drift reporting pattern to the
# global *skill* surfaces: the effective skill sets exposed under
# ``.claude/skills`` vs ``.codex/skills`` across every resolved global home
# (resolve_global_homes()). Output mirrors the MCP parity block shape
# (``claude_only`` / ``codex_only`` / ``shared``) so an agent can ``jq`` either
# audit the same way.
#
# ``_shared`` is the cross-root payload link skills depend on (see fixture_fleet
# and the live skills-private/_shared chain); it is not a real skill, so it is
# excluded from the parity diff by default and surfaced separately under
# ``ignored`` for transparency.

SKILL_PARITY_IGNORED_NAMES = ("_shared",)


def _global_surface_entry_sets(
    *,
    home_root_env: str | None = None,
) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    """Union of installed skill entry names per global surface across homes.

    Walks every resolved global home (``resolve_global_homes``) and, for each
    ``GLOBAL_HOME_SURFACES`` surface (``claude`` / ``codex``), unions the entry
    names visible in that surface's ``.<surface>/skills`` root. Roots whose
    ``realpath`` collapses to one another (the symlinked two-home layout) are
    counted once so a shared dir is never double-attributed.

    Returns ``(by_surface, roots)`` where ``by_surface`` maps a surface name to
    the set of entry names seen there, and ``roots`` is the ordered per-(home,
    surface) detail used for the audit's home breakdown.
    """
    by_surface: dict[str, set[str]] = {surface: set() for surface in GLOBAL_HOME_SURFACES}
    roots: list[dict[str, Any]] = []
    seen_real: set[str] = set()
    for home in resolve_global_homes(home_root_env=home_root_env):
        for surface in GLOBAL_HOME_SURFACES:
            root = Path(home["home"]) / f".{surface}" / "skills"
            real = _realpath(root)
            collapsed = real in seen_real
            seen_real.add(real)
            names = _global_root_entry_names(root)
            roots.append(
                {
                    "origin": str(home["origin"]),
                    "surface": surface,
                    "root": str(root),
                    "realpath": real,
                    "present": root.is_dir(),
                    "collapsed": collapsed,
                    "entries": names,
                }
            )
            # A realpath-collapsed root has already contributed its entries via
            # the first home that owns it; unioning again is harmless (it is the
            # same set) but skipping keeps the per-surface union honest about
            # distinct roots only.
            if not collapsed:
                by_surface[surface].update(names)
    return by_surface, roots


def collect_skill_parity(
    *,
    home_root_env: str | None = None,
    ignored_names: tuple[str, ...] = SKILL_PARITY_IGNORED_NAMES,
) -> dict[str, Any]:
    """Diff the effective GLOBAL skill sets of Claude vs Codex.

    The skill-surface analogue of ``mcp_visibility.collect_mcp_audit``'s parity
    block. It answers the question the MCP audit answers for servers, for
    skills: which global skills does ONE agent see that the OTHER does not?

    The shape mirrors ``mcp_visibility._parity_payload`` so callers (and ``jq``
    pipelines) treat skill drift and MCP drift identically:

    * ``claude_only`` / ``codex_only`` / ``shared`` — the partition.
    * ``in_sync`` — convenience boolean (no divergence).
    * ``ignored`` — entries excluded from the diff (e.g. the ``_shared`` payload
      link), reported so the exclusion is never silent.
    * ``homes`` — per-(home, surface) root detail (origin, realpath, entries,
      whether the root realpath-collapsed) so an operator can see exactly which
      home contributes which divergence.
    * ``summary`` — counts mirroring the MCP audit summary block.

    Read-only: it scans the resolved homes and never mutates anything.
    """
    by_surface, roots = _global_surface_entry_sets(home_root_env=home_root_env)
    ignore = set(ignored_names)

    claude_all = by_surface.get("claude", set())
    codex_all = by_surface.get("codex", set())
    claude_set = claude_all - ignore
    codex_set = codex_all - ignore

    claude_only = sorted(claude_set - codex_set)
    codex_only = sorted(codex_set - claude_set)
    shared = sorted(claude_set & codex_set)
    ignored = sorted((claude_all | codex_all) & ignore)

    return {
        "claude_only": claude_only,
        "codex_only": codex_only,
        "shared": shared,
        "ignored": ignored,
        "in_sync": not claude_only and not codex_only,
        "homes": roots,
        "summary": {
            "claude_total": len(claude_set),
            "codex_total": len(codex_set),
            "shared": len(shared),
            "claude_only": len(claude_only),
            "codex_only": len(codex_only),
            "divergent": len(claude_only) + len(codex_only),
            "ignored": len(ignored),
        },
    }


def skill_parity_next_actions(parity: dict[str, Any]) -> list[str]:
    """Operator-review actions for the skill parity block.

    Mirrors ``mcp_visibility._mcp_next_actions``' parity advice: name the
    divergent skills and point at the (operator-reviewed) relink dry-run. These
    are *suggestions for an operator to review*, never an auto-apply.
    """
    actions: list[str] = []
    claude_only = parity.get("claude_only") or []
    codex_only = parity.get("codex_only") or []
    if claude_only:
        actions.append(
            "mirror Claude-only global skills into the Codex surface (or unlink if obsolete): "
            + ", ".join(claude_only)
        )
    if codex_only:
        actions.append(
            "mirror Codex-only global skills into the Claude surface (or unlink if obsolete): "
            + ", ".join(codex_only)
        )
    if claude_only or codex_only:
        actions.append(
            "review the symmetric-layout relink plan (DRY RUN, no live mutation): "
            "skill_visibility.relink_global_homes_to_symmetric_layout()"
        )
    return actions


# Which layout hooks the OS home's per-agent skill surfaces to the managed home.
# See skillbox-config/docs/HOME_LAYOUT.md for the decision + rationale. The
# canonical layout is ``directory-symlink``: ``<home>/.<surface>/skills`` is a
# single symlink to the managed surface dir, so the two agents share ONE inode
# and can never drift entry-by-entry.
SKILL_HOME_CANONICAL_LAYOUT = "directory-symlink"


def _classify_surface_layout(root: Path) -> str:
    """Classify how a ``.<surface>/skills`` root is hooked up.

    * ``dir-symlink``  — the skills dir itself is a symlink (canonical layout).
    * ``per-entry``    — a real dir whose entries are individual symlinks.
    * ``real-dir``     — a real dir holding real (non-symlink) skill dirs.
    * ``missing``      — the root does not exist.
    """
    if not os.path.lexists(root):
        return "missing"
    if root.is_symlink():
        return "dir-symlink"
    if not root.is_dir():
        return "missing"
    try:
        entries = [e for e in root.iterdir() if not e.name.startswith(".")]
    except OSError:
        return "real-dir"
    if entries and all(e.is_symlink() for e in entries):
        return "per-entry"
    return "real-dir"


def relink_global_homes_to_symmetric_layout(
    *,
    home_root_env: str | None = None,
    target_layout: str = SKILL_HOME_CANONICAL_LAYOUT,
    managed_home_root: str | None = None,
) -> dict[str, Any]:
    """DRY-RUN: describe the relinks that WOULD make the global homes symmetric.

    This function NEVER mutates the filesystem. It computes the relink plan that
    would converge every resolved global home's per-agent skill surfaces onto
    the canonical ``directory-symlink`` layout (a single
    ``<home>/.<surface>/skills`` symlink into the managed surface dir), so Claude
    and Codex share one inode and cannot drift entry-by-entry.

    Applying the plan against LIVE operator homes is an operator-reviewed step,
    deliberately out of scope here: callers get the planned actions to review,
    not a mutation. ``managed_home_root`` (defaults to ``SKILLBOX_HOME_ROOT``)
    names the managed home whose ``.<surface>/skills`` dirs are the link targets.

    Returns ``{"dry_run": True, "target_layout", "managed_home", "actions",
    "summary"}`` where each action is ``{op: "relink", surface, home_origin,
    link, current_layout, would_point_to, reason}``.
    """
    if target_layout != SKILL_HOME_CANONICAL_LAYOUT:
        raise RuntimeError(
            f"unsupported target layout {target_layout!r}; "
            f"only {SKILL_HOME_CANONICAL_LAYOUT!r} is supported"
        )

    raw_managed = managed_home_root
    if raw_managed is None:
        raw_managed = (
            os.environ.get(GLOBAL_HOME_ROOT_ENV, "")
            if home_root_env is None
            else home_root_env
        )
    managed_raw = str(raw_managed or "").strip()
    managed_home = (
        Path(os.path.expandvars(os.path.expanduser(managed_raw))) if managed_raw else None
    )

    actions: list[dict[str, Any]] = []
    homes = resolve_global_homes(home_root_env=home_root_env)
    for home in homes:
        origin = str(home["origin"])
        for surface in GLOBAL_HOME_SURFACES:
            root = Path(home["home"]) / f".{surface}" / "skills"
            layout = _classify_surface_layout(root)
            # The managed home's OWN surface dirs are the canonical link targets;
            # nothing relinks them onto themselves.
            target_dir = (
                managed_home / f".{surface}" / "skills" if managed_home is not None else None
            )
            is_managed_self = (
                managed_home is not None
                and _realpath(root) == _realpath(target_dir)
            )
            already_symmetric = layout == "dir-symlink" and (
                target_dir is None or _realpath(root) == _realpath(target_dir)
            )
            if is_managed_self or already_symmetric:
                continue
            # Without a managed home there is no shared target to point at; the
            # divergence is real but only an operator can pick the target.
            if target_dir is None:
                actions.append(
                    {
                        "op": "relink",
                        "surface": surface,
                        "home_origin": origin,
                        "link": str(root),
                        "current_layout": layout,
                        "would_point_to": None,
                        "reason": (
                            "no SKILLBOX_HOME_ROOT managed home is set; an operator must "
                            "choose the shared target before relinking to the canonical layout"
                        ),
                    }
                )
                continue
            actions.append(
                {
                    "op": "relink",
                    "surface": surface,
                    "home_origin": origin,
                    "link": str(root),
                    "current_layout": layout,
                    "would_point_to": str(target_dir),
                    "reason": (
                        f"converge {layout} surface onto the canonical "
                        f"{SKILL_HOME_CANONICAL_LAYOUT} (single shared inode with the managed home)"
                    ),
                }
            )

    return {
        "dry_run": True,
        "target_layout": target_layout,
        "managed_home": str(managed_home) if managed_home is not None else None,
        "actions": actions,
        "summary": {
            "homes": len(homes),
            "surfaces": len(GLOBAL_HOME_SURFACES),
            "relinks_planned": len(actions),
            "blocked_no_managed_home": sum(
                1 for action in actions if action.get("would_point_to") is None
            ),
        },
    }


def _global_root_entry_names(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    names: list[str] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        names.append(_installed_skill_name(entry))
    return names


def _project_skill_roots(cwd: Path) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    cwd = cwd.resolve()
    home = Path.home().resolve()
    for parent in [cwd, *cwd.parents]:
        if parent == home:
            break
        for surface in ("claude", "codex"):
            root = parent / f".{surface}" / "skills"
            if root.is_dir():
                roots.append((surface, root))
        if (parent / ".git").exists():
            break
    return roots


def _path_is_skill_dir(path: Path) -> bool:
    return path.is_dir() and (path / "SKILL.md").is_file()


def _skill_source_root_from_path(path: str) -> Path | None:
    raw = str(path or "").strip()
    if not raw or "://" in raw or raw.startswith("git@"):
        return None
    candidate = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    if _path_is_skill_dir(candidate) or candidate.suffix == ".skill":
        return candidate.parent
    return candidate


def _declared_skill_source_roots(model: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    for skillset in model.get("skills") or []:
        if skillset.get("kind") != "skill-repo-set":
            continue
        declared, _ = _declared_entries_from_config(skillset)
        for entry in declared:
            if entry.get("source_kind") != "path":
                continue
            root = _skill_source_root_from_path(str(entry.get("source") or ""))
            if root is not None:
                roots.append(root)
    return roots


def _installed_skill_source_roots(occurrences: list[dict[str, Any]]) -> list[Path]:
    roots: list[Path] = []
    for occurrence in occurrences:
        if occurrence.get("availability") != "installed" or occurrence.get("state") == "broken":
            continue
        root = _skill_source_root_from_path(str(occurrence.get("source") or ""))
        if root is not None:
            roots.append(root)
    return roots


def _operator_install_scan_roots(model: dict[str, Any]) -> list[Path]:
    roots = [
        *_expand_skill_source_patterns(list(DEFAULT_SKILL_INSTALL_SCAN_ROOT_PATTERNS)),
        *_expand_skill_source_patterns(_policy_skill_install_scan_patterns(model)),
    ]
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        # Alias-canonicalize the dedup key so the dual ``/srv/repos`` +
        # ``/srv/skillbox/repos`` scan-root pair from skill-scope.yaml collapses
        # to a single resolved root (no double fleet walk, no 2x repo count).
        key = _canonicalize_repo_path(str(root.resolve()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root.resolve())
    return deduped


def _configured_skill_audit_scan_roots(model: dict[str, Any]) -> list[Path]:
    """Return default repo scan roots for a cross-repo skill audit."""
    return _operator_install_scan_roots(model)


def _all_skill_install_roots(model: dict[str, Any]) -> list[Path]:
    home = Path.home()
    roots = [
        home / ".claude" / "skills",
        home / ".codex" / "skills",
        home / ".agents" / "skills",
    ]
    for scan_root in _operator_install_scan_roots(model):
        if not scan_root.is_dir():
            continue
        for current, dirnames, _ in os.walk(scan_root):
            dirnames[:] = sorted(
                dirname for dirname in dirnames
                if dirname not in SKILL_SOURCE_SCAN_SKIP_DIRS
            )
            current_path = Path(current)
            rel = current_path.relative_to(scan_root)
            if len(rel.parts) > SKILL_INSTALL_SCAN_MAX_DEPTH:
                dirnames[:] = []
                continue
            if _has_skipped_source_part(rel):
                dirnames[:] = []
                continue
            if current_path.name == "skills" and current_path.parent.name in {".claude", ".codex", ".agents"}:
                roots.append(current_path)
                dirnames[:] = []

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _all_installed_skill_names(model: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for root in _all_skill_install_roots(model):
        installed, _ = _scan_installed_root(
            root,
            layer=f"installed:any:{root}",
            label="installed anywhere",
            rank=GLOBAL_LAYER_RANK,
        )
        names.update(
            str(item.get("name") or "")
            for item in installed
            if item.get("state") != "broken" and str(item.get("name") or "")
        )
    return names


def _skill_source_roots(model: dict[str, Any], occurrences: list[dict[str, Any]]) -> list[Path]:
    roots = [
        *_expand_skill_source_patterns(list(DEFAULT_SKILL_SOURCE_ROOT_PATTERNS)),
        *_expand_skill_source_patterns(_policy_skill_source_patterns(model)),
        *_declared_skill_source_roots(model),
        *_installed_skill_source_roots(occurrences),
    ]
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root.resolve())
    return deduped


def _has_skipped_source_part(path: Path) -> bool:
    return any(part in SKILL_SOURCE_SCAN_SKIP_DIRS for part in path.parts)


def _skill_source_candidates(root: Path) -> list[dict[str, Any]]:
    if not root.exists() or not root.is_dir():
        return []

    candidates: list[dict[str, Any]] = []
    skill_dirs: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            dirname for dirname in dirnames
            if dirname not in SKILL_SOURCE_SCAN_SKIP_DIRS
        )
        current_path = Path(current)
        if _has_skipped_source_part(current_path.relative_to(root)):
            dirnames[:] = []
            continue
        if "SKILL.md" in filenames:
            skill_dirs.append(current_path)
            dirnames[:] = []

    for skill_dir in sorted(skill_dirs):
        skill_dir = skill_dir.resolve()
        candidates.append({
            "name": skill_dir.name,
            "source": str(skill_dir),
            "source_bucket": _source_bucket(str(skill_dir)),
            "root": str(root.resolve()),
            "state": "undefined",
        })
    return candidates


def _undefined_source_skills(
    model: dict[str, Any],
    occurrences: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    synced_names = {
        str(item.get("name") or "")
        for item in occurrences
        if str(item.get("name") or "") and item.get("state") != "broken"
    }
    synced_names.update(_all_installed_skill_names(model))
    root_summaries: list[dict[str, Any]] = []
    undefined_by_path: dict[str, dict[str, Any]] = {}

    for root in _skill_source_roots(model, occurrences):
        candidates = _skill_source_candidates(root)
        root_summaries.append({
            "id": f"source:{root}",
            "label": str(root),
            "rank": 0,
            "kind": "source",
            "path": str(root),
            "present": root.is_dir(),
            "skill_count": len(candidates),
            "undefined_count": sum(1 for item in candidates if item["name"] not in synced_names),
        })
        for candidate in candidates:
            if candidate["name"] in synced_names:
                continue
            undefined_by_path[candidate["source"]] = candidate

    undefined = sorted(
        undefined_by_path.values(),
        key=lambda item: (
            SOURCE_BUCKET_ORDER.get(str(item.get("source_bucket") or ""), 8),
            str(item.get("name") or ""),
            str(item.get("source") or ""),
        ),
    )
    return undefined, root_summaries


def _layer_surface_preference(layer: str) -> int:
    if ":claude" in layer:
        return 2
    if ":codex" in layer:
        return 1
    return 0


def _normalized_source(source: Any) -> str:
    raw = str(source or "").strip()
    if not raw:
        return ""
    if "://" in raw or raw.startswith("git@"):
        return raw
    return os.path.realpath(os.path.expandvars(os.path.expanduser(raw)))


def _same_source(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_source = _normalized_source(first.get("source"))
    second_source = _normalized_source(second.get("source"))
    return bool(first_source and second_source and first_source == second_source)


def _is_material_shadow(winner: dict[str, Any], hidden: dict[str, Any]) -> bool:
    if winner.get("availability") == "installed" and hidden.get("availability") == "declared":
        return False
    return not _same_source(winner, hidden)


def _effective_occurrences(occurrences: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for occurrence in occurrences:
        grouped.setdefault(str(occurrence.get("name", "")), []).append(occurrence)

    effective: list[dict[str, Any]] = []
    shadowed: list[dict[str, Any]] = []
    for name, group in grouped.items():
        ordered = sorted(
            group,
            key=lambda item: (
                int(item.get("layer_rank", 0)),
                0 if item.get("state") == "broken" else 1,
                _layer_surface_preference(str(item.get("layer", ""))),
                str(item.get("layer", "")),
                str(item.get("source", "")),
            ),
            reverse=True,
        )
        winner = dict(ordered[0])
        hidden = [
            item for item in ordered[1:]
            if _is_material_shadow(winner, item)
        ]
        winner["shadowed_count"] = len(hidden)
        if hidden:
            winner["shadows"] = [
                {
                    "layer": item.get("layer"),
                    "state": item.get("state"),
                    "source": item.get("source"),
                }
                for item in hidden
            ]
            shadowed.append({
                "name": name,
                "winner_layer": winner.get("layer"),
                "shadowed_layers": [item.get("layer") for item in hidden],
            })
        effective.append(winner)

    return sorted(effective, key=lambda item: str(item.get("name", ""))), shadowed


def _collect_installed_visibility_layers(
    cwd_path: Path,
    *,
    include_global: bool,
    include_project: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    occurrences: list[dict[str, Any]] = []
    layers: list[dict[str, Any]] = []
    if include_global:
        for surface, root in _default_global_roots():
            installed, summary = _scan_installed_root(
                root,
                layer=f"global:{surface}",
                label=f"global {surface}",
                rank=GLOBAL_LAYER_RANK,
            )
            occurrences.extend(installed)
            layers.append(summary)
    if include_project:
        for surface, root in _project_skill_roots(cwd_path):
            installed, summary = _scan_installed_root(
                root,
                layer=f"project:{surface}:{root.parent.parent}",
                label=f"project {surface}",
                rank=PROJECT_LAYER_RANK,
            )
            occurrences.extend(installed)
            layers.append(summary)
    return occurrences, layers
