"""Shared additive test fixtures.

Keep helpers here dependency-free and additive: move duplicated fixture setup
into this module without changing the assertions or behavioral intent of the
tests that adopt it.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


PRESSURE_HEADING = "## Pressure And Offload Policy"
PRESSURE_PLACEHOLDER = "<PRESSURE-ADVISORY-NORMALIZED>"
ROOT_PLACEHOLDER = "<ROOT>"
INSTALLED_SKILLS_PLACEHOLDER = "<INSTALLED-SKILLS-NORMALIZED>"
SYNC_VERB_PLACEHOLDER = "<SYNC-VERB-NORMALIZED>:"


def _deep_merge(base: dict[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def make_runtime_model(**overrides: Any) -> dict[str, Any]:
    """Return a minimal runtime model dict, with deep-merged overrides."""
    model: dict[str, Any] = {
        "root_dir": "/tmp/skillbox-fixture",
        "manifest_file": "/tmp/skillbox-fixture/workspace/runtime.yaml",
        "env": {
            "SKILLBOX_WORKSPACE_ROOT": "/workspace",
            "SKILLBOX_REPOS_ROOT": "/workspace/repos",
            "SKILLBOX_SKILLS_ROOT": "/workspace/skills",
            "SKILLBOX_LOG_ROOT": "/workspace/logs",
            "SKILLBOX_HOME_ROOT": "/home/sandbox",
        },
        "selection": {"default_client": "personal"},
        "active_profiles": ["core"],
        "active_clients": ["personal"],
        "clients": [{"id": "personal", "label": "Personal"}],
        "profiles": [{"id": "core", "label": "Core"}],
        "repos": [{"id": "app", "kind": "repo", "host_path": "/repo/app", "profiles": ["core"]}],
        "artifacts": [
            {
                "id": "bundle",
                "path": "/tmp/bundle.tgz",
                "host_path": "/tmp/bundle.tgz",
                "profiles": ["core"],
            }
        ],
        "env_files": [],
        "skills": [{"id": "domain-planner", "profiles": ["core"]}],
        "skill_repos": [{"id": "skills", "path": "/repo/skills", "profiles": ["core"]}],
        "tasks": [
            {"id": "prepare", "repo": "app", "profiles": ["core"]},
            {"id": "build-api", "depends_on": ["prepare"], "repo": "app", "profiles": ["core"]},
        ],
        "services": [
            {"id": "db", "kind": "service", "profiles": ["core"]},
            {
                "id": "api",
                "kind": "service",
                "depends_on": ["db"],
                "bootstrap_tasks": ["build-api"],
                "repo": "app",
                "artifact": "bundle",
                "profiles": ["core"],
            },
            {"id": "memory-mcp", "kind": "mcp", "mcp_server": "memory", "profiles": ["core"]},
        ],
        "logs": [],
        "checks": [{"id": "runtime-doctor", "type": "command", "repo": "app", "profiles": ["core"]}],
        "bridges": [],
        "service_mode_commands": [],
        "ingress_routes": [],
        "parity_ledger": [],
    }
    return _deep_merge(model, overrides)


def _safe_relative_path(raw: str | os.PathLike[str]) -> Path:
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"workspace fixture paths must be relative: {raw!r}")
    return rel


def _materialize_tree(root: Path, structure: Mapping[str, Any]) -> None:
    for raw_name, value in structure.items():
        target = root / _safe_relative_path(raw_name)
        if isinstance(value, Mapping):
            target.mkdir(parents=True, exist_ok=True)
            _materialize_tree(target, value)
        elif value is None:
            target.mkdir(parents=True, exist_ok=True)
        elif isinstance(value, bytes):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(value), encoding="utf-8")


@contextmanager
def make_temp_workspace(structure: Mapping[str, Any]) -> Iterator[Path]:
    """Materialize a file tree in a TemporaryDirectory and yield its root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _materialize_tree(root, structure)
        yield root


def _normalize_pressure_section(markdown: str) -> str:
    lines = markdown.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        if line == PRESSURE_HEADING:
            out.append("")
            out.append(PRESSURE_PLACEHOLDER)
            out.append("")
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            continue
        i += 1
    return "\n".join(out)


# The resolved skill set depends on which skill source repos are checked out and
# synced on the machine, and the sync verb depends on whether a link already
# exists. Both differ between a working checkout and the `git archive` extract
# the canonical gate runs in — so a golden that pins them can only ever pass in
# one of the two, which is what kept scripts/self-test.sh red. Normalized for the
# same reason (and in the same way) as the pressure advisory: the structure and
# the presence of the line stay locked, only the volatile token is masked.
_SKILLS_LINE_RE = re.compile(r"^(\s*-\s+\*\*[a-z0-9-]*skills\*\*:\s*).+$", re.M)
_SYNC_VERB_RE = re.compile(r"\b(?:exists|symlink-context|write-context|install-skill):(?=\s)")


