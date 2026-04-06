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


class LocalRuntimeCoreModelTests(unittest.TestCase):
    """WG-007: Model-level tests for local_runtime_core_cutover contract.

    Covers the bullet list under "Model-level" in the WG-007 brief:
      * bootstrap_task XOR-owner validation (both/neither -> LOCAL_RUNTIME_COVERAGE_GAP)
      * All seven stable error codes are exported constants
      * Canonical constants (LOCAL_RUNTIME_START_MODES, PARITY_LEDGER_ACTIONS,
        PARITY_OWNERSHIP_STATES, CANONICAL_RUNTIME_RECORDS) are exposed
      * Flattening rules for env_files, services, bootstrap_tasks:
          source.kind -> source_kind
          healthcheck.type/url/port -> health_type/health_target
          commands.<mode> -> service_mode_command records
    """

    # --- stable error codes (US-1..US-4) ---------------------------------

    def test_all_seven_stable_error_codes_are_exported_constants(self) -> None:
        expected = {
            "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
            "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            "LOCAL_RUNTIME_PROFILE_UNKNOWN",
            "LOCAL_RUNTIME_START_BLOCKED",
            "LOCAL_RUNTIME_SERVICE_DEFERRED",
            "LOCAL_RUNTIME_MODE_UNSUPPORTED",
            "LOCAL_RUNTIME_COVERAGE_GAP",
        }
        for code in expected:
            self.assertTrue(
                hasattr(runtime_model_module, code),
                f"runtime_model must export {code} as a module constant",
            )
            self.assertEqual(getattr(runtime_model_module, code), code)
        self.assertEqual(
            set(runtime_model_module.LOCAL_RUNTIME_ERROR_CODES), expected
        )

    def test_local_runtime_start_modes_and_parity_sets_are_exported(self) -> None:
        self.assertEqual(
            runtime_model_module.LOCAL_RUNTIME_START_MODES,
            ("reuse", "prod", "fresh"),
        )
        self.assertEqual(
            set(runtime_model_module.PARITY_LEDGER_ACTIONS),
            {"declare", "bridge", "build", "drop"},
        )
        self.assertEqual(
            set(runtime_model_module.PARITY_OWNERSHIP_STATES),
            {"covered", "bridge-only", "deferred", "external"},
        )

    def test_canonical_runtime_records_declares_bootstrap_task_xor_fields(self) -> None:
        records = runtime_model_module.CANONICAL_RUNTIME_RECORDS
        self.assertIn("bootstrap_task", records)
        self.assertIn("repo_id", records["bootstrap_task"])
        self.assertIn("bridge_id", records["bootstrap_task"])
        self.assertIn("service_mode_command", records)
        self.assertIn("parity_ledger_item", records)
        self.assertIn("legacy_env_bridge", records)

    # --- bootstrap_task XOR owner validation (US-3) ----------------------

    def test_bootstrap_task_xor_rejects_both_owners(self) -> None:
        tasks = [
            {
                "id": "bad-task",
                "kind": "bootstrap",
                "repo_id": "approval-feedback-api",
                "bridge_id": "local-core-bridge",
                "command": "true",
            }
        ]
        with self.assertRaises(runtime_model_module.LocalRuntimeContractError) as ctx:
            runtime_model_module._validate_bootstrap_task_owner_xor(tasks)
        self.assertEqual(
            ctx.exception.code,
            runtime_model_module.LOCAL_RUNTIME_COVERAGE_GAP,
        )

    def test_bootstrap_task_xor_rejects_neither_owner(self) -> None:
        tasks = [
            {
                "id": "orphan-task",
                "kind": "bootstrap",
                "command": "true",
            }
        ]
        with self.assertRaises(runtime_model_module.LocalRuntimeContractError) as ctx:
            runtime_model_module._validate_bootstrap_task_owner_xor(tasks)
        self.assertEqual(
            ctx.exception.code,
            runtime_model_module.LOCAL_RUNTIME_COVERAGE_GAP,
        )

    def test_bootstrap_task_xor_accepts_bridge_only_owner(self) -> None:
        tasks = [
            {
                "id": "env-bridge-local-core",
                "kind": "bootstrap",
                "bridge_id": "local-core-bridge",
                "command": "sync.sh --emit",
            }
        ]
        # must not raise
        runtime_model_module._validate_bootstrap_task_owner_xor(tasks)

    def test_bootstrap_task_xor_accepts_repo_only_owner(self) -> None:
        tasks = [
            {
                "id": "approval-feedback-db-bootstrap",
                "kind": "bootstrap",
                "repo_id": "approval-feedback-api",
                "command": "docker start unclawg-db-1",
            }
        ]
        runtime_model_module._validate_bootstrap_task_owner_xor(tasks)

    def test_bootstrap_task_xor_ignores_non_bootstrap_tasks(self) -> None:
        # Existing core runtime tasks declare neither owner; they must
        # remain valid because they are not bootstrap tasks.
        tasks = [
            {"id": "legacy-task", "kind": "task", "command": "noop"},
        ]
        runtime_model_module._validate_bootstrap_task_owner_xor(tasks)

    # --- flattening rules (shared.md:269-272) ----------------------------

    def test_flatten_env_file_promotes_source_kind_and_path(self) -> None:
        env_file = {
            "id": "approval-feedback-env",
            "repo": "approval-feedback-api",
            "path": "/repo/approval_feedback_api/.env",
            "source": {
                "kind": "file",
                "source_path": "/bridge/out/unclawg-approval-feedback-api/local.env",
            },
        }
        runtime_model_module._flatten_env_file_record(env_file)
        self.assertEqual(env_file["source_kind"], "file")
        self.assertEqual(
            env_file["source_path"],
            "/bridge/out/unclawg-approval-feedback-api/local.env",
        )
        self.assertEqual(env_file["target_path"], "/repo/approval_feedback_api/.env")
        self.assertEqual(env_file["repo_id"], "approval-feedback-api")

    def test_flatten_service_promotes_healthcheck_type_and_url(self) -> None:
        service = {
            "id": "approval_feedback_api",
            "repo": "approval-feedback-api",
            "healthcheck": {"type": "http", "url": "http://localhost:8010/health"},
            "commands": {
                "reuse": "make local-up-daemon",
                "prod": "make local-up-prod",
                "fresh": "make local-up-prod-fresh",
            },
        }
        extracted = runtime_model_module._flatten_service_record(service)
        self.assertEqual(service["health_type"], "http")
        self.assertEqual(service["health_target"], "http://localhost:8010/health")
        self.assertEqual(service["repo_id"], "approval-feedback-api")

        # commands.<mode> -> service_mode_command records
        by_mode = {rec["mode"]: rec for rec in extracted}
        self.assertEqual(set(by_mode), {"reuse", "prod", "fresh"})
        self.assertEqual(by_mode["reuse"]["service_id"], "approval_feedback_api")
        self.assertEqual(by_mode["prod"]["command"], "make local-up-prod")
        self.assertEqual(by_mode["fresh"]["command"], "make local-up-prod-fresh")
        self.assertEqual(
            by_mode["reuse"]["id"], "approval_feedback_api:reuse"
        )

    def test_flatten_service_without_commands_extracts_no_mode_records(self) -> None:
        service = {
            "id": "legacy",
            "command": "python3 -m http.server",
            "healthcheck": {"type": "port", "port": 8080},
        }
        extracted = runtime_model_module._flatten_service_record(service)
        self.assertEqual(extracted, [])
        self.assertEqual(service["health_type"], "port")
        self.assertEqual(service["health_target"], 8080)

    def test_flatten_bootstrap_task_promotes_repo_to_repo_id(self) -> None:
        task = {
            "id": "approval-feedback-db-bootstrap",
            "kind": "bootstrap",
            "repo": "approval-feedback-api",
            "command": "docker start unclawg-db-1",
        }
        runtime_model_module._flatten_bootstrap_task_record(task)
        self.assertEqual(task["repo_id"], "approval-feedback-api")


if __name__ == "__main__":
    unittest.main()
