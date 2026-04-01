from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import runtime_model as runtime_model_module


class RuntimeModelUnitTests(unittest.TestCase):
    def test_normalize_runtime_sections_merges_profiles_and_clients(self) -> None:
        resolved = {
            "version": 2,
            "selection": {"profiles": ["core"]},
            "core": {
                "repos": [{"id": "core-repo", "path": "/workspace"}],
                "services": [{"id": "core-service", "path": "/workspace/.env-manager"}],
            },
            "surfaces": {
                "services": [{"id": "api-service", "path": "/workspace/api", "profiles": ["surfaces"]}],
                "logs": [{"id": "api-log", "path": "/workspace/logs/api"}],
            },
            "clients": [
                {
                    "id": "acme",
                    "label": "Acme",
                    "repo_roots": [{"id": "acme-root", "path": "/workspace/repos/acme"}],
                    "repos": [{"id": "acme-repo", "path": "/workspace/repos/acme/app"}],
                    "checks": [{"id": "acme-check", "path": "/workspace/repos/acme"}],
                }
            ],
        }
        overlay_clients = [
            {
                "id": "beta",
                "label": "Beta",
                "default_cwd": "/monoserver/project-beta",
                "tasks": [{"id": "beta-task"}],
            }
        ]

        normalized = runtime_model_module._normalize_runtime_sections(
            resolved,
            overlay_clients=overlay_clients,
        )

        self.assertEqual(normalized["selection"], {"profiles": ["core"]})
        self.assertEqual({client["id"] for client in normalized["clients"]}, {"acme", "beta"})
        self.assertIn("core-repo", {repo["id"] for repo in normalized["repos"]})
        self.assertIn("acme-root", {repo["id"] for repo in normalized["repos"]})
        self.assertIn("api-service", {service["id"] for service in normalized["services"]})
        self.assertIn("beta-task", {task["id"] for task in normalized["tasks"]})
        self.assertIn("acme-check", {check["id"] for check in normalized["checks"]})

    def test_runtime_path_to_host_path_maps_known_roots(self) -> None:
        env_values = {
            "SKILLBOX_WORKSPACE_ROOT": "/workspace",
            "SKILLBOX_HOME_ROOT": "/home/sandbox",
            "SKILLBOX_MONOSERVER_ROOT": "/monoserver",
            "SKILLBOX_CLIENTS_ROOT": "/workspace/workspace/clients",
            "SKILLBOX_CLIENTS_HOST_ROOT": "./workspace/clients",
            "SKILLBOX_MONOSERVER_HOST_ROOT": "./monoserver-host",
        }

        clients_path = runtime_model_module.runtime_path_to_host_path(
            ROOT_DIR,
            env_values,
            "/workspace/workspace/clients/acme/overlay.yaml",
        )
        workspace_path = runtime_model_module.runtime_path_to_host_path(
            ROOT_DIR,
            env_values,
            "/workspace/scripts/box.py",
        )
        home_path = runtime_model_module.runtime_path_to_host_path(
            ROOT_DIR,
            env_values,
            "/home/sandbox/.claude/settings.json",
        )
        monoserver_path = runtime_model_module.runtime_path_to_host_path(
            ROOT_DIR,
            env_values,
            "/monoserver/project/app.py",
        )

        self.assertEqual(clients_path, ROOT_DIR / "workspace" / "clients" / "acme" / "overlay.yaml")
        self.assertEqual(workspace_path, ROOT_DIR / "scripts" / "box.py")
        self.assertEqual(home_path, ROOT_DIR / "home" / ".claude" / "settings.json")
        self.assertEqual(monoserver_path, (ROOT_DIR / "monoserver-host" / "project" / "app.py").resolve())

    def test_build_runtime_model_populates_host_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_runtime_fixture(repo)

            model = runtime_model_module.build_runtime_model(repo)

            repo_entry = next(item for item in model["repos"] if item["id"] == "skillbox-self")
            artifact_entry = next(item for item in model["artifacts"] if item["id"] == "tool-bin")
            env_file_entry = next(item for item in model["env_files"] if item["id"] == "runtime-env")
            skill_entry = next(item for item in model["skills"] if item["id"] == "default-skills")
            task_entry = next(item for item in model["tasks"] if item["id"] == "bootstrap")
            service_entry = next(item for item in model["services"] if item["id"] == "env-manager")
            log_entry = next(item for item in model["logs"] if item["id"] == "runtime-log")
            check_entry = next(item for item in model["checks"] if item["id"] == "workspace-root")
            client_entry = next(item for item in model["clients"] if item["id"] == "acme")

            self.assertEqual(Path(repo_entry["host_path"]).resolve(), repo.resolve())
            self.assertEqual(Path(artifact_entry["host_path"]).resolve(), (repo / "home" / ".local" / "bin" / "tool").resolve())
            self.assertEqual(Path(artifact_entry["source"]["host_path"]).resolve(), (repo / "artifacts" / "tool").resolve())
            self.assertEqual(Path(env_file_entry["host_path"]).resolve(), (repo / ".env").resolve())
            self.assertEqual(Path(env_file_entry["source"]["host_path"]).resolve(), (repo / "env" / "runtime.env").resolve())
            self.assertEqual(Path(skill_entry["bundle_dir_host_path"]).resolve(), (repo / "default-skills").resolve())
            self.assertEqual(
                Path(skill_entry["install_targets"][0]["host_path"]).resolve(),
                (repo / "home" / ".claude" / "skills").resolve(),
            )
            self.assertEqual(Path(task_entry["success"]["host_path"]).resolve(), (repo / ".done" / "bootstrap.ok").resolve())
            self.assertEqual(Path(service_entry["host_path"]).resolve(), (repo / ".env-manager" / "manage.py").resolve())
            self.assertEqual(Path(service_entry["healthcheck"]["host_path"]).resolve(), repo.resolve())
            self.assertEqual(Path(log_entry["host_path"]).resolve(), (repo / "logs" / "runtime").resolve())
            self.assertEqual(Path(check_entry["host_path"]).resolve(), repo.resolve())
            self.assertEqual(
                Path(client_entry["default_cwd_host_path"]).resolve(),
                (repo / "workspace" / "clients" / "acme").resolve(),
            )

    def _write_runtime_fixture(self, repo: Path) -> None:
        (repo / "workspace" / "clients" / "acme").mkdir(parents=True, exist_ok=True)
        (repo / "artifacts").mkdir(parents=True, exist_ok=True)
        (repo / "env").mkdir(parents=True, exist_ok=True)
        (repo / "home" / ".claude" / "skills").mkdir(parents=True, exist_ok=True)

        (repo / ".env.example").write_text(
            "SKILLBOX_NAME=skillbox\n"
            "SKILLBOX_WORKSPACE_ROOT=/workspace\n"
            "SKILLBOX_REPOS_ROOT=/workspace/repos\n"
            "SKILLBOX_SKILLS_ROOT=/workspace/skills\n"
            "SKILLBOX_LOG_ROOT=/workspace/logs\n"
            "SKILLBOX_HOME_ROOT=/home/sandbox\n"
            "SKILLBOX_MONOSERVER_ROOT=/monoserver\n"
            "SKILLBOX_CLIENTS_ROOT=/workspace/workspace/clients\n"
            "SKILLBOX_CLIENTS_HOST_ROOT=./workspace/clients\n"
            "SKILLBOX_MONOSERVER_HOST_ROOT=./monoserver-host\n",
            encoding="utf-8",
        )
        (repo / "workspace" / "runtime.yaml").write_text(
            "version: 2\n"
            "selection:\n"
            "  profiles:\n"
            "    - core\n"
            "core:\n"
            "  repos:\n"
            "    - id: skillbox-self\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "  artifacts:\n"
            "    - id: tool-bin\n"
            "      path: ${SKILLBOX_HOME_ROOT}/.local/bin/tool\n"
            "      source:\n"
            "        kind: file\n"
            "        path: ./artifacts/tool\n"
            "  env_files:\n"
            "    - id: runtime-env\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}/.env\n"
            "      source:\n"
            "        kind: file\n"
            "        path: ./env/runtime.env\n"
            "  skills:\n"
            "    - id: default-skills\n"
            "      bundle_dir: ${SKILLBOX_WORKSPACE_ROOT}/default-skills\n"
            "      manifest: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.manifest\n"
            "      sources_config: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.sources.yaml\n"
            "      lock_path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.lock.json\n"
            "      install_targets:\n"
            "        - id: claude\n"
            "          path: ${SKILLBOX_HOME_ROOT}/.claude/skills\n"
            "  tasks:\n"
            "    - id: bootstrap\n"
            "      success:\n"
            "        path: ${SKILLBOX_WORKSPACE_ROOT}/.done/bootstrap.ok\n"
            "  services:\n"
            "    - id: env-manager\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}/.env-manager/manage.py\n"
            "      healthcheck:\n"
            "        path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "  logs:\n"
            "    - id: runtime-log\n"
            "      path: ${SKILLBOX_LOG_ROOT}/runtime\n"
            "  checks:\n"
            "    - id: workspace-root\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "clients:\n"
            "  - id: acme\n"
            "    label: Acme\n"
            "    default_cwd: ${SKILLBOX_CLIENTS_ROOT}/acme\n",
            encoding="utf-8",
        )
        (repo / "artifacts" / "tool").write_text("tool\n", encoding="utf-8")
        (repo / "env" / "runtime.env").write_text("KEY=VALUE\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
