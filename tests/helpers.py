"""Shared additive test fixtures.

Keep helpers here dependency-free and additive: move duplicated fixture setup
into this module without changing the assertions or behavioral intent of the
tests that adopt it.
"""

from __future__ import annotations

import copy
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


PRESSURE_HEADING = "## Pressure And Offload Policy"
PRESSURE_PLACEHOLDER = "<PRESSURE-ADVISORY-NORMALIZED>"
ROOT_PLACEHOLDER = "<ROOT>"


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


def normalize_golden(text: str, root: str | os.PathLike[str]) -> str:
    """Normalize volatile golden text such as host pressure and temp roots."""
    normalized = _normalize_pressure_section(text)
    root_text = str(root)
    if root_text:
        normalized = normalized.replace(root_text, ROOT_PLACEHOLDER)
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
