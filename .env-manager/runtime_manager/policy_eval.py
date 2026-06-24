"""Skill-visibility policy evaluation.

Single responsibility: scope-rule evaluation, layer ranking, overlay state and
matching, and machine/registry/alias resolution -- i.e. deciding *where a skill
is allowed to live* and *which scope rules / overlays / categories apply to a
cwd*. Depends only on ._skill_common.
"""

from __future__ import annotations

import fnmatch
import fcntl
import glob
import hashlib
import os
import shutil
import tempfile
import time
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
    require_yaml,
)

from ._skill_common import *
__all__ = [
    'SKILL_SCOPE_POLICY_FILES',
    'OVERLAY_STATE_ENV',
    'OVERLAY_STATE_DEFAULT',
    'OVERLAY_ENV_VAR',
    'SKILL_SOURCE_ROOT_KEYS',
    'SKILL_INSTALL_SCAN_ROOT_KEYS',
    'WILDCARD_CHARS',
    '_frontmatter_truthy',
    '_git_repo_root_for_beads',
    '_skill_metadata_source_dir',
    '_parse_skill_frontmatter',
    '_skill_requires_beads',
    '_beads_required_skills',
    '_beads_status_for_cwd',
    '_path_name_matches_client_id',
    'matched_skill_clients',
    '_load_lock_records',
    '_skill_repo_declared_source',
    '_skill_repo_declared_names',
    '_declared_skill_repo_entries',
    '_declared_entries_from_config',
    '_skillset_layer',
    '_target_states_for_skill',
    '_overlay_state_path',
    'active_overlays',
    'set_overlay',
    'toggle_overlay',
    '_overlay_default_off',
    'declared_overlay_records',
    'declared_overlays',
    'rule_overlay_tags',
    'undeclared_active_overlays',
    'overlay_scoped_skill_names',
    '_load_scope_policy',
    '_operator_scope_policies',
    '_repo_override_policy',
    'lint_repo_override_policy',
    'update_repo_override_policy',
    'OVERRIDE_WRITE_LOCK_TIMEOUT_SECONDS',
    'OverrideWriteLockTimeout',
    '_project_categories_for_policy',
    '_project_categories',
    '_matched_project_categories',
    '_policy_categories_by_id',
    'REGISTRY_FILE_ENV_VAR',
    'REGISTRY_FILE_REL',
    'RegistryResolutionError',
    '_registry_doctor_module',
    '_registry_file_path',
    '_load_registry_entries',
    '_did_you_mean',
    '_machine_repo_roots',
    '_registry_path_remainder',
    '_resolve_registry_path',
    '_scope_rule_repo_ids',
    '_resolve_scope_rule_repos',
    '_scope_rule_patterns',
    '_scope_rule_category_ids',
    '_scope_rule_direct_paths',
    '_scope_rule_paths',
    '_scope_rule_from_raw',
    '_scope_rules',
    'last_scope_rule_errors',
    '_policy_skill_source_patterns',
    '_policy_skill_install_scan_patterns',
    '_expand_skill_source_patterns',
    '_global_allow_patterns',
    '_matching_scope_rule',
    '_skill_scope_violations',
    '_is_literal_skill_pattern',
    '_skill_is_effective',
    '_matched_scope_rules_for_cwd',
    '_scope_rule_is_expected_by_default',
    '_missing_for_cwd',
    '_repo_root_for_skill_install',
    '_category_by_id',
    '_scope_allows_global',
    '_global_install_allowed',
]


SKILL_SCOPE_POLICY_FILES = ("skill-scope.yaml", "skills-scope.yaml")
SKILL_OVERRIDES_REL = Path(".skillbox") / "skill-overrides.yaml"
OVERLAY_STATE_ENV = "SKILLBOX_OVERLAY_STATE"
OVERLAY_STATE_DEFAULT = "~/.skillbox-state/overlays"
OVERLAY_ENV_VAR = "SKILLBOX_OVERLAYS"
SKILL_SOURCE_ROOT_KEYS = ("skill_source_roots", "source_roots", "skill_roots")
SKILL_INSTALL_SCAN_ROOT_KEYS = ("skill_install_scan_roots", "install_scan_roots")
WILDCARD_CHARS = set("*?[")
OVERRIDE_POLICY_VERSION = 1
OVERRIDE_LIST_KEYS = ("pin_on", "pin_off", "opt_out_global", "defaults")
OVERRIDE_OVERLAY_KEYS = ("enable", "disable")
OVERRIDE_WRITE_LOCK_TIMEOUT_SECONDS = 5.0
_OVERRIDE_WRITE_POLL_INTERVAL_SECONDS = 0.02
OVERRIDE_ALLOWED_KEYS = {
    "version",
    "pin_on",
    "pin_off",
    "opt_out_global",
    "overlays",
    "defaults",
    "reason",
}
OVERRIDE_LINT_SEVERITY_ERROR = "error"
OVERRIDE_LINT_SEVERITY_WARN = "warn"


