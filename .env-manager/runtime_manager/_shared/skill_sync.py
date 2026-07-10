from __future__ import annotations

# Generated mechanically from runtime_manager/shared.py; keep logic changes out of this split.
# ruff: noqa: F401
import argparse as argparse
import copy
import datetime
import fcntl
import hashlib
import json
import os
import re
import selectors as selectors
import shlex
import signal as signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

PACKAGE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PACKAGE_DIR.parent
DEFAULT_ROOT_DIR = SCRIPT_DIR.parent.resolve()
SCRIPTS_DIR = DEFAULT_ROOT_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from runtime_manager.errors import DEPRECATION_MARKER  # noqa: E402
except ImportError:  # loaded standalone without a package
    if str(PACKAGE_DIR) not in sys.path:
        sys.path.insert(0, str(PACKAGE_DIR))
    from errors import DEPRECATION_MARKER  # type: ignore[no-redef]  # noqa: E402

from lib.runtime_model import (  # noqa: E402
    LOOPBACK_BIND_HOSTS as LOOPBACK_BIND_HOSTS,
    PERSISTENCE_ERROR_CODES,
    PersistenceContractError,
    RUNTIME_ID_INVALID,
    RUNTIME_ID_PATTERN,
    RUNTIME_ID_PATTERN_TEXT,
    RuntimeIdValidationError as RuntimeIdValidationError,
    WILDCARD_BIND_HOSTS as WILDCARD_BIND_HOSTS,
    build_runtime_model,
    classify_bind_scope as classify_bind_scope,
    client_config_host_dir,
    client_config_runtime_dir,
    client_configs_host_root,
    compile_persistence_summary,
    extract_command_port as extract_command_port,
    extract_host_port as extract_host_port,
    host_path_to_absolute_path,
    load_yaml,
    load_runtime_env,
    runtime_manifest_path,
    runtime_path_to_host_path as runtime_path_to_host_path,
    storage_binding_by_id,
    validate_runtime_id as validate_runtime_id,
)
from lib.redaction import REDACTION_MARKER as REDACTION_MARKER  # noqa: E402
from lib.redaction import SECRET_KEY_PATTERN as SECRET_KEY_PATTERN  # noqa: E402
from lib.redaction import is_secret_key as is_secret_key  # noqa: E402
from lib.redaction import redact_text as redact_text  # noqa: E402
from lib.redaction import redact_value as redact_value  # noqa: E402
from .digest import (
    directory_tree_sha256,
    file_sha256,
)

from .proc import (
    run_command,
)

from .fs import (
    atomic_replace_tree,
    ensure_directory,
    write_json_file,
)

VALID_SKILL_SYNC_MODES = {"clone-and-install"}

SKILL_REPOS_LOCKFILE_VERSION = 2

SKILL_REPOS_CONFIG_VERSION = 2

SHARED_SKILL_ASSET_DIR = "_shared"

DEFAULT_SKILLIGNORE_PATTERNS = [
    ".git/",
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
    "modes/",
    "briefs/",
]

CLONE_DIR_ROOT_REL = Path("workspace") / "skill-repos"

def load_skill_repos_config(config_path: Path) -> dict[str, Any]:
    """Load and validate a skill_repos YAML config file."""
    from runtime_manager.shared_distribution import ConfigError, validate_distribution_config

    if not config_path.is_file():
        raise RuntimeError(f"SKILL_CONFIG_INVALID: config file missing at {config_path}")
    raw = load_yaml(config_path)
    if not isinstance(raw, dict):
        raise RuntimeError(f"SKILL_CONFIG_INVALID: expected a YAML mapping in {config_path}")
    version = raw.get("version")
    if version != SKILL_REPOS_CONFIG_VERSION:
        raise RuntimeError(
            f"SKILL_CONFIG_INVALID: expected version {SKILL_REPOS_CONFIG_VERSION}, got {version!r} in {config_path}"
        )
    entries = raw.get("skill_repos")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise RuntimeError(f"SKILL_CONFIG_INVALID: skill_repos must be a list in {config_path}")

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"SKILL_CONFIG_INVALID: skill_repos[{i}] must be a mapping")
        if entry.get("distributor"):
            continue
        has_repo = bool(entry.get("repo"))
        has_path = bool(entry.get("path"))
        if has_repo == has_path:
            raise RuntimeError(
                f"SKILL_CONFIG_INVALID: skill_repos[{i}] must have exactly one of 'repo' or 'path'"
            )
        if has_repo and not entry.get("ref"):
            raise RuntimeError(f"SKILL_CONFIG_INVALID: skill_repos[{i}] repo entry requires a 'ref'")
        pick = entry.get("pick")
        if pick is not None and not isinstance(pick, list):
            raise RuntimeError(f"SKILL_CONFIG_INVALID: skill_repos[{i}] pick must be a list")

    try:
        validate_distribution_config(raw, config_path)
    except ConfigError as exc:
        raise RuntimeError(f"SKILL_CONFIG_INVALID: {exc}") from exc

    return raw

