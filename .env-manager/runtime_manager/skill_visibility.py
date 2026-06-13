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


DEFAULT_LAYER_RANK = 10
CLIENT_LAYER_RANK = 20
GLOBAL_LAYER_RANK = 30
PROJECT_LAYER_RANK = 40
SKILL_SCOPE_POLICY_FILES = ("skill-scope.yaml", "skills-scope.yaml")
OVERLAY_STATE_ENV = "SKILLBOX_OVERLAY_STATE"
OVERLAY_STATE_DEFAULT = "~/.skillbox-state/overlays"
OVERLAY_ENV_VAR = "SKILLBOX_OVERLAYS"
SKILL_SOURCE_ROOT_KEYS = ("skill_source_roots", "source_roots", "skill_roots")
SKILL_INSTALL_SCAN_ROOT_KEYS = ("skill_install_scan_roots", "install_scan_roots")
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
WILDCARD_CHARS = set("*?[")
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


def _frontmatter_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _git_repo_root_for_beads(cwd: Path) -> Path | None:
    current = cwd.resolve() if cwd.is_dir() else cwd.resolve().parent
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _skill_metadata_source_dir(item: dict[str, Any]) -> Path | None:
    source = str(item.get("source") or "").strip()
    name = str(item.get("name") or "").strip()
    if not source:
        return None
    source_path = Path(os.path.expandvars(os.path.expanduser(source)))
    if not source_path.is_absolute():
        return None
    source_path = source_path.resolve()
    if (source_path / "SKILL.md").is_file():
        return source_path
    if name and (source_path / name / "SKILL.md").is_file():
        return (source_path / name).resolve()
    return None


def _parse_skill_frontmatter(skill_dir: Path) -> dict[str, Any]:
    skill_md = skill_dir / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    body = text[3:end].strip()
    if not body:
        return {}
    if yaml is None:
        return {}
    try:
        parsed = yaml.safe_load(body)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _skill_requires_beads(skill_dir: Path) -> bool:
    frontmatter = _parse_skill_frontmatter(skill_dir)
    metadata = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
    return _frontmatter_truthy(frontmatter.get("requires_beads")) or _frontmatter_truthy(
        metadata.get("requires_beads")
    )


def _beads_required_skills(effective: list[dict[str, Any]]) -> list[dict[str, Any]]:
    required: dict[str, dict[str, Any]] = {}
    for item in effective:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        skill_dir = _skill_metadata_source_dir(item)
        if skill_dir is None or not _skill_requires_beads(skill_dir):
            continue
        required[name] = {
            "name": name,
            "source": str(skill_dir),
            "layer": item.get("layer"),
        }
    return sorted(required.values(), key=lambda entry: entry["name"])


def _beads_status_for_cwd(effective: list[dict[str, Any]], cwd: Path) -> dict[str, Any]:
    required = _beads_required_skills(effective)
    repo_root = _git_repo_root_for_beads(cwd)
    beads_dir = repo_root / ".beads" if repo_root else None
    br_path = shutil.which("br")
    initialized = bool(beads_dir and beads_dir.is_dir())
    issues: list[dict[str, Any]] = []
    if required and repo_root is None:
        issues.append({
            "code": "no_git_repo",
            "message": "BEADS DRIFT: beads-aware skills are active, but cwd is not inside a git repo",
            "hint": "run from a repo root or repo subdirectory before initializing beads",
        })
    if required and br_path is None:
        issues.append({
            "code": "missing_br",
            "message": "BEADS DRIFT: beads-aware skills are active, but `br` is not on PATH",
            "hint": "install beads_rust, then rerun sbp recalibrate",
        })
    if required and repo_root is not None and not initialized:
        issues.append({
            "code": "no_beads_dir",
            "message": f"BEADS DRIFT: {len(required)} active skill(s) require .beads/ in this repo",
            "hint": f"sbp beads init --cwd {repo_root}",
        })
    return {
        "required": bool(required),
        "required_skills": required,
        "repo_root": str(repo_root) if repo_root else None,
        "beads_dir": str(beads_dir) if beads_dir else None,
        "initialized": initialized,
        "br": br_path,
        "ok": not issues,
        "issues": issues,
        "next_actions": [issue["hint"] for issue in issues if issue.get("hint")],
    }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _expand_policy_path(raw_path: Any) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(str(raw_path)))).resolve())
SOURCE_BUCKET_ORDER = {
    "opensource/skills": 0,
    "skills-private": 1,
    "marketingskills": 2,
    "sweet-potato": 3,
    "local": 4,
    "archive": 9,
    "external": 10,
}