def normalize_sync_verbs(text: str) -> str:
    """Mask the sync verb, which reflects pre-existing local state, not contract."""
    return _SYNC_VERB_RE.sub(SYNC_VERB_PLACEHOLDER, text)


def normalize_golden(text: str, root: str | os.PathLike[str]) -> str:
    """Normalize volatile golden text such as host pressure and temp roots."""
    normalized = _normalize_pressure_section(text)
    root_text = str(root)
    if root_text:
        normalized = normalized.replace(root_text, ROOT_PLACEHOLDER)
    normalized = _SKILLS_LINE_RE.sub(rf"\1{INSTALLED_SKILLS_PLACEHOLDER}", normalized)
    normalized = _SYNC_VERB_RE.sub(SYNC_VERB_PLACEHOLDER, normalized)
    return normalized


def make_fake_binary(directory: str | os.PathLike[str], name: str, script: str) -> Path:
    """Create an executable test binary in directory and return its path."""
    target = Path(directory) / _safe_relative_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = script if script.startswith("#!") else f"#!/usr/bin/env bash\n{script}"
    if not body.endswith("\n"):
        body += "\n"
    target.write_text(body, encoding="utf-8")
    target.chmod(0o755)
    return target


# ---------------------------------------------------------------------------
# Hermetic external-state joins (bead skillbox-era-program-v6ac.7.1)
#
# `sbp git` grew joins that read state OUTSIDE the repo under test: the
# reconcile receipts store, the two amp guard scripts, the fleet_convergence
# script. Each is env-overridable, and each defaults to a real path on the
# operator's machine. A fixture that forgets one silently scans the host: on
# 2026-08-15 a real receipts store leaked into fixture envelopes the same day
# the receipts join shipped, and the goldens started failing on one machine and
# passing on another.
#
# The fix is not "remember to pin them" — it is one registry, used by every
# suite, plus a byte-identity regression that fails when the registry falls
# behind the code (tests/test_git_estate_hermetic.py).
#
# ADDING A JOIN? Add its env var here. That is the whole contract: the
# regression test reads this list, so an unregistered join fails it.
# ---------------------------------------------------------------------------

#: Every env var that redirects an external-state join away from the host.
#: Values are pointed at paths under the test's tmp dir that are never created,
#: because "absent" is the state the default envelope is pinned against.
HERMETIC_JOIN_ENVS: tuple[str, ...] = (
    # reconcile receipts store -> `last_reconcile` fields
    "SKILLBOX_RECONCILE_RECEIPTS_DIR",
    # amp capsule guard script -> capsule verdict fields/markers
    "SKILLBOX_AMP_CAPSULE_GUARD",
    # amp campaign guard script -> campaign verdict fields/markers
    "SKILLBOX_AMP_CAMPAIGN_GUARD",
    # fleet_convergence.py -> --live origin state
    "SKILLBOX_FLEET_CONVERGENCE",
)

#: Budget overrides for those joins. Not hermeticity-critical on their own (an
#: absent join never spends its budget), but pinned so a slow host cannot make
#: a fixture flaky through a join it was not even testing.
HERMETIC_JOIN_BUDGET_ENVS: tuple[str, ...] = (
    "SKILLBOX_FLEET_CONVERGENCE_TIMEOUT_S",
    "SKILLBOX_AMP_GUARD_TIMEOUT_S",
)


def hermetic_join_env(tmp: str | os.PathLike[str], **overrides: str) -> dict[str, str]:
    """Env pinning every external-state join at a nonexistent path under ``tmp``.

    Pass ``overrides`` to point one join at a real fixture while the rest stay
    absent — that is how the join-specific suites test a present store without
    reopening the others to the host.

    The paths are deliberately *not* created. Absent is the state the default
    envelope is pinned against, and a directory that exists but is empty is a
    different code path in at least the receipts join.
    """
    base = Path(tmp)
    env = {name: str(base / f"no-{name.lower()}") for name in HERMETIC_JOIN_ENVS}
    env.update({name: "0.5" for name in HERMETIC_JOIN_BUDGET_ENVS})
    unknown = set(overrides) - set(HERMETIC_JOIN_ENVS) - set(HERMETIC_JOIN_BUDGET_ENVS)
    if unknown:
        raise AssertionError(
            f"unregistered join env(s) {sorted(unknown)}; add them to "
            "HERMETIC_JOIN_ENVS in tests/helpers.py"
        )
    env.update(overrides)
    return env