def clone_dir_name(repo: str) -> str:
    """Convert 'owner/repo' to 'owner-repo' for clone directory naming."""
    return repo.replace("/", "-")

def _load_skillignore(skill_dir: Path) -> list[str]:
    """Load .skillignore patterns from a skill directory, falling back to defaults."""
    patterns = list(DEFAULT_SKILLIGNORE_PATTERNS)
    ignore_file = skill_dir / ".skillignore"
    if ignore_file.is_file():
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in patterns:
                patterns.append(line)
    return patterns

def _matches_skillignore(rel_path: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any skillignore pattern."""
    import fnmatch

    parts = rel_path.split("/")
    for pattern in patterns:
        if pattern.endswith("/"):
            dir_pattern = pattern.rstrip("/")
            for part in parts[:-1]:
                if fnmatch.fnmatch(part, dir_pattern):
                    return True
            if fnmatch.fnmatch(parts[-1], dir_pattern) and len(parts) > 0:
                pass
        else:
            if fnmatch.fnmatch(parts[-1], pattern):
                return True
            if fnmatch.fnmatch(rel_path, pattern):
                return True
    return False

def filtered_copy_skill(source_dir: Path, target_dir: Path) -> str:
    """Copy a skill directory to target, respecting .skillignore. Returns tree SHA."""
    resolved_source = source_dir.resolve()
    if target_dir.is_symlink():
        resolved_target = target_dir.parent.resolve() / target_dir.name
    else:
        resolved_target = target_dir.resolve()
    try:
        resolved_source.relative_to(resolved_target)
        overlaps = True
    except ValueError:
        overlaps = False
    try:
        resolved_target.relative_to(resolved_source)
        overlaps = True
    except ValueError:
        pass
    if overlaps:
        raise RuntimeError(
            "Refusing to install skill with overlapping source and target paths: "
            f"{source_dir} -> {target_dir}"
        )

    patterns = _load_skillignore(source_dir)

    def _build(stage_dir: Path) -> None:
        for source_file in sorted(source_dir.rglob("*")):
            rel = source_file.relative_to(source_dir).as_posix()

            if _matches_skillignore(rel, patterns):
                continue

            if rel == ".skillignore":
                continue

            if source_file.is_symlink():
                raise RuntimeError(
                    "Refusing to install skill with symlinked file: "
                    f"{source_file.relative_to(source_dir)}"
                )

            if not source_file.is_file():
                continue

            dest = stage_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source_file), str(dest))

    atomic_replace_tree(target_dir, _build, root_mode=0o755)

    tree_sha = directory_tree_sha256(target_dir)
    if tree_sha is None:
        raise RuntimeError(f"Failed to hash installed skill directory {target_dir}")
    return tree_sha

def _resolve_skill_dirs(
    entry: dict[str, Any],
    source_root: Path,
    repo_name: str,
) -> list[tuple[str, Path]]:
    """Resolve skill name -> source directory pairs from a config entry.

    Returns list of (skill_name, skill_source_dir) tuples.
    """
    pick = entry.get("pick")
    if pick:
        results = []
        for skill_name in pick:
            skill_dir = source_root / skill_name
            if not (skill_dir / "SKILL.md").is_file():
                raise RuntimeError(
                    f"SKILL_NOT_FOUND_IN_REPO: skill '{skill_name}' not found in {source_root} "
                    f"(no SKILL.md at {skill_dir})"
                )
            results.append((skill_name, skill_dir))
        return results

    if (source_root / "SKILL.md").is_file():
        return [(repo_name, source_root)]

    raise RuntimeError(
        f"SKILL_CONFIG_INVALID: repo {entry.get('repo', source_root)} has no pick list "
        "and no SKILL.md at root. Add a pick list or ensure SKILL.md exists at the repo root."
    )

def _checkout_skill_repo_ref(repo: str, ref: str, clone_path: Path) -> None:
    checkout_result = run_command(["git", "checkout", ref], cwd=clone_path)
    if checkout_result.returncode != 0:
        checkout_result = run_command(["git", "checkout", f"origin/{ref}"], cwd=clone_path)
    if checkout_result.returncode != 0:
        raise RuntimeError(
            f"SKILL_REPO_CLONE_FAILED: git checkout failed for {repo}@{ref}: "
            f"{checkout_result.stderr.strip() or checkout_result.stdout.strip()}"
        )

def _current_skill_repo_commit(clone_path: Path) -> str | None:
    rev_result = run_command(["git", "rev-parse", "HEAD"], cwd=clone_path)
    return rev_result.stdout.strip() if rev_result.returncode == 0 else None

def _pull_skill_repo_branch(repo: str, ref: str, clone_path: Path) -> None:
    branch_result = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone_path)
    checked_out_branch = (
        branch_result.returncode == 0
        and branch_result.stdout.strip()
        and branch_result.stdout.strip() != "HEAD"
    )
    if not checked_out_branch:
        return
    pull_result = run_command(["git", "pull", "--ff-only"], cwd=clone_path)
    if pull_result.returncode != 0:
        raise RuntimeError(
            f"SKILL_REPO_CLONE_FAILED: git pull --ff-only failed for {repo}@{ref}: "
            f"{pull_result.stderr.strip() or pull_result.stdout.strip()}"
        )

def _fetch_existing_skill_repo(repo: str, ref: str, clone_path: Path) -> tuple[str, Path, str | None]:
    status_result = run_command(["git", "status", "--porcelain"], cwd=clone_path)
    if status_result.returncode == 0 and status_result.stdout.strip():
        return ("SKILL_REPO_DIRTY", clone_path, None)

    fetch_result = run_command(["git", "fetch", "origin"], cwd=clone_path)
    if fetch_result.returncode != 0:
        raise RuntimeError(
            f"SKILL_REPO_CLONE_FAILED: git fetch failed for {repo}: "
            f"{fetch_result.stderr.strip()}"
        )

    _checkout_skill_repo_ref(repo, ref, clone_path)
    _pull_skill_repo_branch(repo, ref, clone_path)
    return ("fetched", clone_path, _current_skill_repo_commit(clone_path))

def _clone_new_skill_repo(repo: str, ref: str, clone_root: Path, clone_path: Path) -> tuple[str, Path, str | None]:
    clone_root.mkdir(parents=True, exist_ok=True)
    clone_url = f"https://github.com/{repo}.git"
    clone_result = run_command(["git", "clone", clone_url, str(clone_path)])
    if clone_result.returncode != 0:
        ssh_url = f"git@github.com:{repo}.git"
        clone_result = run_command(["git", "clone", ssh_url, str(clone_path)])
        if clone_result.returncode != 0:
            raise RuntimeError(
                f"SKILL_REPO_UNREACHABLE: failed to clone {repo}: "
                f"{clone_result.stderr.strip()}"
            )

    _checkout_skill_repo_ref(repo, ref, clone_path)
    return ("cloned", clone_path, _current_skill_repo_commit(clone_path))

def _clone_or_fetch_repo(
    repo: str,
    ref: str,
    clone_root: Path,
    *,
    dry_run: bool,
) -> tuple[str, Path, str | None]:
    """Clone or fetch a repo. Returns (action, clone_path, resolved_commit_or_None)."""
    clone_path = clone_root / clone_dir_name(repo)
    if clone_path.is_dir():
        if dry_run:
            return ("fetched", clone_path, None)
        return _fetch_existing_skill_repo(repo, ref, clone_path)
    if dry_run:
        return ("cloned", clone_path, None)
    return _clone_new_skill_repo(repo, ref, clone_root, clone_path)

def _resolve_skill_repo_entry_source(
    entry: dict[str, Any],
    config_path: Path,
    clone_root: Path,
    skillset: dict[str, Any],
    dry_run: bool,
    actions: list[str],
) -> tuple[Path, str, str | None, str | None] | None:
    """Resolve an entry to (source_root, repo_name, repo, commit) or None to skip."""
    if entry.get("repo"):
        repo = entry["repo"]
        ref = entry["ref"]
        action, clone_path, commit = _clone_or_fetch_repo(repo, ref, clone_root, dry_run=dry_run)
        actions.append(f"skill-repo-{action}: {repo}")
        if action == "SKILL_REPO_DIRTY":
            actions.append(f"SKILL_REPO_DIRTY: {repo} — skipping (uncommitted changes)")
            return None
        repo_name = repo.split("/")[-1] if "/" in repo else repo
        if not clone_path.is_dir():
            pick = entry.get("pick") or [repo_name]
            for skill_name in pick:
                for target in skillset.get("install_targets") or []:
                    target_root = Path(str(target["host_path"]))
                    actions.append(f"install-skill: {skill_name} -> {target_root / skill_name}")
            return None
        return clone_path, repo_name, repo, commit

    local_path = entry["path"]
    source_root = (
        Path(local_path) if Path(local_path).is_absolute()
        else (config_path.parent / local_path).resolve()
    )
    if not source_root.is_dir():
        if dry_run:
            actions.append(f"skip-local-path: {source_root} (not found)")
            return None
        raise RuntimeError(f"SKILL_CONFIG_INVALID: local path does not exist: {source_root}")
    return source_root, source_root.name, None, None

def _install_skill_to_targets(
    skillset: dict[str, Any],
    skill_name: str,
    skill_source: Path,
    dry_run: bool,
    actions: list[str],
    host_home_root: str | None = None,
) -> dict[str, str]:
    """Filtered-copy a skill into every install target. Returns target_id -> tree_sha."""
    install_tree_shas: dict[str, str] = {}
    for target in skillset.get("install_targets") or []:
        target_root = Path(str(target["host_path"]))
        install_dir = target_root / skill_name
        if dry_run:
            actions.append(f"install-skill: {skill_name} -> {install_dir}")
            _mirror_installed_skill_to_host_home(skill_name, target_root, dry_run, actions, host_home_root)
            continue
        tree_sha = filtered_copy_skill(skill_source, install_dir)
        install_tree_shas[target["id"]] = tree_sha
        actions.append(f"install-skill: {skill_name} -> {install_dir}")
        _mirror_installed_skill_to_host_home(skill_name, target_root, dry_run, actions, host_home_root)
    return install_tree_shas

def _shared_skill_asset_source(source_root: Path) -> Path | None:
    shared_source = source_root / SHARED_SKILL_ASSET_DIR
    return shared_source if shared_source.is_dir() else None

def _install_shared_skill_asset_to_targets(
    skillset: dict[str, Any],
    shared_source: Path,
    dry_run: bool,
    actions: list[str],
    host_home_root: str | None = None,
) -> None:
    """Copy sibling shared skill assets next to installed skills.

    The shared asset directory is not itself a skill, so it is intentionally
    excluded from the skill lockfile. It must still live beside installed
    skills so helper scripts referenced by skill SKILL.md files can resolve it.
    """
    for target in skillset.get("install_targets") or []:
        target_root = Path(str(target["host_path"]))
        install_dir = target_root / SHARED_SKILL_ASSET_DIR
        if dry_run:
            actions.append(f"install-shared-skill-asset: {SHARED_SKILL_ASSET_DIR} -> {install_dir}")
            _mirror_installed_skill_to_host_home(
                SHARED_SKILL_ASSET_DIR,
                target_root,
                dry_run,
                actions,
                host_home_root,
            )
            continue
        filtered_copy_skill(shared_source, install_dir)
        actions.append(f"install-shared-skill-asset: {SHARED_SKILL_ASSET_DIR} -> {install_dir}")
        _mirror_installed_skill_to_host_home(
            SHARED_SKILL_ASSET_DIR,
            target_root,
            dry_run,
            actions,
            host_home_root,
        )

def _host_skill_mirror_root(target_root: Path, host_home_root: str | None = None) -> Path | None:
    host_home = (host_home_root if host_home_root is not None else os.environ.get("SKILLBOX_HOST_HOME_ROOT", "")).strip()
    if not host_home:
        return None
    if target_root.name != "skills" or target_root.parent.name not in {".claude", ".codex"}:
        return None
    host_root = Path(host_home).expanduser() / target_root.parent.name / "skills"
    if host_root.resolve(strict=False) == target_root.resolve(strict=False):
        return None
    return host_root

def _mirror_installed_skill_to_host_home(
    skill_name: str,
    target_root: Path,
    dry_run: bool,
    actions: list[str],
    host_home_root: str | None = None,
) -> None:
    mirror_root = _host_skill_mirror_root(target_root, host_home_root)
    if mirror_root is None:
        return
    source_dir = target_root / skill_name
    mirror_dir = mirror_root / skill_name
    if dry_run:
        actions.append(f"mirror-host-skill: {skill_name} -> {mirror_dir}")
        return
    ensure_directory(mirror_root, dry_run=False)
    if mirror_dir.is_symlink():
        if os.readlink(mirror_dir) == str(source_dir):
            actions.append(f"mirror-host-skill-unchanged: {skill_name} -> {mirror_dir}")
            return
        mirror_dir.unlink()
    elif mirror_dir.exists():
        actions.append(f"mirror-host-skill-skip: {skill_name} -> {mirror_dir} (exists)")
        return
    mirror_dir.symlink_to(source_dir, target_is_directory=True)
    actions.append(f"mirror-host-skill: {skill_name} -> {mirror_dir}")

def _symlink_points_inside(path: Path, root: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        raw_target = Path(os.readlink(path))
    except OSError:
        return False
    target = raw_target if raw_target.is_absolute() else path.parent / raw_target
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False

def _reconcile_host_skill_mirrors(
    wanted_by_target_root: dict[Path, set[str]],
    stale_by_target_root: dict[Path, set[str]],
    dry_run: bool,
    actions: list[str],
    host_home_root: str | None = None,
) -> None:
    for target_root, stale_names in sorted(stale_by_target_root.items(), key=lambda item: str(item[0])):
        mirror_root = _host_skill_mirror_root(target_root, host_home_root)
        if mirror_root is None or not mirror_root.exists():
            continue
        wanted_names = wanted_by_target_root.get(target_root, set())
        for entry in sorted(mirror_root.iterdir(), key=lambda path: path.name):
            if entry.name.startswith(".") or entry.name not in stale_names or entry.name in wanted_names:
                continue
            if not _symlink_points_inside(entry, target_root):
                continue
            if dry_run:
                actions.append(f"mirror-host-skill-would-remove: {entry.name} -> {entry}")
                continue
            entry.unlink()
            actions.append(f"mirror-host-skill-remove: {entry.name} -> {entry}")

def _skill_repo_lock_skill_names(lock_path: Path) -> set[str]:
    if not lock_path.is_file():
        return set()
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return {
        str(skill.get("name"))
        for skill in payload.get("skills") or []
        if str(skill.get("name") or "").strip()
    }

def _build_lock_skill_entry(
    skill_name: str,
    entry: dict[str, Any],
    repo: str | None,
    commit: str | None,
    install_tree_shas: dict[str, str],
    dry_run: bool,
) -> dict[str, Any]:
    lock_entry: dict[str, Any] = {
        "name": skill_name,
        "declared_ref": entry.get("ref"),
        "resolved_commit": commit,
    }
    if repo:
        lock_entry["repo"] = repo
    else:
        lock_entry["source_path"] = str(entry.get("path", ""))
    if not dry_run and install_tree_shas:
        first_target = next(iter(install_tree_shas))
        lock_entry["install_tree_sha"] = install_tree_shas[first_target]
    return lock_entry

def _persist_skill_repo_lockfile(
    lock_path: Path,
    config_path: Path,
    lock_skills: list[dict[str, Any]],
    actions: list[str],
) -> None:
    """Write the lockfile, preserving synced_at when semantic content is unchanged."""
    new_config_sha = file_sha256(config_path)
    synced_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if lock_path.is_file():
        try:
            existing_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            existing_skills = {
                (s.get("name"), s.get("resolved_commit"), s.get("install_tree_sha"))
                for s in existing_lock.get("skills") or []
            }
            new_skills = {
                (s.get("name"), s.get("resolved_commit"), s.get("install_tree_sha"))
                for s in lock_skills
            }
            if existing_lock.get("config_sha") == new_config_sha and existing_skills == new_skills:
                synced_at = existing_lock.get("synced_at", synced_at)
        except (json.JSONDecodeError, KeyError):
            pass
    lock_payload = {
        "version": SKILL_REPOS_LOCKFILE_VERSION,
        "config_sha": new_config_sha,
        "synced_at": synced_at,
        "skills": lock_skills,
    }
    changed = write_json_file(lock_path, lock_payload)
    actions.append(f"{'write-lockfile' if changed else 'lockfile-unchanged'}: {lock_path}")

def sync_skill_repo_sets(model: dict[str, Any], dry_run: bool) -> list[str]:
    """Sync skill-repo-set skill sets: clone repos, filtered-copy skills, write lock."""
    actions: list[str] = []
    mirror_wanted_by_target_root: dict[Path, set[str]] = {}
    mirror_stale_by_target_root: dict[Path, set[str]] = {}
    host_home_root = str((model.get("env") or {}).get("SKILLBOX_HOST_HOME_ROOT") or "").strip() or None

    for skillset in model["skills"]:
        if skillset.get("kind") != "skill-repo-set":
            continue
        if (skillset.get("sync") or {}).get("mode", "") != "clone-and-install":
            continue

        config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
        lock_path = Path(str(skillset.get("lock_path_host_path", "")))
        clone_root = Path(str(skillset.get("clone_root_host_path", "")))
        previous_lock_names = _skill_repo_lock_skill_names(lock_path)

        config = load_skill_repos_config(config_path)
        for target in skillset.get("install_targets") or []:
            ensure_directory(Path(str(target["host_path"])), dry_run)

        lock_skills: list[dict[str, Any]] = []
        installed_shared_asset_tree_sha: str | None = None
        installed_shared_asset_source: Path | None = None
        for entry in config.get("skill_repos") or []:
            if entry.get("distributor"):
                continue
            resolved = _resolve_skill_repo_entry_source(
                entry, config_path, clone_root, skillset, dry_run, actions,
            )
            if resolved is None:
                continue
            source_root, repo_name, repo, commit = resolved
            skill_dirs = _resolve_skill_dirs(entry, source_root, repo_name)
            shared_source = _shared_skill_asset_source(source_root)
            if shared_source is not None:
                shared_tree_sha = directory_tree_sha256(shared_source)
                if shared_tree_sha is None:
                    raise RuntimeError(f"SKILL_SHARED_ASSET_INVALID: failed to hash {shared_source}")
                if installed_shared_asset_tree_sha is not None:
                    if shared_tree_sha != installed_shared_asset_tree_sha:
                        raise RuntimeError(
                            "SKILL_SHARED_ASSET_CONFLICT: "
                            f"{shared_source} differs from {installed_shared_asset_source}"
                        )
                else:
                    installed_shared_asset_tree_sha = shared_tree_sha
                    installed_shared_asset_source = shared_source
                    for target in skillset.get("install_targets") or []:
                        target_root = Path(str(target["host_path"]))
                        if _host_skill_mirror_root(target_root, host_home_root) is not None:
                            mirror_wanted_by_target_root.setdefault(target_root, set()).add(
                                SHARED_SKILL_ASSET_DIR
                            )
                    _install_shared_skill_asset_to_targets(
                        skillset,
                        shared_source,
                        dry_run,
                        actions,
                        host_home_root,
                    )
            for skill_name, skill_source in skill_dirs:
                for target in skillset.get("install_targets") or []:
                    target_root = Path(str(target["host_path"]))
                    if _host_skill_mirror_root(target_root, host_home_root) is not None:
                        mirror_wanted_by_target_root.setdefault(target_root, set()).add(skill_name)
                install_tree_shas = _install_skill_to_targets(
                    skillset, skill_name, skill_source, dry_run, actions, host_home_root,
                )
                lock_skills.append(_build_lock_skill_entry(
                    skill_name, entry, repo, commit, install_tree_shas, dry_run,
                ))

        current_lock_names = {
            str(skill.get("name"))
            for skill in lock_skills
            if str(skill.get("name") or "").strip()
        }
        stale_lock_names = previous_lock_names - current_lock_names
        if stale_lock_names:
            for target in skillset.get("install_targets") or []:
                target_root = Path(str(target["host_path"]))
                if _host_skill_mirror_root(target_root, host_home_root) is not None:
                    mirror_stale_by_target_root.setdefault(target_root, set()).update(stale_lock_names)

        if dry_run:
            actions.append(f"write-lockfile: {lock_path}")
            continue
        _persist_skill_repo_lockfile(lock_path, config_path, lock_skills, actions)

    _reconcile_host_skill_mirrors(
        mirror_wanted_by_target_root,
        mirror_stale_by_target_root,
        dry_run,
        actions,
        host_home_root,
    )

    return actions