def _path_under_or_equal(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return candidate == root


def _path_prefix_matches(cwd: Path, raw_prefix: str) -> bool:
    prefix = str(raw_prefix or "").strip()
    if not prefix:
        return False
    expanded = Path(os.path.expandvars(os.path.expanduser(prefix))).resolve()
    return _path_under_or_equal(cwd, expanded)


def _path_name_matches_client_id(cwd: Path, client_id: str) -> bool:
    normalized_id = client_id.lower().replace("-", "_")
    if not normalized_id:
        return False
    for part in cwd.parts:
        normalized_part = part.lower().replace("-", "_")
        if normalized_part == normalized_id:
            return True
        if normalized_id in normalized_part:
            return True
    return False


def matched_skill_clients(model: dict[str, Any], cwd: Path) -> list[dict[str, Any]]:
    """Return client overlays whose cwd_match prefixes match cwd."""
    matches: list[dict[str, Any]] = []
    cwd = cwd.resolve()
    for client in model.get("clients") or []:
        context = client.get("context") or {}
        raw_matches = context.get("cwd_match") or []
        if isinstance(raw_matches, str):
            raw_matches = [raw_matches]
        best_prefix = ""
        best_len = -1
        for raw_prefix in raw_matches:
            prefix = str(raw_prefix)
            if _path_prefix_matches(cwd, prefix):
                expanded = str(Path(os.path.expandvars(os.path.expanduser(prefix))).resolve())
                if len(expanded) > best_len:
                    best_prefix = expanded
                    best_len = len(expanded)
        if best_prefix:
            client_id = str(client.get("id", ""))
            default_match = _path_prefix_matches(cwd, str(client.get("default_cwd") or ""))
            path_name_match = _path_name_matches_client_id(cwd, client_id)
            matches.append({
                "id": client_id,
                "label": str(client.get("label") or client.get("id") or ""),
                "match": best_prefix,
                "_default_match": default_match,
                "_path_name_match": path_name_match,
            })
    ordered = sorted(
        matches,
        key=lambda item: (
            not bool(item.get("_path_name_match")),
            -len(item["match"]),
            not bool(item.get("_default_match")),
            item["id"],
        ),
    )
    return [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in ordered
    ]


def _load_lock_records(lock_path: Path) -> tuple[dict[str, dict[str, Any]], str | None]:
    if not lock_path.is_file():
        return {}, None
    try:
        payload = load_json_file(lock_path)
    except RuntimeError as exc:
        return {}, str(exc)

    records: dict[str, dict[str, Any]] = {}
    for entry in payload.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if name:
            records[name] = entry
    return records, None


def _skill_repo_declared_source(entry: dict[str, Any], config_path: Path) -> tuple[str, str]:
    if entry.get("repo"):
        return "repo", str(entry["repo"])
    if entry.get("path"):
        raw_source = str(entry["path"])
        source_path = Path(os.path.expandvars(os.path.expanduser(raw_source)))
        if not source_path.is_absolute():
            source_path = (config_path.parent / source_path).resolve()
        return "path", str(source_path)
    if entry.get("distributor"):
        return "distributor", str(entry["distributor"])
    return "unknown", ""


def _skill_repo_declared_names(entry: dict[str, Any], source_kind: str, source: str) -> list[str]:
    pick = entry.get("pick")
    if isinstance(pick, list) and pick:
        return [str(item) for item in pick if str(item).strip()]
    if source_kind == "repo" and source:
        return [source.split("/")[-1]]
    if source_kind == "path" and source:
        return [Path(source).name]
    return []


def _declared_skill_repo_entries(
    entry: dict[str, Any],
    *,
    config_path: Path,
    index: int,
) -> list[dict[str, Any]]:
    source_kind, source = _skill_repo_declared_source(entry, config_path)
    declared_ref = entry.get("ref")
    return [
        {
            "name": name,
            "source_kind": source_kind,
            "source": source,
            "declared_ref": declared_ref,
            "config_index": index,
        }
        for name in _skill_repo_declared_names(entry, source_kind, source)
    ]


def _declared_entries_from_config(
    skillset: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
    try:
        config = load_skill_repos_config(config_path)
    except RuntimeError as exc:
        return [], str(exc)

    declared: list[dict[str, Any]] = []
    for index, entry in enumerate(config.get("skill_repos") or []):
        if not isinstance(entry, dict):
            continue
        declared.extend(_declared_skill_repo_entries(entry, config_path=config_path, index=index))
    return declared, None


def _skillset_layer(skillset: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    skillset_id = str(skillset.get("id", ""))
    if skillset_id == "default-skills":
        return {
            "id": "default",
            "label": "default",
            "rank": DEFAULT_LAYER_RANK,
            "scope": "default",
        }

    active_clients = [str(item) for item in model.get("active_clients") or []]
    for client_id in active_clients:
        if skillset_id == f"{client_id}-skills":
            return {
                "id": f"client:{client_id}",
                "label": client_id,
                "rank": CLIENT_LAYER_RANK,
                "scope": "client",
            }

    return {
        "id": f"skillset:{skillset_id}",
        "label": skillset_id,
        "rank": CLIENT_LAYER_RANK - 1,
        "scope": "skillset",
    }


def _target_states_for_skill(
    skillset: dict[str, Any],
    skill_name: str,
    lock_record: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    lock_sha = (lock_record or {}).get("install_tree_sha")
    for target in skillset.get("install_targets") or []:
        target_root = Path(str(target.get("host_path", "")))
        install_dir = target_root / skill_name
        installed_sha = directory_tree_sha256(install_dir) if install_dir.is_dir() else None
        if not install_dir.is_dir():
            state = "missing"
        elif lock_sha and installed_sha == lock_sha:
            state = "ok"
        elif lock_sha:
            state = "stale"
        else:
            state = "present"
        states.append({
            "id": str(target.get("id", "")),
            "path": str(install_dir),
            "state": state,
            "tree_sha256": installed_sha,
        })
    return states


def _source_bucket(path: str) -> str:
    raw = path or ""
    expanded = os.path.realpath(os.path.expandvars(os.path.expanduser(raw)))
    home = str(Path.home())
    buckets = [
        (f"{home}/repos/opensource/skills", "opensource/skills"),
        (f"{home}/repos/skills-private", "skills-private"),
        (f"{home}/repos/marketingskills", "marketingskills"),
        (f"{home}/repos/sweet-potato", "sweet-potato"),
        (f"{home}/projects/jsm-skill-archive", "archive"),
        (f"{home}/projects/jsm-skill-archive-", "archive"),
    ]
    for prefix, bucket in buckets:
        if (
            expanded == prefix
            or expanded.startswith(prefix + os.sep)
            or (prefix.endswith("-") and expanded.startswith(prefix))
        ):
            return bucket
    if expanded.startswith(home + os.sep):
        return "local"
    return "external"


def _overlay_state_path() -> Path:
    raw = os.environ.get(OVERLAY_STATE_ENV) or OVERLAY_STATE_DEFAULT
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def active_overlays() -> set[str]:
    """Return the set of overlay names currently enabled for this operator.

    Reads from the file at $SKILLBOX_OVERLAY_STATE (default
    ~/.skillbox-state/overlays), one overlay name per line. $SKILLBOX_OVERLAYS
    env var (comma-separated) augments the file so agent sessions can opt in
    ephemerally without flipping global state.
    """
    overlays: set[str] = set()
    state_path = _overlay_state_path()
    if state_path.is_file():
        try:
            for line in state_path.read_text(encoding="utf-8").splitlines():
                name = line.strip()
                if name and not name.startswith("#"):
                    overlays.add(name)
        except OSError:
            pass
    for item in (os.environ.get(OVERLAY_ENV_VAR) or "").split(","):
        name = item.strip()
        if name:
            overlays.add(name)
    return overlays


def set_overlay(name: str, enabled: bool) -> bool:
    """Persist overlay toggle. Returns the new enabled state."""
    state_path = _overlay_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    current: set[str] = set()
    if state_path.is_file():
        for line in state_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                current.add(stripped)
    if enabled:
        current.add(name)
    else:
        current.discard(name)
    atomic_write_text(
        state_path,
        "\n".join(sorted(current)) + ("\n" if current else ""),
    )
    return enabled


def toggle_overlay(name: str) -> bool:
    """Flip overlay state and return the new enabled flag."""
    return set_overlay(name, name not in active_overlays())


def overlay_scoped_skill_names(model: dict[str, Any], overlay_name: str) -> set[str]:
    """Literal skill names declared by rules tagged with this overlay.

    Walks scope policies directly (not through _scope_rules, which filters
    overlay-off rules). Glob patterns are skipped — only exact names are
    returned, since the unlink path is not a wildcard prune.
    """
    names: set[str] = set()
    target = overlay_name.strip()
    if not target:
        return names
    for policy in _operator_scope_policies(model):
        for raw_rule in policy.get("rules") or []:
            if not isinstance(raw_rule, dict):
                continue
            if str(raw_rule.get("overlay") or "").strip() != target:
                continue
            for item in (
                raw_rule.get("skills")
                or raw_rule.get("patterns")
                or raw_rule.get("names")
                or []
            ):
                name = str(item).strip()
                if name and not any(ch in name for ch in "*?["):
                    names.add(name)
    return names


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

    This is equivalent to ``SKILLBOX_OVERLAYS=<overlay_name> skill sync``
    narrowed to ``cwd`` — it runs the SAME policy evaluation as ``skill sync``
    rather than blindly linking every literal overlay-tagged skill. The named
    overlay is treated as active only for the duration of this call (the
    ``SKILLBOX_OVERLAYS`` env var that ``active_overlays`` reads is patched and
    restored), so NO overlay state is persisted.

    The sync plan it builds is the contract: ``--dry-run`` previews exactly the
    set ``apply`` would link, so activating an overlay in a cwd that the policy
    does not match links the policy-correct set (often zero), never all of the
    overlay's literal skills.
    """
    target = (overlay_name or "").strip()
    if not target:
        return []

    previous = os.environ.get(OVERLAY_ENV_VAR)
    forced = [item for item in (previous or "").split(",") if item.strip()]
    if target not in forced:
        forced.append(target)
    os.environ[OVERLAY_ENV_VAR] = ",".join(forced)
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
            os.environ.pop(OVERLAY_ENV_VAR, None)
        else:
            os.environ[OVERLAY_ENV_VAR] = previous

    return _activations_from_sync_plan(plan)


def _load_scope_policy(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        return None
    return raw


def _operator_scope_policies(model: dict[str, Any]) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = []
    env = model.get("env") or {}
    clients_root = str(
        env.get("SKILLBOX_CLIENTS_HOST_ROOT")
        or env.get("SKILLBOX_CLIENTS_ROOT")
        or ""
    ).strip()
    if clients_root:
        config_root = Path(os.path.expandvars(os.path.expanduser(clients_root))).resolve().parent
        for file_name in SKILL_SCOPE_POLICY_FILES:
            policy = _load_scope_policy(config_root / file_name)
            if policy is not None:
                policy.setdefault("_policy_path", str(config_root / file_name))
                policies.append(policy)

    active_clients = {str(item) for item in model.get("active_clients") or []}
    for client in model.get("clients") or []:
        client_id = str(client.get("id", ""))
        if active_clients and client_id not in active_clients:
            continue
        context = client.get("context") or {}
        policy = context.get("skill_scope")
        if isinstance(policy, dict):
            policy = dict(policy)
            policy.setdefault("_policy_path", f"client:{client_id}:context.skill_scope")
            policies.append(policy)

    return policies


def _project_categories_for_policy(policy: dict[str, Any]) -> list[dict[str, Any]]:
    raw_categories = policy.get("project_categories") or {}
    categories: list[dict[str, Any]] = []

    if isinstance(raw_categories, dict):
        iterator = raw_categories.items()
    else:
        iterator = []
        if isinstance(raw_categories, list):
            iterator = [
                (str(item.get("id") or item.get("name") or ""), item)
                for item in raw_categories
                if isinstance(item, dict)
            ]

    for category_id, raw_category in iterator:
        category_name = str(category_id).strip()
        if not category_name:
            continue
        if isinstance(raw_category, dict):
            raw_paths = raw_category.get("paths") or raw_category.get("allowed_paths") or []
            notes = str(raw_category.get("notes") or raw_category.get("description") or "")
        else:
            raw_paths = raw_category
            notes = ""
        paths = [
            _expand_policy_path(item)
            for item in _as_list(raw_paths)
            if str(item).strip()
        ]
        categories.append({
            "id": category_name,
            "paths": paths,
            "notes": notes,
            "policy_path": str(policy.get("_policy_path") or ""),
        })

    return categories


def _project_categories(model: dict[str, Any]) -> list[dict[str, Any]]:
    categories_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for policy in _operator_scope_policies(model):
        for category in _project_categories_for_policy(policy):
            categories_by_key[(category["policy_path"], category["id"])] = category
    return sorted(
        categories_by_key.values(),
        key=lambda item: (str(item.get("policy_path") or ""), str(item.get("id") or "")),
    )


def _matched_project_categories(model: dict[str, Any], cwd: Path) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    cwd = cwd.resolve()
    for category in _project_categories(model):
        matched_paths = [
            path for path in category.get("paths") or []
            if _path_prefix_matches(cwd, path)
        ]
        if not matched_paths:
            continue
        item = dict(category)
        item["match"] = max(matched_paths, key=len)
        matches.append(item)
    return sorted(matches, key=lambda item: (-len(str(item.get("match") or "")), item["id"]))


def _policy_categories_by_id(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(category.get("id")): category
        for category in _project_categories_for_policy(policy)
    }


def _scope_rule_patterns(raw_rule: dict[str, Any]) -> list[str]:
    raw_patterns = raw_rule.get("skills") or raw_rule.get("patterns") or raw_rule.get("names") or []
    return [str(item).strip() for item in raw_patterns if str(item).strip()]


def _scope_rule_category_ids(raw_rule: dict[str, Any]) -> list[str]:
    return [
        str(item).strip()
        for item in _as_list(raw_rule.get("categories") or raw_rule.get("project_categories"))
        if str(item).strip()
    ]


def _scope_rule_direct_paths(raw_rule: dict[str, Any]) -> list[str]:
    raw_paths = [
        item
        for item in _as_list(raw_rule.get("paths") or raw_rule.get("allowed_paths"))
        if str(item).strip()
    ]
    return [_expand_policy_path(item) for item in raw_paths]


def _scope_rule_paths(
    raw_rule: dict[str, Any],
    categories: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    category_ids = _scope_rule_category_ids(raw_rule)
    paths = _scope_rule_direct_paths(raw_rule)
    unknown_categories: list[str] = []
    for category_id in category_ids:
        category = categories.get(category_id)
        if not category:
            unknown_categories.append(category_id)
            continue
        paths.extend(str(path) for path in category.get("paths") or [])
    return sorted(set(paths)), category_ids, unknown_categories


def _scope_rule_from_raw(
    raw_rule: dict[str, Any],
    *,
    index: int,
    policy: dict[str, Any],
    categories: dict[str, dict[str, Any]],
    overlays_on: set[str],
) -> dict[str, Any] | None:
    overlay = str(raw_rule.get("overlay") or "").strip()
    if overlay and overlay not in overlays_on:
        return None
    patterns = _scope_rule_patterns(raw_rule)
    if not patterns:
        return None
    paths, category_ids, unknown_categories = _scope_rule_paths(raw_rule, categories)
    return {
        "id": str(raw_rule.get("id") or f"rule-{index}"),
        "patterns": patterns,
        "paths": paths,
        "categories": category_ids,
        "unknown_categories": unknown_categories,
        "allow_global": bool(raw_rule.get("allow_global", False)),
        "default": raw_rule.get("default", "on"),
        "activation": str(raw_rule.get("activation") or "").strip(),
        "notes": str(raw_rule.get("notes") or raw_rule.get("reason") or ""),
        "overlay": overlay,
        "policy_path": str(policy.get("_policy_path") or ""),
    }


def _scope_rules(model: dict[str, Any]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    overlays_on = active_overlays()
    for policy in _operator_scope_policies(model):
        categories = _policy_categories_by_id(policy)
        for index, raw_rule in enumerate(policy.get("rules") or []):
            if not isinstance(raw_rule, dict):
                continue
            rule = _scope_rule_from_raw(
                raw_rule,
                index=index,
                policy=policy,
                categories=categories,
                overlays_on=overlays_on,
            )
            if rule is not None:
                rules.append(rule)
    return rules


def _policy_skill_source_patterns(model: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    for policy in _operator_scope_policies(model):
        for key in SKILL_SOURCE_ROOT_KEYS:
            raw_patterns = policy.get(key)
            if raw_patterns is None:
                continue
            if isinstance(raw_patterns, str):
                raw_patterns = [raw_patterns]
            patterns.extend(str(item).strip() for item in raw_patterns or [] if str(item).strip())
    return patterns


def _policy_skill_install_scan_patterns(model: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    for policy in _operator_scope_policies(model):
        for key in SKILL_INSTALL_SCAN_ROOT_KEYS:
            raw_patterns = policy.get(key)
            if raw_patterns is None:
                continue
            if isinstance(raw_patterns, str):
                raw_patterns = [raw_patterns]
            patterns.extend(str(item).strip() for item in raw_patterns or [] if str(item).strip())
    return patterns


def _expand_skill_source_patterns(patterns: list[str]) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for raw_pattern in patterns:
        expanded = os.path.expandvars(os.path.expanduser(str(raw_pattern)))
        matches = glob.glob(expanded)
        if not matches:
            matches = [expanded]
        for match in matches:
            root = Path(match).resolve()
            key = str(root)
            if key not in seen:
                seen.add(key)
                roots.append(root)
    return roots


def _global_allow_patterns(model: dict[str, Any]) -> list[str] | None:
    patterns: list[str] = []
    has_explicit_policy = False
    for policy in _operator_scope_policies(model):
        raw_allowlist = policy.get("global_allowlist")
        if raw_allowlist is not None:
            has_explicit_policy = True
            patterns.extend(str(item).strip() for item in raw_allowlist or [] if str(item).strip())
        for rule in policy.get("rules") or []:
            if not isinstance(rule, dict) or not bool(rule.get("allow_global", False)):
                continue
            raw_patterns = rule.get("skills") or rule.get("patterns") or rule.get("names") or []
            patterns.extend(str(item).strip() for item in raw_patterns if str(item).strip())
    if not has_explicit_policy:
        return None
    return sorted(set(patterns))


def _matching_scope_rule(
    skill_name: str,
    rules: list[dict[str, Any]],
    *,
    cwd: Path | None = None,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for rule in rules:
        for pattern in rule.get("patterns") or []:
            if fnmatch.fnmatchcase(skill_name, str(pattern)):
                matches.append(rule)
                break
    if cwd is not None:
        cwd = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(str(cwd)))))
        path_matches = [
            rule for rule in matches
            if any(_path_prefix_matches(cwd, path) for path in rule.get("paths") or [])
        ]
        if path_matches:
            return max(
                path_matches,
                key=lambda rule: max(
                    len(str(path))
                    for path in rule.get("paths") or []
                    if _path_prefix_matches(cwd, path)
                ),
            )
    if matches:
        return matches[0]
    return None


def _path_is_under(path: str, roots: list[str]) -> bool:
    if not path:
        return False
    candidate = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(path))))
    for raw_root in roots:
        root = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(raw_root))))
        if _path_under_or_equal(candidate, root):
            return True
    return False


def _skill_scope_violations(
    model: dict[str, Any],
    occurrences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rules = _scope_rules(model)
    if not rules:
        return []

    violations: list[dict[str, Any]] = []
    for occurrence in occurrences:
        if occurrence.get("availability") != "installed":
            continue
        name = str(occurrence.get("name") or "")
        install_path = str(occurrence.get("path") or "")
        rule = _matching_scope_rule(
            name,
            rules,
            cwd=Path(install_path) if install_path else None,
        )
        if not rule:
            continue

        layer = str(occurrence.get("layer") or "")
        allowed = False
        reason = ""
        if layer.startswith("global:"):
            allowed = bool(rule.get("allow_global"))
            reason = "global install is not allowed by this skill scope rule"
        else:
            allowed = _path_is_under(install_path, list(rule.get("paths") or []))
            reason = "installed outside allowed repo path"

        if not allowed:
            violation = dict(occurrence)
            violation["scope_rule"] = rule["id"]
            violation["scope_policy_path"] = rule.get("policy_path")
            violation["allowed_paths"] = list(rule.get("paths") or [])
            violation["reason"] = reason
            violations.append(violation)

    return violations


def _is_literal_skill_pattern(pattern: str) -> bool:
    return not any(char in pattern for char in WILDCARD_CHARS)


def _skill_is_effective(effective: list[dict[str, Any]], skill_name: str) -> bool:
    for item in effective:
        if str(item.get("name") or "") == skill_name and item.get("state") != "broken":
            return True
    return False


def _matched_scope_rules_for_cwd(model: dict[str, Any], cwd: Path) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    cwd = cwd.resolve()
    for rule in _scope_rules(model):
        paths = list(rule.get("paths") or [])
        matched_paths = [path for path in paths if _path_prefix_matches(cwd, path)]
        if not matched_paths:
            continue
        item = dict(rule)
        item["match"] = max(matched_paths, key=len)
        matched.append(item)
    return sorted(matched, key=lambda item: (-len(str(item.get("match") or "")), item["id"]))


def _scope_rule_is_expected_by_default(rule: dict[str, Any]) -> bool:
    raw_default = rule.get("default", "on")
    if isinstance(raw_default, bool):
        default_on = raw_default
    else:
        default_on = str(raw_default or "on").strip().lower() not in {
            "off",
            "false",
            "manual",
            "on-demand",
            "on_demand",
        }
    activation = str(rule.get("activation") or "").strip().lower()
    return default_on and activation not in {"manual", "on-demand", "on_demand"}


def _missing_for_cwd(
    model: dict[str, Any],
    cwd: Path,
    effective: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for rule in _matched_scope_rules_for_cwd(model, cwd):
        if not _scope_rule_is_expected_by_default(rule):
            continue
        for pattern in rule.get("patterns") or []:
            skill_name = str(pattern)
            if not _is_literal_skill_pattern(skill_name):
                continue
            if _skill_is_effective(effective, skill_name):
                continue
            missing.append({
                "name": skill_name,
                "scope_rule": rule.get("id"),
                "scope_policy_path": rule.get("policy_path"),
                "allowed_paths": list(rule.get("paths") or []),
                "categories": list(rule.get("categories") or []),
                "reason": "skill is expected for this cwd but is not currently effective",
            })
    return sorted(missing, key=lambda item: (item["name"], str(item.get("scope_rule") or "")))


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
            "hint": (
                "Add this skill to the active client's skill-repos.yaml or install it "
                "under a repo-local .claude/skills or .codex/skills in one of the allowed paths."
            ),
        })
    for item in issues.get("scope_violations") or []:
        _add_skill_visibility_recommendation(recommendations, seen, {
            "action": "move_or_unlink_skill",
            "skill": item.get("name"),
            "scope_rule": item.get("scope_rule"),
            "source_path": item.get("path"),
            "allowed_paths": item.get("allowed_paths") or [],
            "hint": "Move this project-local install under an allowed repo path, or unlink it here.",
        })
    for item in issues.get("global_not_allowed") or []:
        _add_skill_visibility_recommendation(recommendations, seen, {
            "action": "move_global_to_project",
            "skill": item.get("name"),
            "source_path": item.get("path"),
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
            "hint": "Declare this skill in the global policy or remove the user-global link.",
        })
    return recommendations


def _repo_root_for_skill_install(cwd: Path) -> Path:
    cwd = cwd.resolve()
    home = Path.home().resolve()
    current = cwd if cwd.is_dir() else cwd.parent
    for parent in [current, *current.parents]:
        if parent == home:
            break
        if (parent / ".git").exists():
            return parent
    return current


def _category_by_id(model: dict[str, Any], category_id: str) -> dict[str, Any] | None:
    for category in _project_categories(model):
        if str(category.get("id") or "") == category_id:
            return category
    return None


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
            rule = _matching_scope_rule(skill_name, _scope_rules(model), cwd=cwd)
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

    rule = _matching_scope_rule(skill_name, _scope_rules(model), cwd=cwd)
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
        rule = _matching_scope_rule(skill_name, _scope_rules(model), cwd=cwd)
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


def _scope_allows_global(model: dict[str, Any], skill_name: str) -> bool:
    rule = _matching_scope_rule(skill_name, _scope_rules(model))
    return bool(rule and rule.get("allow_global"))


def _global_install_allowed(model: dict[str, Any], skill_name: str) -> bool:
    patterns = _global_allow_patterns(model)
    if patterns is None:
        return True
    return any(fnmatch.fnmatchcase(skill_name, pattern) for pattern in patterns)


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


def _installed_skill_name(path: Path) -> str:
    if path.suffix == ".skill":
        return path.stem
    return path.name


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


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


def _realpath(path: Path | str) -> str:
    return os.path.realpath(os.path.expandvars(os.path.expanduser(str(path))))


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
        key = str(root.resolve())
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
#                  a DIFFERENT machine profile (e.g. /Users/b/repos/... seen from
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


def _machines_classifier() -> tuple[Any, str | None]:
    """Best-effort ``(MachinesConfig, current_machine_id)`` for foreign-path checks.

    Resolution flows through ``runtime_manager.machines`` (the same profile API
    used by ``_explain_machine_profile``). A missing/unparseable machines.yaml or
    an undetectable machine yields ``(None, None)`` so taxonomy degrades to
    moved/dangling rather than raising — broken-link triage must answer even on
    boxes that have not declared a profile. Overridable via the module-level
    ``_machines_classifier_override`` hook so tests can inject a canonical-schema
    config without depending on the live host identity.
    """
    override = globals().get("_machines_classifier_override")
    if override is not None:
        return override()
    try:
        from . import machines as _machines  # noqa: PLC0415
    except Exception:  # pragma: no cover - import guard
        return None, None
    try:
        config = _machines.load_machines_config()
    except Exception:
        return None, None
    try:
        machine_id = config.detect_machine_id()
    except Exception:
        machine_id = None
    return config, machine_id


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
    path = str(occurrence.get("path") or "")
    name = str(occurrence.get("name") or "")
    if origin == "moved" and source_options:
        source = str(source_options[0].get("source") or "")
        return f"ln -sfn {source} {path}"
    if origin == "dangling":
        return f"rm {path}  # prune dead link {name!r}"
    if origin == "other-machine":
        target = str(occurrence.get("link_target") or occurrence.get("link_target_abs") or "")
        suffix = f" (target {target} belongs to another machine"
        suffix += f", not {machine_id!r})" if machine_id else ")"
        return f"rm {path}  # migrate: drop foreign-machine link{suffix}"
    # unreadable: do not guess; show the operator what to inspect.
    return f"ls -ld {path}  # investigate unreadable link (permission/loop?)"


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
         is the mhb case: 47 links all under /Users/b are ONE decision).
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


def collect_skill_visibility(
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    include_global: bool = True,
    include_project: bool = True,
    include_sources: bool = False,
) -> dict[str, Any]:
    """Collect a conflict-aware skill availability view for a model."""
    cwd_path = Path(cwd or os.getcwd()).resolve()
    declared_occurrences, declared_layers = _declared_skill_occurrences(model)
    installed_occurrences, installed_layers = _collect_installed_visibility_layers(
        cwd_path,
        include_global=include_global,
        include_project=include_project,
    )
    occurrences = [*declared_occurrences, *installed_occurrences]
    layers = [*declared_layers, *installed_layers]
    # Classify broken installed links up front so the taxonomy fields (origin,
    # suggested_action, fix_command) flow into both occurrences and the issue
    # groups that reference the same dicts.
    _enrich_broken_links(model, occurrences)

    effective, shadowed = _effective_occurrences(occurrences)
    issues = _visibility_issue_groups(
        model,
        cwd_path,
        occurrences,
        declared_occurrences,
        effective,
        shadowed,
    )
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
    next_actions = skill_visibility_next_actions(issues)
    for action in beads.get("next_actions") or []:
        if action not in next_actions:
            next_actions.append(action)

    return {
        "cwd": str(cwd_path),
        "matched_clients": matched_skill_clients(model, cwd_path),
        "matched_project_categories": _matched_project_categories(model, cwd_path),
        "matched_scope_rules": _matched_scope_rules_for_cwd(model, cwd_path),
        "active_clients": model.get("active_clients") or [],
        "active_profiles": model.get("active_profiles") or [],
        "global_surfaces": global_home_surfaces_report() if include_global else [],
        "layers": sorted(layers, key=lambda item: int(item.get("rank", 0))),
        "source_roots": sorted(source_roots, key=lambda item: str(item.get("path") or "")),
        "effective": effective,
        "occurrences": occurrences,
        "undefined_sources": undefined_sources,
        "beads": beads,
        "issues": issues,
        "policy": {
            "files": policy_files,
            "project_categories": _project_categories(model),
        },
        "recommendations": recommendations,
        "summary": summary,
        "next_actions": next_actions,
    }


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
    expanded = _expand_policy_path(raw)
    item = candidates.setdefault(expanded, {"path": expanded, "sources": []})
    if source not in item["sources"]:
        item["sources"].append(source)


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


def _classified_broken_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact per-link taxonomy rows for the audit (one row per broken link).

    Each row carries the classification a triager acts on: ``name``, ``path``,
    ``origin``, ``suggested_action`` and the exact ``fix_command``. ``origin``
    defaults to ``dangling`` if an item was never enriched, mirroring
    :func:`broken_link_class_counts`.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                "name": str(item.get("name") or ""),
                "path": str(item.get("path") or ""),
                "link_target": str(item.get("link_target") or ""),
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


def _skill_audit_repo_row(
    candidate: dict[str, Any],
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "path": candidate["path"],
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
        "missing_for_cwd": _skill_names(issues.get("missing_for_cwd") or []),
        "scope_violations": _skill_names(issues.get("scope_violations") or []),
        "broken_project": _skill_names(issues.get("broken_project") or []),
        # Per-link triage taxonomy + counts-by-class so the audit answers
        # "what kind of broken is this" (relink / prune / migrate / investigate)
        # rather than re-listing N undifferentiated names.
        "broken_project_links": _classified_broken_rows(issues.get("broken_project") or []),
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
        "broken_global_links": _classified_broken_rows(issues.get("broken_global") or []),
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
        },
        "global": global_row,
        "repos": repos,
        "next_actions": _skill_audit_next_actions(repos_with_issues, global_row, overlays, active),
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
        "state": item.get("state"),
        "source_bucket": item.get("source_bucket"),
        "source": item.get("source"),
        "shadowed_count": item.get("shadowed_count", 0),
    }
    if item.get("path"):
        result["path"] = item.get("path")
    return {key: value for key, value in result.items() if value not in (None, "")}


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
        "effective": [_compact_skill_visibility_skill(item) for item in payload.get("effective") or []],
        "issues": _compact_skill_visibility_issues(payload),
        "beads": payload.get("beads") or {},
        "recommendations": payload.get("recommendations") or [],
        "policy": payload.get("policy") or {},
        "source_roots": payload.get("source_roots") or [],
        "undefined_sources": payload.get("undefined_sources") or [],
        "next_actions": payload.get("next_actions") or [],
    }


EXPLAIN_SCHEMA_VERSION = "2026-06-13+skill_explain"

# Maps an occurrence ``layer`` id (e.g. ``default``, ``client:foo``,
# ``global:claude``, ``project:codex:/repo``) to one of the four ranking
# families the policy ladder is built on (DEFAULT_LAYER_RANK ..
# PROJECT_LAYER_RANK, near the top of this module). PROJECT wins over GLOBAL
# wins over CLIENT wins over DEFAULT.
def _layer_family(occurrence: dict[str, Any]) -> str:
    layer = str(occurrence.get("layer") or "")
    rank = int(occurrence.get("layer_rank") or 0)
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
        "won": won,
    }
    return {key: value for key, value in view.items() if value not in (None, "")} | {"won": won}


def _explain_lost_reason(
    winner: dict[str, Any] | None,
    loser: dict[str, Any],
) -> str:
    """Why this occurrence is NOT the effective one."""
    if loser.get("state") == "broken":
        return "broken link (source target does not resolve here)"
    if winner is None:
        return "no effective occurrence for this skill"
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
    overlays_on = active_overlays()
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
    for rule in _scope_rules(model):
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
    used elsewhere) so a path like ``/srv/repos/...`` vs ``/Users/b/repos/...``
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
        # A real source exists: the narrowest fix is a scoped activate, which
        # both prints the SKILL.md packet now and links it for future sessions.
        remediation.append({
            "rank": 1,
            "kind": "activate",
            "command": f"sbp skill activate {skill_name} --cwd {cwd}",
            "manage_command": (
                f"python3 .env-manager/manage.py skill activate {skill_name} --cwd {cwd}"
            ),
            "why": (
                f"a source for {skill_name!r} exists ({source_options[0].get('source')}); "
                "activating links it here and returns the SKILL.md packet immediately"
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
        (item for item in payload.get("effective") or [] if str(item.get("name") or "") == skill_name),
        None,
    )
    visible = bool(winner) and winner.get("state") != "broken"

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
        elif winner and winner.get("state") == "broken":
            reason = (
                f"{skill_name!r} is NOT visible: the only occurrence is a broken link "
                f"({winner.get('path')})"
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
        "layer_family": _layer_family(winner) if winner else None,
        "layer_label": winner.get("layer_label") if winner else None,
        "layer_rank": winner.get("layer_rank") if winner else None,
        "winner": _explain_occurrence_view(winner, won=True) if winner else None,
        "occurrences": occurrence_views,
        "lost": losers,
        "scope_rules": scope_rules,
        "inactive_overlay_rules": inactive_overlay_rules,
        "source_options": source_options,
        "active_overlays": sorted(active_overlays()),
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


def _join_or_none(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values if str(value)) or "(none)"


def _truncate_names(names: list[str], limit: int) -> str:
    visible = names[: max(0, limit)]
    text = ", ".join(visible) if visible else "-"
    remaining = len(names) - len(visible)
    if remaining > 0:
        text += f" (+{remaining})"
    return text


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
