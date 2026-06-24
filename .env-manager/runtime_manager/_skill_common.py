"""Shared leaf helpers for the skill-visibility package.

Single responsibility: low-level, dependency-free primitives (path matching,
machine/registry-path canonicalization, layer-rank constants, source-bucket
ordering, and small formatting helpers) that every other skill-visibility module
imports. Has NO intra-package dependencies, so it sits at the bottom of the
import layering and breaks would-be cycles.
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

__all__ = [
    'DISPATCHER_CORE',
    'DEFAULT_LAYER_RANK',
    'CLIENT_LAYER_RANK',
    'GLOBAL_LAYER_RANK',
    'PROJECT_LAYER_RANK',
    '_as_list',
    '_expand_policy_path',
    '_path_under_or_equal',
    '_path_prefix_matches',
    '_source_bucket',
    '_path_is_under',
    '_installed_skill_name',
    '_path_exists',
    '_realpath',
    '_machines_classifier',
    '_canonicalize_repo_path',
    '_join_or_none',
    '_truncate_names',
]


DISPATCHER_CORE = ("smart", "sbp")
DEFAULT_LAYER_RANK = 10
CLIENT_LAYER_RANK = 20
GLOBAL_LAYER_RANK = 30
PROJECT_LAYER_RANK = 40


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


def _source_bucket(path: str) -> str:
    raw = path or ""
    expanded = os.path.realpath(os.path.expandvars(os.path.expanduser(raw)))
    home = str(Path.home())
    buckets = [
        (f"{home}/repos/opensource/skills", "opensource/skills"),
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


def _path_is_under(path: str, roots: list[str]) -> bool:
    if not path:
        return False
    candidate = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(path))))
    for raw_root in roots:
        root = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(raw_root))))
        if _path_under_or_equal(candidate, root):
            return True
    return False


def _installed_skill_name(path: Path) -> str:
    if path.suffix == ".skill":
        return path.stem
    return path.name


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _realpath(path: Path | str) -> str:
    return os.path.realpath(os.path.expandvars(os.path.expanduser(str(path))))


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


def _canonicalize_repo_path(path: str) -> str:
    """Collapse declared symlink/bind aliases to the canonical tree.

    ``/srv/repos`` is a symlink alias of ``/srv/skillbox/repos`` on the devbox,
    so the same repo can be named two ways. ``machines.canonicalize_alias`` folds
    the alias form into the canonical form by string prefix -- machine-agnostic,
    so it works even when the alias symlink is not resolvable on the current box
    (e.g. a ``/srv/repos/...`` path evaluated where the link is absent, or a
    foreign Mac path). This is strictly stronger than ``Path.resolve()`` for
    dedup, because ``resolve()`` only collapses links that exist on this host and
    silently leaves un-resolvable aliases as a distinct path -> a double-counted
    repo. Non-alias paths pass through unchanged. Falls back to the input on any
    machines.yaml failure so the audit still runs on profile-less boxes.
    """
    raw = str(path or "")
    if not raw:
        return raw
    config, _machine_id = _machines_classifier()
    if config is None:
        return raw
    try:
        return config.canonicalize_alias(raw)
    except Exception:  # pragma: no cover - defensive: never break the audit
        return raw


def _join_or_none(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values if str(value)) or "(none)"


def _truncate_names(names: list[str], limit: int) -> str:
    visible = names[: max(0, limit)]
    text = ", ".join(visible) if visible else "-"
    remaining = len(names) - len(visible)
    if remaining > 0:
        text += f" (+{remaining})"
    return text