class OverrideWriteLockTimeout(RuntimeError):
    """Raised when the repo override writer cannot acquire its sidecar lock."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        super().__init__(f"Timed out acquiring skill override lock {self.lock_path}.")


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


def _overlay_default_off(raw: Any) -> bool:
    """Interpret a declared overlay's `default:` field as off (True) or on.

    Overlays are opt-in: a missing or unparseable default is treated as ``off``
    (the conservative mode-pack posture). YAML may parse ``off``/``on`` as
    booleans (False/True) or leave them as strings depending on quoting, so we
    normalise both.
    """
    if isinstance(raw, bool):
        return not raw  # YAML `off` -> False -> default-off True
    text = str(raw or "").strip().lower()
    if text in {"on", "true", "enabled", "yes"}:
        return False
    return True


def declared_overlay_records(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Declared overlays from every in-scope policy's top-level `overlays:` block.

    Each record is ``{name, description, default_off, policy_path}``. The
    ``overlays:`` block is the single declaration point for mode-pack overlays
    (layer 3 of the global skill contract). A list of mappings (``- name: ...``)
    or a mapping (``marketing: {description: ...}``) are both accepted; bare
    string entries (``- marketing``) declare a name with no metadata. Later
    policies (client overlays) win on duplicate names so a client can re-describe
    an operator overlay without creating a phantom duplicate.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for policy in _operator_scope_policies(model):
        policy_path = str(policy.get("_policy_path") or "")
        raw_overlays = policy.get("overlays")
        entries: list[tuple[str, dict[str, Any]]] = []
        if isinstance(raw_overlays, dict):
            entries = [
                (str(key).strip(), value if isinstance(value, dict) else {})
                for key, value in raw_overlays.items()
            ]
        elif isinstance(raw_overlays, list):
            for item in raw_overlays:
                if isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    entries.append((name, item))
                else:
                    entries.append((str(item).strip(), {}))
        for name, meta in entries:
            if not name:
                continue
            by_name[name] = {
                "name": name,
                "description": str(meta.get("description") or "").strip(),
                "default_off": _overlay_default_off(meta.get("default", "off")),
                "policy_path": policy_path,
            }
    return [by_name[name] for name in sorted(by_name)]


def declared_overlays(model: dict[str, Any]) -> set[str]:
    """Set of overlay names declared by an in-scope policy `overlays:` block."""
    return {record["name"] for record in declared_overlay_records(model)}


def rule_overlay_tags(model: dict[str, Any]) -> set[str]:
    """Every distinct ``overlay:`` tag enumerated from scope-policy rules.

    This is the enumeration the unlink/activate paths walk; comparing it against
    :func:`declared_overlays` is what surfaces a ghost (undeclared) overlay tag.
    """
    tags: set[str] = set()
    for policy in _operator_scope_policies(model):
        for raw_rule in policy.get("rules") or []:
            if not isinstance(raw_rule, dict):
                continue
            tag = str(raw_rule.get("overlay") or "").strip()
            if tag:
                tags.add(tag)
    return tags


def undeclared_active_overlays(model: dict[str, Any]) -> list[str]:
    """Active overlay-state entries that name an UNDECLARED overlay.

    An overlay-state-file entry (or ``SKILLBOX_OVERLAYS`` opt-in) for a name with
    no declaration silently filters nothing -- it can never match a rule -- so it
    is a footgun, not an error. The caller surfaces these as AUDIT WARNINGS. When
    no overlay is declared anywhere, there is no registry to validate against, so
    nothing is flagged (mirrors the global-contract lint's empty-policy pass).
    """
    declared = declared_overlays(model)
    if not declared:
        return []
    return sorted(name for name in active_overlays() if name not in declared)


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


def _load_scope_policy(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        return None
    return raw


def _empty_repo_override_policy(
    repo_root: Path,
    policy_path: Path,
    *,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    error_list = list(errors or [])
    return {
        "ok": not error_list,
        "version": OVERRIDE_POLICY_VERSION,
        "pin_on": [],
        "pin_off": [],
        "opt_out_global": [],
        "overlays": {"enable": [], "disable": []},
        "defaults": [],
        "reason": "",
        "errors": error_list,
        "_repo_root": str(repo_root),
        "_policy_path": str(policy_path),
    }


def _override_error(
    policy_path: Path,
    message: str,
    *,
    key: str | None = None,
) -> dict[str, Any]:
    from .errors import OVERRIDE_PARSE_ERROR  # noqa: PLC0415

    error: dict[str, Any] = {
        "code": OVERRIDE_PARSE_ERROR,
        "message": message,
        "path": str(policy_path),
    }
    if key is not None:
        error["key"] = key
    return error


def _override_name_list(value: Any) -> list[str]:
    return sorted(
        {
            str(item).strip()
            for item in _as_list(value)
            if str(item).strip()
        }
    )


def _repo_override_policy(cwd: str | os.PathLike[str] | Path) -> dict[str, Any]:
    """Read the repo-local skill override policy anchored at the git root.

    Missing files resolve to an empty policy. Malformed YAML or schema
    violations are reported in ``errors`` while the effective override payload
    fails safe to empty lists.
    """
    cwd_path = Path(os.path.expandvars(os.path.expanduser(str(cwd))))
    repo_root = _repo_root_for_skill_install(cwd_path)
    policy_path = repo_root / SKILL_OVERRIDES_REL
    empty_policy = _empty_repo_override_policy(repo_root, policy_path)

    if not policy_path.is_file():
        return empty_policy

    try:
        raw = load_yaml(policy_path)
    except RuntimeError as exc:
        return _empty_repo_override_policy(
            repo_root,
            policy_path,
            errors=[_override_error(policy_path, str(exc))],
        )
    if not isinstance(raw, dict):
        return _empty_repo_override_policy(
            repo_root,
            policy_path,
            errors=[_override_error(policy_path, "skill override policy must be a mapping")],
        )

    errors: list[dict[str, Any]] = []
    unknown_keys = sorted(str(key) for key in set(raw) - OVERRIDE_ALLOWED_KEYS)
    for key in unknown_keys:
        errors.append(
            _override_error(policy_path, f"unknown skill override key: {key}", key=key)
        )

    if raw.get("version") != OVERRIDE_POLICY_VERSION:
        errors.append(
            _override_error(
                policy_path,
                f"skill override version must be {OVERRIDE_POLICY_VERSION}",
                key="version",
            )
        )

    overlays_value = raw.get("overlays")
    overlays_raw = overlays_value or {}
    if overlays_value is not None and not isinstance(overlays_value, dict):
        errors.append(
            _override_error(policy_path, "overlays must be a mapping", key="overlays")
        )
        overlays_raw = {}
    for key in sorted(set(overlays_raw) - set(OVERRIDE_OVERLAY_KEYS)):
        errors.append(
            _override_error(
                policy_path,
                f"unknown overlays key: {key}",
                key=f"overlays.{key}",
            )
        )

    policy = _empty_repo_override_policy(repo_root, policy_path, errors=errors)
    if errors:
        return policy

    for key in OVERRIDE_LIST_KEYS:
        policy[key] = _override_name_list(raw.get(key))
    policy["overlays"] = {
        key: _override_name_list(overlays_raw.get(key))
        for key in OVERRIDE_OVERLAY_KEYS
    }
    policy["reason"] = str(raw.get("reason") or "").strip()
    return policy


def _override_entry_locations(policy_path: Path) -> dict[str, dict[str, list[int]]]:
    """Best-effort map of override list entries to 1-based YAML line numbers."""
    locations: dict[str, dict[str, list[int]]] = {}
    if yaml is None or not policy_path.is_file():
        return locations
    try:
        document = yaml.compose(policy_path.read_text(encoding="utf-8"))
    except Exception:
        return locations
    if document is None:
        return locations

    def _line(node: Any) -> int:
        mark = getattr(node, "start_mark", None)
        return int(getattr(mark, "line", 0)) + 1

    def _record(section: str, raw_name: Any, line: int) -> None:
        name = str(raw_name).strip()
        if not name:
            return
        locations.setdefault(section, {}).setdefault(name, []).append(line)

    def _scalar_entries(value_node: Any) -> list[tuple[str, int]]:
        node_id = str(getattr(value_node, "id", ""))
        if node_id == "scalar":
            return [(str(getattr(value_node, "value", "")).strip(), _line(value_node))]
        if node_id != "sequence":
            return []
        entries: list[tuple[str, int]] = []
        for item_node in getattr(value_node, "value", []) or []:
            if str(getattr(item_node, "id", "")) != "scalar":
                continue
            entries.append((str(getattr(item_node, "value", "")).strip(), _line(item_node)))
        return entries

    if str(getattr(document, "id", "")) != "mapping":
        return locations

    for key_node, value_node in getattr(document, "value", []) or []:
        key = str(getattr(key_node, "value", "")).strip()
        if key in OVERRIDE_LIST_KEYS:
            for name, line in _scalar_entries(value_node):
                _record(key, name, line)
        elif key == "overlays" and str(getattr(value_node, "id", "")) == "mapping":
            for overlay_key_node, overlay_value_node in getattr(value_node, "value", []) or []:
                overlay_key = str(getattr(overlay_key_node, "value", "")).strip()
                if overlay_key not in OVERRIDE_OVERLAY_KEYS:
                    continue
                section = f"overlays.{overlay_key}"
                for name, line in _scalar_entries(overlay_value_node):
                    _record(section, name, line)
    return locations


def _first_location(
    locations: dict[str, dict[str, list[int]]],
    section: str,
    name: str,
) -> int | None:
    lines = locations.get(section, {}).get(name) or []
    return lines[0] if lines else None


def _override_lint_finding(
    *,
    rule: str,
    severity: str,
    skill: str | None,
    explanation: str,
    suggested_fix: str,
    policy_path: str,
    line: int | None = None,
    lines: dict[str, int | None] | None = None,
    code: str | None = None,
    did_you_mean: str | None = None,
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "rule": rule,
        "severity": severity,
        "explanation": explanation,
        "suggested_fix": suggested_fix,
        "policy_path": policy_path,
    }
    if code:
        finding["code"] = code
    if skill:
        finding["skill"] = skill
    if line is not None:
        finding["line"] = line
    if lines:
        finding["lines"] = lines
    if did_you_mean:
        finding["did_you_mean"] = did_you_mean
    return finding


def _override_parse_findings(policy: dict[str, Any]) -> list[dict[str, Any]]:
    policy_path = str(policy.get("_policy_path") or "")
    findings: list[dict[str, Any]] = []
    for error in policy.get("errors") or []:
        message = str(error.get("message") or "could not parse skill override policy")
        findings.append(
            _override_lint_finding(
                rule="parse_error",
                severity=OVERRIDE_LINT_SEVERITY_ERROR,
                skill=None,
                explanation=message,
                suggested_fix="Fix the override YAML/schema issue, then rerun `sbp skill lint`.",
                policy_path=policy_path,
                code=str(error.get("code") or "OVERRIDE_PARSE_ERROR"),
            )
        )
    return findings


def lint_repo_override_policy(
    cwd: str | os.PathLike[str] | Path,
    *,
    known_skill_names: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Static lint for ``.skillbox/skill-overrides.yaml``.

    The reader already fails safe on malformed files. This layer converts those
    non-fatal reader errors plus semantic contradictions into structured
    findings so doctor/recalibrate can report every issue they can still see.
    """
    policy = _repo_override_policy(cwd)
    policy_path = str(policy.get("_policy_path") or "")
    locations = _override_entry_locations(Path(policy_path))
    findings = _override_parse_findings(policy)
    known = {
        str(name).strip()
        for name in (known_skill_names or [])
        if str(name).strip()
    }

    if policy.get("ok"):
        for skill_name in sorted(set(policy.get("pin_on") or []) & set(policy.get("pin_off") or [])):
            pin_on_line = _first_location(locations, "pin_on", skill_name)
            pin_off_line = _first_location(locations, "pin_off", skill_name)
            findings.append(
                _override_lint_finding(
                    rule="contradiction",
                    severity=OVERRIDE_LINT_SEVERITY_ERROR,
                    skill=skill_name,
                    explanation=(
                        f"{skill_name!r} appears in both pin_on and pin_off, "
                        "so the override asks for mutually exclusive outcomes."
                    ),
                    suggested_fix=(
                        "Keep exactly one of `pin_on` or `pin_off` for this skill, "
                        "or remove both entries."
                    ),
                    policy_path=policy_path,
                    lines={"pin_on": pin_on_line, "pin_off": pin_off_line},
                )
            )

        for skill_name in sorted(set(policy.get("opt_out_global") or []) & set(DISPATCHER_CORE)):
            findings.append(
                _override_lint_finding(
                    rule="floor_opt_out",
                    severity=OVERRIDE_LINT_SEVERITY_ERROR,
                    skill=skill_name,
                    explanation=(
                        f"{skill_name!r} is dispatcher-core floor policy and cannot "
                        "be opted out by repo-local overrides."
                    ),
                    suggested_fix=(
                        f"Remove {skill_name!r} from `opt_out_global`; dispatcher "
                        "floor skills must remain available."
                    ),
                    policy_path=policy_path,
                    line=_first_location(locations, "opt_out_global", skill_name),
                    code="OVERRIDE_REFUSED_FLOOR",
                )
            )

        if known:
            referenced_sections = ("pin_on", "pin_off", "opt_out_global", "defaults")
            for section in referenced_sections:
                for skill_name in sorted(set(policy.get(section) or [])):
                    if skill_name in known:
                        continue
                    suggestion = _did_you_mean(skill_name, sorted(known))
                    suggested_fix = "Remove the stale override entry or restore the skill source."
                    if suggestion:
                        suggested_fix = f"Did you mean {suggestion!r}? Otherwise {suggested_fix[0].lower()}{suggested_fix[1:]}"
                    findings.append(
                        _override_lint_finding(
                            rule="dangling",
                            severity=OVERRIDE_LINT_SEVERITY_ERROR,
                            skill=skill_name,
                            explanation=(
                                f"{section} references {skill_name!r}, but no declared "
                                "or discoverable skill source by that name was found."
                            ),
                            suggested_fix=suggested_fix,
                            policy_path=policy_path,
                            line=_first_location(locations, section, skill_name),
                            did_you_mean=suggestion,
                        )
                    )

    errors = [
        finding for finding in findings
        if finding.get("severity") == OVERRIDE_LINT_SEVERITY_ERROR
    ]
    warnings = [
        finding for finding in findings
        if finding.get("severity") == OVERRIDE_LINT_SEVERITY_WARN
    ]
    return {
        "ok": not errors,
        "policy_path": policy_path,
        "repo_root": str(policy.get("_repo_root") or ""),
        "exists": Path(policy_path).is_file() if policy_path else False,
        "findings": findings,
        "summary": {
            "total": len(findings),
            "error": len(errors),
            "warn": len(warnings),
        },
    }


def _repo_override_paths(cwd: str | os.PathLike[str] | Path) -> tuple[Path, Path]:
    cwd_path = Path(os.path.expandvars(os.path.expanduser(str(cwd))))
    repo_root = _repo_root_for_skill_install(cwd_path)
    return repo_root, repo_root / SKILL_OVERRIDES_REL


def _override_policy_for_write(policy_path: Path) -> dict[str, Any]:
    if not policy_path.is_file():
        return {
            "version": OVERRIDE_POLICY_VERSION,
            "pin_on": [],
            "pin_off": [],
            "opt_out_global": [],
            "overlays": {"enable": [], "disable": []},
            "defaults": [],
            "reason": "",
        }
    raw = load_yaml(policy_path)
    if not isinstance(raw, dict):
        raise RuntimeError(f"skill override policy must be a mapping: {policy_path}")
    return dict(raw)


def _serialize_override_policy(policy: dict[str, Any]) -> str:
    yaml_mod = require_yaml("write skill override policy")
    payload = dict(policy)
    payload.setdefault("version", OVERRIDE_POLICY_VERSION)
    payload.setdefault("pin_on", [])
    payload.setdefault("pin_off", [])
    payload.setdefault("opt_out_global", [])
    payload.setdefault("overlays", {"enable": [], "disable": []})
    payload.setdefault("defaults", [])
    payload.setdefault("reason", "")
    return yaml_mod.safe_dump(payload, sort_keys=False).rstrip() + "\n"


def _fsync_parent_dir(path: Path) -> None:
    parent_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _atomic_write_override_text(path: Path, content: str, *, fsync: bool) -> None:
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if fsync:
            _fsync_parent_dir(path)
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _wait_for_override_text(path: Path, expected: str) -> None:
    deadline = time.monotonic() + 0.5
    while True:
        try:
            if path.read_text(encoding="utf-8") == expected:
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def update_repo_override_policy(
    cwd: str | os.PathLike[str] | Path,
    mutate_fn: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    fsync: bool = True,
    timeout: float = OVERRIDE_WRITE_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Locked, crash-safe read-modify-write for .skillbox/skill-overrides.yaml."""
    _repo_root, policy_path = _repo_override_paths(cwd)
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = policy_path.with_name(policy_path.name + ".lock")

    def _commit() -> dict[str, Any]:
        current = _override_policy_for_write(policy_path)
        updated = mutate_fn(dict(current))
        if updated is None:
            updated = current
        serialized = _serialize_override_policy(updated)
        previous = policy_path.read_text(encoding="utf-8") if policy_path.is_file() else ""
        changed = previous != serialized
        if changed:
            _atomic_write_override_text(policy_path, serialized, fsync=fsync)
            _wait_for_override_text(policy_path, serialized)
        policy = _repo_override_policy(policy_path.parent)
        policy["changed"] = changed
        policy["lock_path"] = str(lock_path)
        return policy

    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OverrideWriteLockTimeout(lock_path)
                time.sleep(_OVERRIDE_WRITE_POLL_INTERVAL_SECONDS)
        return _commit()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


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


# --------------------------------------------------------------------------- #
# Registry id/category -> path resolution (skill-scope `repos:` / `categories:`)
#
# A scope rule may name repos by their registry id (``repos: [app_core, app_core-server]``)
# and/or by a registry classification (``categories: [backend]`` matching a
# repo's ``bucket``) instead of hand-listing literal ``paths:``. The id->path
# taxonomy is the canonical operator registry at
# ``skillbox-config/registry/repos.yaml`` (the SAME file
# ``scripts/registry_doctor.py`` validates), and the per-machine path is derived
# at eval time from ``machines.yaml`` so a policy edit names a repo ONCE.
#
# Resolution is ADDITIVE: the resolved paths are appended to whatever literal
# ``paths:`` the rule already carries and handed to the existing path-matching
# logic unchanged (``_scope_rule_paths`` -> ``_matching_scope_rule`` /
# ``_path_prefix_matches``). Raw ``paths:`` keep working untouched.
# --------------------------------------------------------------------------- #

# Env override pointing directly at a registry/repos.yaml file (test seam +
# operator escape hatch), mirroring SKILLBOX_MACHINES_FILE.
REGISTRY_FILE_ENV_VAR = "SKILLBOX_REGISTRY_FILE"
# repos.yaml lives in the private config repo beside skill-scope.yaml/machines.yaml.
REGISTRY_FILE_REL = ("registry", "repos.yaml")


class RegistryResolutionError(ValueError):
    """A scope rule named a registry id/category that the registry does not declare.

    The message embeds a fix hint: the nearest declared id (did-you-mean) and the
    full declared id list, so a typo is self-healing rather than a silent miss.
    """


def _registry_doctor_module() -> Any | None:
    """Import skillbox-config ``scripts/registry_doctor.py`` (the canonical loader).

    We reuse registry_doctor's ``load_registry``/``normalize_registry`` so ids are
    validated against the SAME source and parsing logic the registry doctor uses,
    rather than inventing a second validator. Located relative to the runtime root
    exactly like ``machines.py`` finds machines.yaml (sibling-of-runtime-root, then
    the devbox sibling-of-opensource nesting). Best-effort: a missing config repo
    or PyYAML returns ``None`` so resolution degrades to "no registry" rather than
    raising on boxes without the private config checked out.
    """
    override = globals().get("_registry_doctor_module_override")
    if override is not None:
        return override()
    import importlib.util  # noqa: PLC0415

    here = os.path.abspath(__file__)
    runtime_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    candidates = [
        os.path.join(runtime_root, "..", "skillbox-config", "scripts", "registry_doctor.py"),
        os.path.join(runtime_root, "..", "..", "skillbox-config", "scripts", "registry_doctor.py"),
    ]
    for candidate in candidates:
        path = os.path.abspath(candidate)
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("_skillbox_registry_doctor", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception:  # pragma: no cover - defensive: missing PyYAML etc.
            return None
    return None


def _registry_file_path(doctor: Any) -> Path | None:
    """Resolve the repos.yaml path: env override, else registry_doctor's default."""
    override = str(os.environ.get(REGISTRY_FILE_ENV_VAR) or "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    default = getattr(doctor, "DEFAULT_REGISTRY", None)
    if default is not None and Path(default).is_file():
        return Path(default)
    return None


def _load_registry_entries() -> list[dict[str, Any]]:
    """Return the registry's repo entries (id/path/bucket/...), or [].

    Reuses registry_doctor.load_registry to read repos.yaml from the canonical
    location. Each entry keeps its declared (un-expanded) ``path`` so machine
    translation can re-root it; ``id`` and ``bucket`` drive id/category lookup.
    """
    doctor = _registry_doctor_module()
    if doctor is None:
        return []
    registry_path = _registry_file_path(doctor)
    if registry_path is None or not registry_path.is_file():
        return []
    try:
        payload = doctor.load_registry(registry_path)
    except Exception:  # pragma: no cover - defensive parse guard
        return []
    entries: list[dict[str, Any]] = []
    for item in payload.get("repos") or []:
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            entries.append(item)
    return entries


def _did_you_mean(target: str, candidates: list[str]) -> str | None:
    """Nearest candidate id by difflib ratio (>=0.6), for the fix hint."""
    import difflib  # noqa: PLC0415

    matches = difflib.get_close_matches(target, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _machine_detected() -> bool:
    """True when the current machine is positively identified.

    Distinguishes "machine detected (just no repo_roots declared)" from "machine
    undetected" — the two collapse to an empty :func:`_machine_repo_roots` set but
    have OPPOSITE meaning for registry-id resolution. ``_machines_classifier``
    swallows every failure (missing/broken machines.yaml, unmatched hostname, a
    renamed host, a worker container) to ``(None, None)``; an undetected machine
    must NOT silently re-root to the home-form-only set, so callers fail loud.
    """
    config, machine_id = _machines_classifier()
    return config is not None and bool(machine_id)


def _machine_repo_roots() -> list[str]:
    """Current machine's declared repo roots (expanded), longest-first.

    Empty when machines.yaml is missing, the machine is undetected, OR the
    detected machine simply declares no repo_roots. Callers that re-root a
    registry id MUST first gate on :func:`_machine_detected` — an empty set from
    an UNDETECTED machine means "cannot re-root" (a hard error), not "fall back to
    the home-relative form".
    """
    config, machine_id = _machines_classifier()
    if config is None or not machine_id:
        return []
    profile = config.get(machine_id)
    if profile is None:
        return []
    roots = [str(root) for root in profile.repo_roots if str(root).strip()]
    return sorted(roots, key=len, reverse=True)


def _registry_path_remainder(declared_path: str) -> tuple[str, bool]:
    """Split a registry path into (remainder, was_under_repos_root).

    The registry authors paths home-relative (``~/repos/app_core``, ``~/hard/x``).
    Paths under the ``~/repos`` family get re-rooted under the CURRENT machine's
    repo roots (so ``app_core`` -> ``/srv/skillbox/repos/app_core`` on the devbox); paths
    under other roots (``~/hard/...``) carry no machine mapping and are expanded
    home-relative as-is. Returns ``("", False)`` when there is no remainder.
    """
    expanded = _expand_policy_path(declared_path)
    home_repos = _expand_policy_path("~/repos")
    prefix = home_repos.rstrip("/") + "/"
    if expanded == home_repos:
        return "", True
    if expanded.startswith(prefix):
        return expanded[len(prefix):], True
    return expanded, False


def _resolve_registry_path(declared_path: str) -> list[str]:
    """Map one registry repo path to every spelling on the CURRENT machine.

    Emits the repo's path under (a) the registry's home-relative form and (b)
    each of the current machine's repo roots, so the resolved set is a superset
    of the spellings an operator would otherwise hand-list. All spellings collapse
    under ``_expand_policy_path``'s ``Path.resolve()`` (e.g. the ``/srv/repos``
    symlink alias folds into ``/srv/skillbox/repos``), so the EFFECTIVE matched
    set equals the hand-listed literals — proving back-compat equivalence.
    """
    remainder, under_repos = _registry_path_remainder(declared_path)
    spellings: list[str] = []
    # (a) home-relative form (the registry's own spelling).
    spellings.append(declared_path)
    if under_repos:
        # (b) re-root the remainder under each current-machine repo root.
        for root in _machine_repo_roots():
            spellings.append(os.path.join(root, remainder) if remainder else root)
    resolved = sorted({_expand_policy_path(item) for item in spellings if str(item).strip()})
    return resolved


def _resolve_registry_entry_path(entry: dict[str, Any]) -> list[str]:
    """Resolve ONE registry entry's declared path to current-machine spellings.

    Fail-loud guards (match the project's posture — unknown-id already raises):

    * BUG B — a known id whose registry entry has a missing/empty ``path`` is a
      MALFORMED entry, not a silent no-op: it would otherwise resolve to ``[]``
      and make the rule match nothing. Raise :class:`RegistryResolutionError`.

    * BUG A — a path under ``~/repos`` MUST be re-rooted under the current
      machine's repo roots. When the machine is UNDETECTED
      (:func:`_machine_detected` is False — broken/missing machines.yaml, a
      renamed host, a worker container, etc.) we cannot re-root, so re-rooting
      would silently collapse to the home-form-only spelling and the rule would
      match NO real ``/srv/skillbox/repos/<repo>`` cwd. Raise instead of mis-scoping.
    """
    declared_path = str(entry.get("path") or "").strip()
    if not declared_path:
        raise RegistryResolutionError(
            f"registry id {str(entry.get('id') or '')!r} has a missing/empty "
            "`path` in registry/repos.yaml -- malformed entry; cannot resolve "
            "it to a current-machine path."
        )
    _remainder, under_repos = _registry_path_remainder(declared_path)
    if under_repos and not _machine_detected():
        raise RegistryResolutionError(
            "registry-id rule used but current machine undetected -- cannot "
            "re-root; check machines.yaml / SKILLBOX_MACHINE "
            f"(resolving registry id {str(entry.get('id') or '')!r} -> "
            f"{declared_path!r})."
        )
    return _resolve_registry_path(declared_path)


def _scope_rule_repo_ids(raw_rule: dict[str, Any]) -> list[str]:
    return [
        str(item).strip()
        for item in _as_list(raw_rule.get("repos"))
        if str(item).strip()
    ]


def _resolve_scope_rule_repos(
    repo_ids: list[str],
    category_ids: list[str],
    entries: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Resolve registry ids + registry-category ids to current-machine paths.

    ``repos:`` names registry ids directly; ``categories:`` additionally matches a
    repo's registry ``bucket`` (registry-derived category membership). Returns
    ``(paths, registry_category_ids_that_matched)``. An unknown ``repos:`` id is a
    hard :class:`RegistryResolutionError` with a did-you-mean fix hint; a
    ``categories:`` id that matches no registry bucket is NOT an error here (the
    policy ``project_categories`` block is the other resolution path for category
    ids and owns that miss), so this only reports buckets it positively matched.
    """
    if not repo_ids and not category_ids:
        return [], []
    if not entries:
        # Registry unreadable: a literal id can't be validated/resolved. Unknown
        # ids would normally raise; with no registry at all we surface that the
        # ids could not be resolved rather than silently dropping them, but only
        # for explicit ``repos:`` (categories degrade to policy resolution).
        if repo_ids:
            raise RegistryResolutionError(
                "cannot resolve repos: "
                + ", ".join(repr(rid) for rid in repo_ids)
                + " -- registry/repos.yaml not found or unreadable on this machine."
            )
        return [], []

    by_id = {str(entry.get("id")): entry for entry in entries}
    declared_ids = sorted(by_id)
    paths: list[str] = []

    for repo_id in repo_ids:
        entry = by_id.get(repo_id)
        if entry is None:
            hint = _did_you_mean(repo_id, declared_ids)
            suggestion = f"; did you mean {hint!r}?" if hint else ""
            raise RegistryResolutionError(
                f"id {repo_id!r} not in registry/repos.yaml{suggestion} "
                f"declared ids: {', '.join(declared_ids)}"
            )
        paths.extend(_resolve_registry_entry_path(entry))

    matched_categories: list[str] = []
    for category_id in category_ids:
        members = [
            entry for entry in entries
            if str(entry.get("bucket") or "").strip() == category_id
        ]
        if not members:
            continue
        matched_categories.append(category_id)
        for entry in members:
            paths.extend(_resolve_registry_entry_path(entry))

    return sorted(set(paths)), matched_categories


def _scope_rule_patterns(raw_rule: dict[str, Any]) -> list[str]:
    # `skills:`/`patterns:`/`names:` may be authored as a scalar (`skills: foo`)
    # or a list. Coerce via _as_list so a scalar STRING becomes a single-element
    # list instead of being char-iterated (`skills: foo` -> ['f','o','o']). The
    # `or` chain preserves the prior fall-through (an empty/absent key tries the
    # next alias); _as_list only wraps the chosen value.
    raw_patterns = raw_rule.get("skills") or raw_rule.get("patterns") or raw_rule.get("names") or []
    return [str(item).strip() for item in _as_list(raw_patterns) if str(item).strip()]


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
    *,
    registry_entries: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Build a rule's effective path list (literal + category + registry).

    Returns ``(paths, category_ids, unknown_categories, repo_ids)``. Resolution
    order is purely ADDITIVE — literal ``paths:`` first (unchanged), then policy
    ``project_categories`` expansion (unchanged), then registry ``repos:`` and
    registry-bucket ``categories:`` expansion appended. The merged set is deduped
    and sorted, then handed downstream to the EXISTING path matcher untouched.
    An unknown ``repos:`` id raises :class:`RegistryResolutionError`.
    """
    category_ids = _scope_rule_category_ids(raw_rule)
    repo_ids = _scope_rule_repo_ids(raw_rule)
    paths = _scope_rule_direct_paths(raw_rule)
    unknown_categories: list[str] = []
    matched_policy_categories: set[str] = set()
    for category_id in category_ids:
        category = categories.get(category_id)
        if not category:
            continue
        matched_policy_categories.add(category_id)
        paths.extend(str(path) for path in category.get("paths") or [])

    # Registry-derived expansion: `repos:` ids and registry-bucket `categories:`.
    if registry_entries is None:
        registry_entries = _load_registry_entries()
    registry_paths, registry_categories = _resolve_scope_rule_repos(
        repo_ids, category_ids, registry_entries
    )
    paths.extend(registry_paths)

    # A category id is "unknown" only when NEITHER the policy project_categories
    # block NOR the registry bucket taxonomy knows it — so a registry-only
    # category no longer reads as unknown, and a typo still surfaces.
    known_categories = matched_policy_categories | set(registry_categories)
    unknown_categories = [cid for cid in category_ids if cid not in known_categories]

    return sorted(set(paths)), category_ids, unknown_categories, repo_ids


def _scope_rule_from_raw(
    raw_rule: dict[str, Any],
    *,
    index: int,
    policy: dict[str, Any],
    categories: dict[str, dict[str, Any]],
    overlays_on: set[str],
    registry_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    overlay = str(raw_rule.get("overlay") or "").strip()
    if overlay and overlay not in overlays_on:
        return None
    patterns = _scope_rule_patterns(raw_rule)
    if not patterns:
        return None
    paths, category_ids, unknown_categories, repo_ids = _scope_rule_paths(
        raw_rule, categories, registry_entries=registry_entries
    )
    return {
        "id": str(raw_rule.get("id") or f"rule-{index}"),
        "patterns": patterns,
        "paths": paths,
        "categories": category_ids,
        "repos": repo_ids,
        "unknown_categories": unknown_categories,
        "allow_global": bool(raw_rule.get("allow_global", False)),
        "default": raw_rule.get("default", "on"),
        "activation": str(raw_rule.get("activation") or "").strip(),
        "notes": str(raw_rule.get("notes") or raw_rule.get("reason") or ""),
        "overlay": overlay,
        "policy_path": str(policy.get("_policy_path") or ""),
    }


# Per-pass collector for non-fatal scope-rule resolution errors (BUG C). A
# single typo'd `repos:` id must NOT nuke the WHOLE report: `_scope_rules`
# SKIPS the bad rule (so the rest still resolve) and records its error here so a
# doctor lint can surface the typo. Reset at the start of every `_scope_rules`
# pass; read via `last_scope_rule_errors()`.
_LAST_SCOPE_RULE_ERRORS: list[dict[str, Any]] = []


def last_scope_rule_errors() -> list[dict[str, Any]]:
    """Non-fatal scope-rule resolution errors from the most recent ``_scope_rules`` pass.

    Each entry is ``{"policy_path", "rule_id", "index", "error", "type"}``. A
    doctor lint reads this to report a typo'd ``repos:`` id (or a malformed
    registry entry) WITHOUT the bad rule taking down the rest of the report.
    Returns a copy so callers cannot mutate the collector.
    """
    return [dict(item) for item in _LAST_SCOPE_RULE_ERRORS]


def _scope_rules(model: dict[str, Any]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    overlays_on = active_overlays()
    # Load the registry id->path taxonomy once per pass (not per rule) so
    # `repos:`/registry-`categories:` resolution stays cheap.
    registry_entries = _load_registry_entries()
    for policy in _operator_scope_policies(model):
        categories = _policy_categories_by_id(policy)
        for index, raw_rule in enumerate(policy.get("rules") or []):
            if not isinstance(raw_rule, dict):
                continue
            try:
                rule = _scope_rule_from_raw(
                    raw_rule,
                    index=index,
                    policy=policy,
                    categories=categories,
                    overlays_on=overlays_on,
                    registry_entries=registry_entries,
                )
            except RegistryResolutionError as exc:
                # BUG C: one bad rule (typo'd repos: id / malformed registry
                # entry / undetected-machine re-root) must not take down every
                # caller (skills report, skill sync, scope-violations,
                # missing-for-cwd). SKIP it, keep resolving the rest, and record
                # the error so a doctor lint can surface the typo.
                errors.append(
                    {
                        "policy_path": str(policy.get("_policy_path") or ""),
                        "rule_id": str(raw_rule.get("id") or f"rule-{index}"),
                        "index": index,
                        "error": str(exc),
                        "type": type(exc).__name__,
                    }
                )
                continue
            if rule is not None:
                rules.append(rule)
    # Publish this pass's collected errors (replace, not append).
    _LAST_SCOPE_RULE_ERRORS[:] = errors
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
            # Dedup by the alias-canonicalized path so a scan-root list that
            # names both ``/srv/repos`` and ``/srv/skillbox/repos`` (the same
            # tree, two names) yields ONE root and the fleet walk runs once.
            key = _canonicalize_repo_path(str(root))
            if key not in seen:
                seen.add(key)
                roots.append(root)
    return roots


def _global_allow_patterns(model: dict[str, Any]) -> list[str] | None:
    patterns: list[str] = []
    has_explicit_policy = False
    for policy in _operator_scope_policies(model):
        if policy.get("global_allowlist") is not None:
            has_explicit_policy = True
        for rule in policy.get("rules") or []:
            if not isinstance(rule, dict) or not bool(rule.get("allow_global", False)):
                continue
            has_explicit_policy = True
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


def _scope_allows_global(model: dict[str, Any], skill_name: str) -> bool:
    rule = _matching_scope_rule(skill_name, _scope_rules(model))
    return bool(rule and rule.get("allow_global"))


def _global_install_allowed(model: dict[str, Any], skill_name: str) -> bool:
    patterns = _global_allow_patterns(model)
    if patterns is None:
        return True
    return any(fnmatch.fnmatchcase(skill_name, pattern) for pattern in patterns)
