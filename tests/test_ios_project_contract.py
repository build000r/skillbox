from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
SCRIPTS_DIR = ROOT_DIR / "scripts"
for path in (ENV_MANAGER_DIR, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from lib.runtime_model import IOS_COMMAND_LANES, build_runtime_model  # noqa: E402
from runtime_manager.context_rendering import generate_context_markdown  # noqa: E402
from runtime_manager.runtime_ops import compact_runtime_status, runtime_status  # noqa: E402
from runtime_manager.validation import (  # noqa: E402
    check_manifest,
    filter_model,
    normalize_active_clients,
    normalize_active_profiles,
)


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


def _lane_yaml(*, exclude: set[str] | None = None) -> str:
    exclude = exclude or set()
    lines: list[str] = []
    for lane_id in IOS_COMMAND_LANES:
        if lane_id in exclude:
            continue
        lines.extend(
            [
                f"        {lane_id}:",
                f"          command: make {lane_id}",
            ]
        )
    return "\n".join(lines) + "\n"


def _write_personal_overlay(root: Path, *, exclude_lanes: set[str] | None = None) -> None:
    (root / "clients" / "personal" / "overlay.yaml").write_text(
        "version: 1\n"
        "client:\n"
        "  id: personal\n"
        "  label: Personal\n"
        "  default_cwd: ${SKILLBOX_MONOSERVER_ROOT}\n"
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
        f"{_lane_yaml(exclude=exclude_lanes)}"
        "  checks:\n"
        "    - id: recipe-ios-repo\n"
        "      type: path_exists\n"
        "      path: ${SKILLBOX_MONOSERVER_ROOT}/recipe-ios\n"
        "      required: true\n"
        "      profiles: [core]\n",
        encoding="utf-8",
    )


def _filtered_personal_model(root: Path) -> dict[str, object]:
    model = build_runtime_model(root)
    active_profiles = normalize_active_profiles([])
    active_clients = normalize_active_clients(model, ["personal"])
    return filter_model(model, active_profiles, active_clients)


class IOSProjectContractTests(unittest.TestCase):
    def test_ios_repo_and_check_are_visible_in_default_client_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_runtime_root(root)
            _write_personal_overlay(root)

            model = _filtered_personal_model(root)

            repos = {repo["id"]: repo for repo in model["repos"]}
            self.assertIn("recipe-ios", repos)
            self.assertEqual(repos["recipe-ios"]["project_kind"], "ios")
            self.assertEqual(list(repos["recipe-ios"]["command_lanes"].keys()), list(IOS_COMMAND_LANES))
            self.assertIn("recipe-ios-repo", {check["id"] for check in model["checks"]})

    def test_ios_project_requires_the_canonical_command_lane_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_runtime_root(root)
            _write_personal_overlay(root, exclude_lanes={"upload"})

            results = check_manifest(build_runtime_model(root))
            issues = "\n".join(
                issue
                for result in results
                for issue in (result.details or {}).get("issues", [])
            )

            self.assertIn("repo recipe-ios project_kind ios is missing command_lanes: upload", issues)

    def test_context_and_status_expose_ios_project_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_runtime_root(root)
            _write_personal_overlay(root)
            model = _filtered_personal_model(root)

            context = generate_context_markdown(model)
            self.assertIn("| recipe-ios | `/monoserver/recipe-ios` | repo | ios |", context)
            self.assertIn("device-local", context)

            full_status = runtime_status(model)
            recipe_status = next(repo for repo in full_status["repos"] if repo["id"] == "recipe-ios")
            self.assertEqual(recipe_status["project_kind"], "ios")
            self.assertIn("screenshots", recipe_status["command_lanes"])

            compact_status = compact_runtime_status(full_status)
            compact_recipe = next(repo for repo in compact_status["repos"] if repo["id"] == "recipe-ios")
            self.assertEqual(compact_recipe["project_kind"], "ios")
            self.assertIn("device-prod", compact_recipe["command_lanes"])


if __name__ == "__main__":
    unittest.main()
