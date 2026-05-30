"""Golden / drift guard for the runtime context generators.

This module locks the *current* output of the runtime context generators so
that any future, unintended change to context generation fails loudly:

    - ``generate_context_markdown`` for the **core** profile (built against the
      live repo runtime graph) and for a deterministic **personal/iOS** client
      fixture (the same fixture pattern used by ``test_ios_project_contract``).
    - ``generate_skill_context`` for the client fixture (the resolved
      ``context.yaml`` body that ``make focus`` / ``runtime-sync`` writes).
    - The ``context --dry-run --format json`` action contract emitted by the
      ``manage.py context`` CLI handler (``_handle_context``).

What drift this guards
----------------------
The generators stitch together many small section renderers (header, environment,
tooling, pressure policy, repos, services, tasks, skills, logs, quick reference).
A refactor that silently drops a section, reorders lines, renames a heading,
changes a ``make`` command suffix, or alters the skill-context YAML shape would
change agent-facing context without any other test noticing. These goldens make
such a change a visible, reviewable diff.

Why some fields are normalized (not asserted verbatim)
------------------------------------------------------
A few fragments of the markdown are intrinsically host- and time-dependent and
would make the golden non-reproducible across machines / runs. Rather than
*dropping* that content, we normalize the volatile tokens to stable
placeholders so the surrounding structure is still locked:

    - The ``## Pressure And Offload Policy`` section body reads live host disk
      pressure, tailscale worker state, and a host-derived warnings list. Its
      body lines are collapsed to a single ``<PRESSURE-ADVISORY-NORMALIZED>``
      placeholder; the heading and the section's presence are still asserted.
    - The temporary fixture root (an absolute, per-run ``mkdtemp`` path) that
      appears in the resolved skill ``context.yaml`` is normalized to
      ``<ROOT>``.

Regenerating the goldens intentionally
--------------------------------------
If you *meant* to change context generation, regenerate the goldens and review
the resulting diff before committing:

    REGEN_CONTEXT_GOLDENS=1 python3 -m unittest tests.test_context_drift_goldens

The same run then re-asserts against the freshly written files, so a green run
after regeneration confirms the new output is internally consistent.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
SCRIPTS_DIR = ROOT_DIR / "scripts"
for _path in (ENV_MANAGER_DIR, SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from lib.runtime_model import IOS_COMMAND_LANES, build_runtime_model  # noqa: E402
from runtime_manager.context_rendering import (  # noqa: E402
    generate_context_markdown,
    generate_skill_context,
)
from runtime_manager.validation import (  # noqa: E402
    filter_model,
    normalize_active_clients,
    normalize_active_profiles,
)


GOLDENS_DIR = Path(__file__).resolve().parent / "goldens" / "context"
REGEN_ENV = "REGEN_CONTEXT_GOLDENS"

PRESSURE_HEADING = "## Pressure And Offload Policy"
PRESSURE_PLACEHOLDER = "<PRESSURE-ADVISORY-NORMALIZED>"


def _normalize_pressure_section(markdown: str) -> str:
    """Collapse the host-dependent pressure-policy body to a placeholder.

    The heading is preserved (its presence is part of the contract); every
    body line up to the next ``## `` heading or end-of-string is replaced with
    a single placeholder line so live disk / tailscale / warning state does not
    leak into the golden.
    """
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
            # Skip the original body until the next top-level heading.
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            continue
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Deterministic client fixture (mirrors tests/test_ios_project_contract.py so
# the golden tracks the same canonical iOS overlay shape).
# ---------------------------------------------------------------------------
def _write_runtime_root(root: Path) -> None:
    (root / "workspace").mkdir(parents=True)
    (root / "clients" / "personal").mkdir(parents=True)
    (root / "monoserver" / "recipe-ios").mkdir(parents=True)
    (root / ".skillbox-state").mkdir()
    (root / ".env.example").write_text(
        "\n".join(
            [
                "SKILLBOX_STATE_ROOT=./.skillbox-state",
                "SKILLBOX_WORKSPACE_ROOT=/workspace",
                "SKILLBOX_REPOS_ROOT=/workspace/repos",
                "SKILLBOX_SKILLS_ROOT=/workspace/skills",
                "SKILLBOX_LOG_ROOT=/workspace/logs",
                "SKILLBOX_HOME_ROOT=/home/sandbox",
                "SKILLBOX_MONOSERVER_ROOT=/monoserver",
                "SKILLBOX_CLIENTS_ROOT=/workspace/workspace/clients",
                "SKILLBOX_CLIENTS_HOST_ROOT=./clients",
                "SKILLBOX_MONOSERVER_HOST_ROOT=./monoserver",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "workspace" / "persistence.yaml").write_text(
        "version: 1\n"
        "state_root_env: SKILLBOX_STATE_ROOT\n"
        "targets:\n"
        "  local:\n"
        "    provider: local\n"
        "    default_state_root: ./.skillbox-state\n"
        "bindings:\n"
        "  - id: workspace-root\n"
        "    runtime_path: /workspace\n"
        "    storage_class: external\n"
        "    source_ref: root_dir\n"
        "  - id: clients-root\n"
        "    runtime_path: /workspace/workspace/clients\n"
        "    storage_class: persistent\n"
        "    relative_path: clients\n"
        "  - id: monoserver-root\n"
        "    runtime_path: /monoserver\n"
        "    storage_class: persistent\n"
        "    relative_path: monoserver\n",
        encoding="utf-8",
    )
    (root / "workspace" / "runtime.yaml").write_text(
        "version: 2\n"
        "selection: {}\n"
        "core:\n"
        "  repos: []\n"
        "  artifacts: []\n"
        "  env_files: []\n"
        "  skills: []\n"
        "  tasks: []\n"
        "  services: []\n"
        "  logs: []\n"
        "  checks: []\n",
        encoding="utf-8",
    )


def _lane_yaml() -> str:
    lines: list[str] = []
    for lane_id in IOS_COMMAND_LANES:
        lines.append(f"        {lane_id}:")
        lines.append(f"          command: make {lane_id}")
    return "\n".join(lines) + "\n"


def _write_personal_overlay(root: Path) -> None:
    (root / "clients" / "personal" / "overlay.yaml").write_text(
        "version: 1\n"
        "client:\n"
        "  id: personal\n"
        "  label: Personal\n"
        "  default_cwd: ${SKILLBOX_MONOSERVER_ROOT}\n"
        "  context:\n"
        "    cwd_match:\n"
        "      - repos/app\n"
        "    workflow_builder:\n"
        "      workflow_root: workflows\n"
        "  repo_roots:\n"
        "    - id: personal-root\n"
        "      path: ${SKILLBOX_MONOSERVER_ROOT}\n"
        "      profiles: [core]\n"
        "      required: true\n"
        "      source: {kind: bind}\n"
        "      sync: {mode: external}\n"
        "  repos:\n"
        "    - id: recipe-ios\n"
        "      kind: repo\n"
        "      project_kind: ios\n"
        "      repo_path: ${SKILLBOX_MONOSERVER_ROOT}/recipe-ios\n"
        "      profiles: [core, local-core, local-all]\n"
        "      command_lanes:\n"
        f"{_lane_yaml()}"
        "  checks:\n"
        "    - id: recipe-ios-repo\n"
        "      type: path_exists\n"
        "      path: ${SKILLBOX_MONOSERVER_ROOT}/recipe-ios\n"
        "      required: true\n"
        "      profiles: [core]\n",
        encoding="utf-8",
    )


def _core_markdown() -> str:
    """Core-profile context markdown built against the live repo graph."""
    model = build_runtime_model(ROOT_DIR)
    active_profiles = normalize_active_profiles([])
    active_clients = normalize_active_clients(model, [])
    filtered = filter_model(model, active_profiles, active_clients)
    return _normalize_pressure_section(generate_context_markdown(filtered))


class _ClientFixture:
    """Materialized personal/iOS fixture: markdown + resolved skill context."""

    def __init__(self, tmp: tempfile.TemporaryDirectory) -> None:
        self._tmp = tmp
        self.root = Path(tmp.name).resolve()
        _write_runtime_root(self.root)
        _write_personal_overlay(self.root)
        model = build_runtime_model(self.root)
        active_profiles = normalize_active_profiles([])
        active_clients = normalize_active_clients(model, ["personal"])
        self.model = filter_model(model, active_profiles, active_clients)

    def markdown(self) -> str:
        return _normalize_pressure_section(generate_context_markdown(self.model))

    def skill_context(self) -> str:
        actions = generate_skill_context(self.model, self.root, dry_run=False)
        # The fixture declares exactly one client with a context block.
        assert actions == ["write-skill-context: clients/personal/context.yaml"], actions
        ctx_path = self.root / "clients" / "personal" / "context.yaml"
        raw = ctx_path.read_text(encoding="utf-8")
        # Normalize the per-run mkdtemp absolute root to a stable placeholder.
        return raw.replace(str(self.root), "<ROOT>")

    def close(self) -> None:
        self._tmp.cleanup()


def _dry_run_json_actions() -> dict:
    """The stable action contract from ``manage.py context --dry-run``.

    Mirrors ``_handle_context`` in runtime_manager/cli.py without launching a
    subprocess: ``sync_context(dry_run=True)`` plus the json envelope keys.
    """
    from runtime_manager.context_rendering import sync_context
    from runtime_manager.runtime_ops import next_actions_for_context

    model = build_runtime_model(ROOT_DIR)
    active_profiles = normalize_active_profiles([])
    active_clients = normalize_active_clients(model, [])
    filtered = filter_model(model, active_profiles, active_clients)
    actions = sync_context(filtered, ROOT_DIR, dry_run=True)
    return {
        "actions": actions,
        "dry_run": True,
        "next_actions": next_actions_for_context(),
    }


class ContextDriftGoldenTests(unittest.TestCase):
    """Lock current context-generator output; fail on unintended drift."""

    maxDiff = None
    _regen = bool(os.environ.get(REGEN_ENV))

    def _check(self, name: str, actual: str) -> None:
        path = GOLDENS_DIR / name
        if self._regen:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(actual, encoding="utf-8")
        self.assertTrue(
            path.is_file(),
            f"Missing golden {path}. Regenerate with "
            f"{REGEN_ENV}=1 python3 -m unittest tests.test_context_drift_goldens",
        )
        expected = path.read_text(encoding="utf-8")
        self.assertEqual(
            expected,
            actual,
            f"Context drift detected for golden '{name}'. If intentional, "
            f"regenerate with {REGEN_ENV}=1 and review the diff.",
        )

    def test_core_profile_markdown_matches_golden(self) -> None:
        self._check("core_context.md", _core_markdown())

    def test_client_profile_markdown_matches_golden(self) -> None:
        fixture = _ClientFixture(tempfile.TemporaryDirectory())
        try:
            self._check("personal_client_context.md", fixture.markdown())
        finally:
            fixture.close()

    def test_client_skill_context_matches_golden(self) -> None:
        fixture = _ClientFixture(tempfile.TemporaryDirectory())
        try:
            self._check("personal_client_skill_context.yaml", fixture.skill_context())
        finally:
            fixture.close()

    def test_context_dry_run_json_matches_golden(self) -> None:
        payload = _dry_run_json_actions()
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self._check("context_dry_run.json", rendered)

    def test_pressure_section_is_normalized_in_goldens(self) -> None:
        # Guard the guard: the volatile pressure body must not leak host state
        # (e.g. live "GiB free" disk numbers) into the committed goldens.
        for name in ("core_context.md", "personal_client_context.md"):
            text = (GOLDENS_DIR / name).read_text(encoding="utf-8")
            self.assertIn(PRESSURE_HEADING, text)
            self.assertIn(PRESSURE_PLACEHOLDER, text)
            self.assertNotRegex(
                text,
                r"GiB free",
                f"Live disk pressure leaked into golden '{name}'.",
            )


if __name__ == "__main__":
    unittest.main()
