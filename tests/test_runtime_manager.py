from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
MANAGER = ROOT_DIR / ".env-manager" / "manage.py"
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
MANAGE_MODULE = SourceFileLoader(
    "skillbox_manage",
    str(MANAGER.resolve()),
).load_module()


class RuntimeManagerTests(unittest.TestCase):
    def test_sync_creates_core_runtime_state_and_installs_default_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "sync", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            actions = payload["actions"]
            self.assertTrue((repo / "repos").is_dir())
            self.assertTrue((repo / "logs" / "runtime").is_dir())
            self.assertTrue((repo / "logs" / "repos").is_dir())
            self.assertFalse((repo / "logs" / "api").exists())
            self.assertFalse((repo / "logs" / "web").exists())
            self.assertTrue((repo / "home" / ".claude" / "skills" / "sample-skill" / "SKILL.md").is_file())
            self.assertTrue((repo / "home" / ".codex" / "skills" / "sample-skill" / "SKILL.md").is_file())
            self.assertTrue((repo / "home" / ".local" / "bin" / "swimmers").is_file())
            self.assertFalse((repo / "home" / ".claude" / "skills" / "personal-skill").exists())
            self.assertTrue((repo / "workspace" / "default-skills.lock.json").is_file())
            self.assertTrue(any("copy-if-missing:" in action for action in actions))
            self.assertTrue(any("install-skill:" in action for action in actions))
            self.assertTrue(any("write-lockfile:" in action for action in actions))

    def test_render_resolves_runtime_placeholders_and_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "render", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            repos = {item["id"]: item for item in payload["repos"]}
            artifacts = {item["id"]: item for item in payload["artifacts"]}
            skills = {item["id"]: item for item in payload["skills"]}
            self.assertEqual(payload["active_profiles"], ["core"])
            self.assertEqual(payload["active_clients"], [])
            self.assertEqual({client["id"] for client in payload["clients"]}, {"personal", "vibe-coding-client"})
            self.assertEqual(repos["skillbox-self"]["path"], "/workspace")
            self.assertEqual(repos["managed-repos"]["path"], "/workspace/repos")
            self.assertEqual(artifacts["swimmers-bin"]["path"], "/home/sandbox/.local/bin/swimmers")
            self.assertEqual(skills["default-skills"]["bundle_dir"], "/workspace/default-skills")
            self.assertEqual(
                skills["default-skills"]["install_targets"][0]["path"],
                "/home/sandbox/.claude/skills",
            )

    def test_doctor_warns_before_sync_and_passes_after_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            before = self._run(repo, "doctor", "--format", "json")
            self.assertEqual(before.returncode, 0, before.stderr)
            before_payload = json.loads(before.stdout)
            self.assertIn("checks", before_payload)
            self.assertIn("next_actions", before_payload)
            before_results = before_payload["checks"]
            before_warning_codes = {item["code"] for item in before_results if item["status"] == "warn"}
            self.assertIn("syncable-artifact-paths", before_warning_codes)
            self.assertIn("runtime-log-paths", before_warning_codes)
            self.assertIn("skill-lock-state", before_warning_codes)
            self.assertIn("skill-install-state", before_warning_codes)

            sync = self._run(repo, "sync")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            after = self._run(repo, "doctor", "--format", "json")
            self.assertEqual(after.returncode, 0, after.stderr)
            after_payload = json.loads(after.stdout)
            after_results = after_payload["checks"]
            after_warning_codes = {item["code"] for item in after_results if item["status"] == "warn"}
            after_failure_codes = {item["code"] for item in after_results if item["status"] == "fail"}
            self.assertNotIn("syncable-artifact-paths", after_warning_codes)
            self.assertNotIn("runtime-log-paths", after_warning_codes)
            self.assertNotIn("skill-lock-state", after_warning_codes)
            self.assertNotIn("skill-install-state", after_warning_codes)
            self.assertEqual(after_failure_codes, set(), after_results)

    def test_status_reports_installed_skill_targets_and_lock_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            sync = self._run(repo, "sync")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            status = self._run(repo, "status", "--format", "json")
            self.assertEqual(status.returncode, 0, status.stderr)
            payload = json.loads(status.stdout)
            artifact = payload["artifacts"][0]
            skillset = payload["skills"][0]
            skill_entry = skillset["skills"][0]
            target_states = {target["id"]: target["state"] for target in skill_entry["targets"]}

            self.assertEqual(payload["active_profiles"], ["core"])
            self.assertEqual(payload["active_clients"], [])
            self.assertEqual(artifact["id"], "swimmers-bin")
            self.assertTrue(artifact["present"])
            self.assertTrue(skillset["lock_present"])
            self.assertEqual(skill_entry["name"], "sample-skill")
            self.assertEqual(target_states["claude"], "ok")
            self.assertEqual(target_states["codex"], "ok")

    def test_client_selection_activates_client_repo_root_logs_and_skill_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            render = self._run(repo, "render", "--client", "personal", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            render_payload = json.loads(render.stdout)
            self.assertEqual(render_payload["active_profiles"], ["core"])
            self.assertEqual(render_payload["active_clients"], ["personal"])
            self.assertEqual(
                {item["id"] for item in render_payload["repos"]},
                {"skillbox-self", "managed-repos", "personal-root"},
            )
            self.assertEqual(
                {item["id"] for item in render_payload["skills"]},
                {"default-skills", "personal-skills"},
            )
            self.assertEqual(
                {item["id"] for item in render_payload["logs"]},
                {"runtime", "repos", "personal"},
            )

            sync = self._run(repo, "sync", "--client", "personal", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / "logs" / "clients" / "personal").is_dir())
            self.assertTrue((repo / "workspace" / "clients" / "personal" / "skills.lock.json").is_file())
            self.assertTrue((repo / "home" / ".claude" / "skills" / "personal-skill" / "SKILL.md").is_file())
            self.assertTrue((repo / "home" / ".codex" / "skills" / "personal-skill" / "SKILL.md").is_file())

    def test_profile_selection_limits_optional_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            render = self._run(repo, "render", "--profile", "surfaces", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            payload = json.loads(render.stdout)
            self.assertEqual(payload["active_profiles"], ["core", "surfaces"])
            self.assertEqual({item["id"] for item in payload["services"]}, {"internal-env-manager", "api-stub", "web-stub"})
            self.assertEqual({item["id"] for item in payload["logs"]}, {"runtime", "repos", "api", "web"})

    def test_profile_selection_activates_swimmers_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            render = self._run(repo, "render", "--profile", "swimmers", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            payload = json.loads(render.stdout)
            self.assertEqual(payload["active_profiles"], ["core", "swimmers"])
            self.assertEqual({item["id"] for item in payload["repos"]}, {"skillbox-self", "managed-repos"})
            self.assertEqual({item["id"] for item in payload["artifacts"]}, {"swimmers-bin"})
            self.assertEqual(
                {item["id"] for item in payload["services"]},
                {"internal-env-manager", "swimmers-server"},
            )
            self.assertEqual(
                {item["id"] for item in payload["logs"]},
                {"runtime", "repos", "swimmers"},
            )

            sync = self._run(repo, "sync", "--profile", "swimmers", "--format", "json")
            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / "logs" / "swimmers").is_dir())

    def test_profile_selection_activates_connectors_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            render = self._run(repo, "render", "--profile", "connectors", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            payload = json.loads(render.stdout)
            self.assertEqual(payload["active_profiles"], ["connectors", "core"])
            self.assertEqual(
                {item["id"] for item in payload["repos"]},
                {"skillbox-self", "managed-repos", "flywheel-connectors"},
            )
            self.assertEqual(
                {item["id"] for item in payload["artifacts"]},
                {"swimmers-bin", "fwc-bin", "dcg-bin"},
            )
            self.assertEqual(
                {item["id"] for item in payload["services"]},
                {"internal-env-manager", "fwc-mcp", "dcg-mcp"},
            )
            self.assertEqual(
                {item["id"] for item in payload["logs"]},
                {"runtime", "repos", "connectors"},
            )
            self.assertEqual(
                {item["id"] for item in payload["checks"]},
                {
                    "workspace-root",
                    "repos-root",
                    "skills-root",
                    "log-root",
                    "monoserver-root",
                    "runtime-manager",
                    "fwc-binary",
                    "dcg-binary",
                },
            )

            sync = self._run(repo, "sync", "--profile", "connectors", "--format", "json")
            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / "logs" / "connectors").is_dir())
            self.assertTrue((repo / "home" / ".local" / "bin" / "fwc").is_file())
            self.assertTrue((repo / "home" / ".local" / "bin" / "dcg").is_file())

            doctor = self._run(repo, "doctor", "--profile", "connectors", "--format", "json")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            doctor_payload = json.loads(doctor.stdout)
            manifest_check = next(item for item in doctor_payload["checks"] if item["code"] == "runtime-manifest")
            self.assertEqual(manifest_check["status"], "pass")

    def test_up_and_down_manage_selected_service_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._install_fixture_daemon(repo)
            self.addCleanup(self._run, repo, "down", "--service", "fixture-daemon", "--format", "json")

            up = self._run(repo, "up", "--service", "fixture-daemon", "--format", "json")

            self.assertEqual(up.returncode, 0, up.stderr)
            up_payload = json.loads(up.stdout)
            service_result = up_payload["services"][0]
            self.assertEqual(service_result["id"], "fixture-daemon")
            self.assertEqual(service_result["result"], "started")
            self.assertTrue((repo / "logs" / "runtime" / "fixture-daemon.pid").is_file())
            self.assertTrue((repo / "logs" / "runtime" / "fixture-daemon.ready").is_file())

            status = self._run(repo, "status", "--format", "json")
            self.assertEqual(status.returncode, 0, status.stderr)
            status_payload = json.loads(status.stdout)
            daemon_status = next(item for item in status_payload["services"] if item["id"] == "fixture-daemon")
            self.assertEqual(daemon_status["state"], "running")
            self.assertTrue(daemon_status["managed"])
            self.assertIsInstance(daemon_status["pid"], int)

            down = self._run(repo, "down", "--service", "fixture-daemon", "--format", "json")

            self.assertEqual(down.returncode, 0, down.stderr)
            down_payload = json.loads(down.stdout)
            self.assertIn(down_payload["services"][0]["result"], {"stopped", "killed"})
            self.assertFalse((repo / "logs" / "runtime" / "fixture-daemon.pid").exists())
            self.assertFalse((repo / "logs" / "runtime" / "fixture-daemon.ready").exists())

    def test_up_skips_status_only_services_and_logs_show_recent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._install_fixture_daemon(repo)
            self.addCleanup(self._run, repo, "down", "--service", "fixture-daemon", "--format", "json")

            up = self._run(repo, "up", "--format", "json")

            self.assertEqual(up.returncode, 0, up.stderr)
            up_payload = json.loads(up.stdout)
            results_by_id = {item["id"]: item for item in up_payload["services"]}
            self.assertEqual(results_by_id["internal-env-manager"]["result"], "skipped")
            self.assertEqual(results_by_id["fixture-daemon"]["result"], "started")

            logs = self._run(repo, "logs", "--service", "fixture-daemon", "--format", "json")

            self.assertEqual(logs.returncode, 0, logs.stderr)
            logs_payload = json.loads(logs.stdout)
            log_entry = logs_payload["services"][0]
            self.assertTrue(log_entry["present"])
            self.assertTrue(any("fixture-daemon ready" in line for line in log_entry["lines"]))

    def test_up_selected_service_starts_declared_dependencies_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._install_fixture_dependency_pair(repo)
            self.addCleanup(self._run, repo, "down", "--format", "json")

            up = self._run(repo, "up", "--service", "fixture-worker", "--format", "json")

            self.assertEqual(up.returncode, 0, up.stderr)
            up_payload = json.loads(up.stdout)
            self.assertEqual(
                [item["id"] for item in up_payload["services"]],
                ["fixture-daemon", "fixture-worker"],
            )
            self.assertEqual(
                [item["result"] for item in up_payload["services"]],
                ["started", "started"],
            )
            self.assertTrue((repo / "logs" / "runtime" / "fixture-worker.ready").is_file())

    def test_down_selected_dependency_stops_dependents_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._install_fixture_dependency_pair(repo)
            self.addCleanup(self._run, repo, "down", "--format", "json")

            up = self._run(repo, "up", "--service", "fixture-worker", "--format", "json")
            self.assertEqual(up.returncode, 0, up.stderr)

            down = self._run(repo, "down", "--service", "fixture-daemon", "--format", "json")

            self.assertEqual(down.returncode, 0, down.stderr)
            down_payload = json.loads(down.stdout)
            self.assertEqual(
                [item["id"] for item in down_payload["services"]],
                ["fixture-worker", "fixture-daemon"],
            )
            self.assertFalse((repo / "logs" / "runtime" / "fixture-worker.pid").exists())
            self.assertFalse((repo / "logs" / "runtime" / "fixture-daemon.pid").exists())

    def test_restart_replaces_running_service_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._install_fixture_daemon(repo)
            self.addCleanup(self._run, repo, "down", "--service", "fixture-daemon", "--format", "json")

            first_up = self._run(repo, "up", "--service", "fixture-daemon", "--format", "json")
            self.assertEqual(first_up.returncode, 0, first_up.stderr)
            first_pid = json.loads(first_up.stdout)["services"][0]["pid"]

            restart = self._run(repo, "restart", "--service", "fixture-daemon", "--format", "json")

            self.assertEqual(restart.returncode, 0, restart.stderr)
            restart_payload = json.loads(restart.stdout)
            second_pid = restart_payload["start_services"][0]["pid"]
            self.assertNotEqual(first_pid, second_pid)

    def test_restart_selected_service_restarts_graph_in_dependency_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._install_fixture_dependency_pair(repo)
            self.addCleanup(self._run, repo, "down", "--format", "json")

            up = self._run(repo, "up", "--service", "fixture-worker", "--format", "json")
            self.assertEqual(up.returncode, 0, up.stderr)

            restart = self._run(repo, "restart", "--service", "fixture-daemon", "--format", "json")

            self.assertEqual(restart.returncode, 0, restart.stderr)
            restart_payload = json.loads(restart.stdout)
            self.assertEqual(
                [item["id"] for item in restart_payload["stop_services"]],
                ["fixture-worker", "fixture-daemon"],
            )
            self.assertEqual(
                [item["id"] for item in restart_payload["start_services"]],
                ["fixture-daemon", "fixture-worker"],
            )

    def test_doctor_fails_when_selected_client_root_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "doctor", "--client", "vibe-coding-client", "--format", "json")

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            checks = payload["checks"]
            failure_codes = {item["code"] for item in checks if item["status"] == "fail"}
            self.assertIn("required-runtime-paths", failure_codes)
            self.assertIn("required-runtime-checks", failure_codes)

    def test_doctor_fails_when_declared_bundle_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo, include_bundle=False)

            result = self._run(repo, "doctor", "--format", "json")

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            checks = payload["checks"]
            failure_codes = {item["code"] for item in checks if item["status"] == "fail"}
            self.assertIn("skill-bundle-state", failure_codes)

    def test_doctor_fails_when_installed_skill_drifts_from_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            sync = self._run(repo, "sync")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            (repo / "home" / ".claude" / "skills" / "sample-skill" / "SKILL.md").write_text(
                "---\nname: sample-skill\ndescription: drifted\n---\n",
                encoding="utf-8",
            )

            result = self._run(repo, "doctor", "--format", "json")

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            checks = payload["checks"]
            install_failures = [
                item for item in checks if item["status"] == "fail" and item["code"] == "skill-install-state"
            ]
            self.assertEqual(len(install_failures), 1, checks)
            self.assertIn("claude", " ".join(install_failures[0]["details"]["issues"]))

    def test_doctor_fails_when_service_dependency_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            runtime_path = repo / "workspace" / "runtime.yaml"
            runtime_path.write_text(
                runtime_path.read_text(encoding="utf-8").replace(
                    "      log: api\n",
                    "      log: api\n"
                    "      depends_on:\n"
                    "        - missing-service\n",
                    1,
                ),
                encoding="utf-8",
            )

            result = self._run(repo, "doctor", "--profile", "surfaces", "--format", "json")

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            checks = payload["checks"]
            issues = checks[0]["details"]["issues"]
            self.assertTrue(
                any("references unknown dependency 'missing-service'" in issue for issue in issues),
                issues,
            )

    def test_doctor_fails_when_service_dependencies_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            runtime_path = repo / "workspace" / "runtime.yaml"
            runtime_path.write_text(
                runtime_path.read_text(encoding="utf-8")
                .replace(
                    "      log: api\n",
                    "      log: api\n"
                    "      depends_on:\n"
                    "        - web-stub\n",
                    1,
                )
                .replace(
                    "      log: web\n",
                    "      log: web\n"
                    "      depends_on:\n"
                    "        - api-stub\n",
                    1,
                ),
                encoding="utf-8",
            )

            result = self._run(repo, "doctor", "--profile", "surfaces", "--format", "json")

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            checks = payload["checks"]
            issues = checks[0]["details"]["issues"]
            self.assertTrue(
                any("service dependency cycle detected: api-stub -> web-stub -> api-stub" in issue for issue in issues),
                issues,
            )

    def test_client_init_scaffolds_overlay_and_supporting_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "client-init", "acme-studio", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["client_id"], "acme-studio")
            self.assertTrue((repo / "workspace" / "clients" / "acme-studio" / "overlay.yaml").is_file())
            self.assertTrue((repo / "workspace" / "clients" / "acme-studio" / "skills.manifest").is_file())
            self.assertTrue((repo / "workspace" / "clients" / "acme-studio" / "skills.sources.yaml").is_file())
            self.assertTrue((repo / "default-skills" / "clients" / "acme-studio" / "README.md").is_file())
            self.assertTrue((repo / "skills" / "clients" / "acme-studio" / ".gitkeep").is_file())

            render = self._run(repo, "render", "--client", "acme-studio", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            render_payload = json.loads(render.stdout)
            self.assertIn("acme-studio", {client["id"] for client in render_payload["clients"]})
            self.assertEqual(render_payload["active_clients"], ["acme-studio"])
            self.assertEqual(
                {item["id"] for item in render_payload["repos"]},
                {"skillbox-self", "managed-repos", "acme-studio-root"},
            )

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / "logs" / "clients" / "acme-studio").is_dir())
            self.assertTrue((repo / "workspace" / "clients" / "acme-studio" / "skills.lock.json").is_file())

    def test_render_reads_client_overlays_from_configured_clients_host_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            external_clients = repo / "private-config" / "clients"
            external_clients.mkdir(parents=True, exist_ok=True)
            (repo / ".env").write_text(
                "SKILLBOX_CLIENTS_HOST_ROOT=./private-config/clients\n",
                encoding="utf-8",
            )
            self._write_client_overlay(
                repo,
                "acme-studio",
                label="Acme Studio",
                default_cwd="${SKILLBOX_MONOSERVER_ROOT}/acme-studio",
                root_path="${SKILLBOX_MONOSERVER_ROOT}/acme-studio",
                clients_root=external_clients,
            )

            result = self._run(repo, "render", "--client", "acme-studio", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual({client["id"] for client in payload["clients"]}, {"acme-studio"})
            self.assertEqual(payload["active_clients"], ["acme-studio"])

    def test_client_init_writes_to_configured_clients_host_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            external_clients = repo / "private-config" / "clients"
            external_clients.mkdir(parents=True, exist_ok=True)
            (repo / ".env").write_text(
                "SKILLBOX_CLIENTS_HOST_ROOT=./private-config/clients\n",
                encoding="utf-8",
            )

            result = self._run(repo, "client-init", "acme-studio", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((external_clients / "acme-studio" / "overlay.yaml").is_file())
            self.assertTrue((external_clients / "acme-studio" / "skills.manifest").is_file())
            self.assertTrue((external_clients / "acme-studio" / "skills.sources.yaml").is_file())
            self.assertFalse((repo / "workspace" / "clients" / "acme-studio").exists())

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((external_clients / "acme-studio" / "skills.lock.json").is_file())

    def test_client_init_rejects_invalid_client_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "client-init", "Acme Studio", "--format", "json")

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("Invalid client id", payload["error"]["message"])
            self.assertEqual(payload["error"]["type"], "invalid_client_id")
            self.assertIn("recoverable", payload["error"])

    def test_client_init_refuses_existing_overlay_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "client-init", "personal", "--format", "json")

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("workspace/clients/personal/overlay.yaml", payload["error"]["message"])

    def test_client_init_lists_available_blueprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_client_blueprint(
                repo,
                "git-repo",
                "version: 1\n"
                "description: Clone a repo.\n"
                "variables:\n"
                "  - name: PRIMARY_REPO_URL\n"
                "    required: true\n"
                "client:\n"
                "  repos:\n"
                "    - id: app\n"
                "      kind: repo\n"
                "      path: ${CLIENT_ROOT}/app\n"
                "      source:\n"
                "        kind: git\n"
                "        url: ${PRIMARY_REPO_URL}\n"
                "      sync:\n"
                "        mode: clone-if-missing\n",
            )

            result = self._run(repo, "client-init", "--list-blueprints", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            blueprint = payload["blueprints"][0]
            self.assertEqual(blueprint["id"], "git-repo")
            self.assertEqual(blueprint["description"], "Clone a repo.")
            self.assertEqual(blueprint["variables"][0]["name"], "PRIMARY_REPO_URL")
            self.assertTrue(blueprint["variables"][0]["required"])

    def test_client_init_with_blueprint_scaffolds_repo_and_service_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_client_blueprint(
                repo,
                "git-repo-http-service",
                "version: 1\n"
                "description: Clone a primary repo and wire an HTTP service.\n"
                "variables:\n"
                "  - name: PRIMARY_REPO_ID\n"
                "    default: app\n"
                "  - name: PRIMARY_REPO_URL\n"
                "    required: true\n"
                "  - name: PRIMARY_REPO_BRANCH\n"
                "    default: main\n"
                "  - name: PRIMARY_REPO_PATH\n"
                "    default: ${CLIENT_ROOT}/${PRIMARY_REPO_ID}\n"
                "  - name: SERVICE_ID\n"
                "    default: app-dev\n"
                "  - name: SERVICE_COMMAND\n"
                "    required: true\n"
                "  - name: SERVICE_LOG_ID\n"
                "    default: ${CLIENT_ID}-services\n"
                "  - name: SERVICE_LOG_PATH\n"
                "    default: ${SKILLBOX_LOG_ROOT}/clients/${CLIENT_ID}/services\n"
                "  - name: SERVICE_PORT\n"
                "    default: \"4010\"\n"
                "client:\n"
                "  default_cwd: ${PRIMARY_REPO_PATH}\n"
                "  repos:\n"
                "    - id: ${PRIMARY_REPO_ID}\n"
                "      kind: repo\n"
                "      path: ${PRIMARY_REPO_PATH}\n"
                "      required: true\n"
                "      profiles:\n"
                "        - core\n"
                "      source:\n"
                "        kind: git\n"
                "        url: ${PRIMARY_REPO_URL}\n"
                "        branch: ${PRIMARY_REPO_BRANCH}\n"
                "      sync:\n"
                "        mode: clone-if-missing\n"
                "  logs:\n"
                "    - id: ${SERVICE_LOG_ID}\n"
                "      path: ${SERVICE_LOG_PATH}\n"
                "      profiles:\n"
                "        - core\n"
                "  services:\n"
                "    - id: ${SERVICE_ID}\n"
                "      kind: http\n"
                "      repo: ${PRIMARY_REPO_ID}\n"
                "      profiles:\n"
                "        - core\n"
                "      command: ${SERVICE_COMMAND}\n"
                "      log: ${SERVICE_LOG_ID}\n"
                "      healthcheck:\n"
                "        type: http\n"
                "        url: http://127.0.0.1:${SERVICE_PORT}/health\n"
                "        timeout_seconds: 0.5\n",
            )
            source_repo = self._create_git_source_repo(repo, "fixture-app")

            result = self._run(
                repo,
                "client-init",
                "acme-studio",
                "--blueprint",
                "git-repo-http-service",
                "--set",
                f"PRIMARY_REPO_URL={source_repo}",
                "--set",
                "SERVICE_COMMAND=python3 -m http.server 4010",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["blueprint"]["id"], "git-repo-http-service")
            self.assertTrue((repo / "workspace" / "clients" / "acme-studio" / "overlay.yaml").is_file())

            render = self._run(repo, "render", "--client", "acme-studio", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            render_payload = json.loads(render.stdout)
            self.assertEqual(render_payload["active_clients"], ["acme-studio"])
            self.assertEqual(
                {item["id"] for item in render_payload["repos"]},
                {"skillbox-self", "managed-repos", "acme-studio-root", "app"},
            )
            self.assertEqual(
                {item["id"] for item in render_payload["services"]},
                {"internal-env-manager", "app-dev"},
            )
            self.assertIn("acme-studio-services", {item["id"] for item in render_payload["logs"]})
            self.assertEqual(
                next(client for client in render_payload["clients"] if client["id"] == "acme-studio")["default_cwd"],
                "/monoserver/acme-studio/app",
            )

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / "monoserver-host" / "acme-studio" / "app" / "README.md").is_file())
            self.assertTrue((repo / "logs" / "clients" / "acme-studio" / "services").is_dir())
            self.assertTrue(any("clone-if-missing:" in action for action in json.loads(sync.stdout)["actions"]))

    def test_sync_hydrates_declared_client_env_file_and_status_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_env_blueprint(repo)
            source_repo = self._create_git_source_repo(repo, "fixture-app")
            self._write_env_source(repo, "acme-studio", "PORT=4010\nAPI_KEY=test-key\n")

            result = self._run(
                repo,
                "client-init",
                "acme-studio",
                "--blueprint",
                "git-repo-http-service-env",
                "--set",
                f"PRIMARY_REPO_URL={source_repo}",
                "--set",
                "SERVICE_COMMAND=python3 -m http.server 4010",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            (repo / "monoserver-host" / "acme-studio").mkdir(parents=True, exist_ok=True)

            before = self._run(repo, "doctor", "--client", "acme-studio", "--format", "json")
            self.assertEqual(before.returncode, 0, before.stderr)
            before_payload = json.loads(before.stdout)["checks"]
            warning_codes = {item["code"] for item in before_payload if item["status"] == "warn"}
            self.assertIn("syncable-env-files", warning_codes)

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")
            self.assertEqual(sync.returncode, 0, sync.stderr)
            sync_payload = json.loads(sync.stdout)
            self.assertTrue(any("hydrate-env:" in action for action in sync_payload["actions"]))

            env_target = repo / "monoserver-host" / "acme-studio" / "app" / ".env.local"
            self.assertTrue(env_target.is_file())
            self.assertEqual(env_target.read_text(encoding="utf-8"), "PORT=4010\nAPI_KEY=test-key\n")
            self.assertEqual(env_target.stat().st_mode & 0o777, 0o600)

            status = self._run(repo, "status", "--client", "acme-studio", "--format", "json")
            self.assertEqual(status.returncode, 0, status.stderr)
            status_payload = json.loads(status.stdout)
            env_file = status_payload["env_files"][0]
            self.assertEqual(env_file["id"], "app-env")
            self.assertTrue(env_file["present"])
            self.assertTrue(env_file["source_present"])
            self.assertEqual(env_file["state"], "ok")
            self.assertEqual(env_file["mode"], "0600")

            after = self._run(repo, "doctor", "--client", "acme-studio", "--format", "json")
            self.assertEqual(after.returncode, 0, after.stderr)
            after_payload = json.loads(after.stdout)["checks"]
            after_warning_codes = {item["code"] for item in after_payload if item["status"] == "warn"}
            after_failure_codes = {item["code"] for item in after_payload if item["status"] == "fail"}
            self.assertNotIn("syncable-env-files", after_warning_codes)
            self.assertEqual(after_failure_codes, set(), after_payload)

    def test_doctor_fails_when_required_env_source_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_env_blueprint(repo)
            source_repo = self._create_git_source_repo(repo, "fixture-app")

            result = self._run(
                repo,
                "client-init",
                "acme-studio",
                "--blueprint",
                "git-repo-http-service-env",
                "--set",
                f"PRIMARY_REPO_URL={source_repo}",
                "--set",
                "SERVICE_COMMAND=python3 -m http.server 4010",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            (repo / "monoserver-host" / "acme-studio").mkdir(parents=True, exist_ok=True)

            doctor = self._run(repo, "doctor", "--client", "acme-studio", "--format", "json")
            self.assertEqual(doctor.returncode, 2)
            checks = json.loads(doctor.stdout)["checks"]
            failures = [item for item in checks if item["status"] == "fail" and item["code"] == "required-runtime-env-files"]
            self.assertEqual(len(failures), 1, checks)
            self.assertIn("workspace/secrets/clients/acme-studio/app.env", " ".join(failures[0]["details"]["missing_sources"]))

    def test_up_refuses_to_start_service_when_required_env_is_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_env_blueprint(repo)
            source_repo = self._create_git_source_repo(repo, "fixture-app")

            result = self._run(
                repo,
                "client-init",
                "acme-studio",
                "--blueprint",
                "git-repo-http-service-env",
                "--set",
                f"PRIMARY_REPO_URL={source_repo}",
                "--set",
                "SERVICE_COMMAND=python3 -m http.server 4010",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)

            up = self._run(repo, "up", "--client", "acme-studio", "--service", "app-dev", "--format", "json")

            self.assertEqual(up.returncode, 1)
            payload = json.loads(up.stdout)
            self.assertIn("app-env", payload["error"]["message"])
            pid_file = repo / "logs" / "clients" / "acme-studio" / "services" / "app-dev.pid"
            self.assertFalse(pid_file.exists())

    def test_sync_only_hydrates_env_files_for_selected_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_env_blueprint(repo)
            source_repo = self._create_git_source_repo(repo, "fixture-app")
            self._write_env_source(repo, "acme-studio", "PORT=4010\nAPI_KEY=acme\n")
            self._write_env_source(repo, "beta-studio", "PORT=4010\nAPI_KEY=beta\n")

            for client_id in ("acme-studio", "beta-studio"):
                result = self._run(
                    repo,
                    "client-init",
                    client_id,
                    "--blueprint",
                    "git-repo-http-service-env",
                    "--set",
                    f"PRIMARY_REPO_URL={source_repo}",
                    "--set",
                    "SERVICE_COMMAND=python3 -m http.server 4010",
                    "--format",
                    "json",
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / "monoserver-host" / "acme-studio" / "app" / ".env.local").is_file())
            self.assertFalse((repo / "monoserver-host" / "beta-studio" / "app" / ".env.local").exists())

    def test_bootstrap_runs_tasks_in_dependency_order_and_status_reports_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._install_fixture_bootstrap_graph(repo)

            sync = self._run(repo, "sync")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            before_status = self._run(repo, "status", "--format", "json")
            self.assertEqual(before_status.returncode, 0, before_status.stderr)
            before_payload = json.loads(before_status.stdout)
            states_before = {item["id"]: item["state"] for item in before_payload["tasks"]}
            self.assertEqual(states_before["prepare-assets"], "pending")
            self.assertEqual(states_before["build-app"], "blocked")

            before_doctor = self._run(repo, "doctor", "--format", "json")
            self.assertEqual(before_doctor.returncode, 0, before_doctor.stderr)
            before_warning_codes = {
                item["code"]
                for item in json.loads(before_doctor.stdout)["checks"]
                if item["status"] == "warn"
            }
            self.assertIn("bootstrap-task-state", before_warning_codes)

            bootstrap = self._run(repo, "bootstrap", "--task", "build-app", "--format", "json")

            self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
            bootstrap_payload = json.loads(bootstrap.stdout)
            self.assertEqual(
                [item["id"] for item in bootstrap_payload["tasks"]],
                ["prepare-assets", "build-app"],
            )
            self.assertEqual(
                [item["result"] for item in bootstrap_payload["tasks"]],
                ["completed", "completed"],
            )

            order_file = repo / "logs" / "runtime" / "bootstrap-order.log"
            self.assertEqual(
                order_file.read_text(encoding="utf-8").splitlines(),
                ["prepare-assets", "build-app"],
            )

            after_status = self._run(repo, "status", "--format", "json")
            self.assertEqual(after_status.returncode, 0, after_status.stderr)
            after_payload = json.loads(after_status.stdout)
            states_after = {item["id"]: item["state"] for item in after_payload["tasks"]}
            self.assertEqual(states_after["prepare-assets"], "ready")
            self.assertEqual(states_after["build-app"], "ready")

            after_doctor = self._run(repo, "doctor", "--format", "json")
            self.assertEqual(after_doctor.returncode, 0, after_doctor.stderr)
            after_warning_codes = {
                item["code"]
                for item in json.loads(after_doctor.stdout)["checks"]
                if item["status"] == "warn"
            }
            self.assertNotIn("bootstrap-task-state", after_warning_codes)

    def test_up_runs_declared_bootstrap_tasks_before_service_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._install_fixture_bootstrap_graph(repo)
            self._install_fixture_bootstrap_service(repo)
            self.addCleanup(self._run, repo, "down", "--service", "bootstrap-daemon", "--format", "json")

            up = self._run(repo, "up", "--service", "bootstrap-daemon", "--format", "json")

            self.assertEqual(up.returncode, 0, up.stderr)
            up_payload = json.loads(up.stdout)
            self.assertEqual(
                [item["id"] for item in up_payload["bootstrap_tasks"]],
                ["prepare-assets", "build-app"],
            )
            self.assertEqual(up_payload["services"][0]["id"], "bootstrap-daemon")
            self.assertEqual(up_payload["services"][0]["result"], "started")
            self.assertTrue((repo / "logs" / "runtime" / "build-app.ok").is_file())
            self.assertTrue((repo / "logs" / "runtime" / "bootstrap-daemon.ready").is_file())

    def test_client_init_with_blueprint_scaffolds_task_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_client_blueprint(
                repo,
                "git-repo-http-service-bootstrap",
                "version: 1\n"
                "description: Clone a primary repo, declare a bootstrap task, and wire an HTTP service.\n"
                "variables:\n"
                "  - name: PRIMARY_REPO_ID\n"
                "    default: app\n"
                "  - name: PRIMARY_REPO_URL\n"
                "    required: true\n"
                "  - name: PRIMARY_REPO_PATH\n"
                "    default: ${CLIENT_ROOT}/${PRIMARY_REPO_ID}\n"
                "  - name: BOOTSTRAP_TASK_ID\n"
                "    default: app-bootstrap\n"
                "  - name: BOOTSTRAP_COMMAND\n"
                "    required: true\n"
                "  - name: BOOTSTRAP_SUCCESS_PATH\n"
                "    default: ${PRIMARY_REPO_PATH}/.skillbox/bootstrap.ok\n"
                "  - name: SERVICE_ID\n"
                "    default: app-dev\n"
                "  - name: SERVICE_COMMAND\n"
                "    required: true\n"
                "  - name: SERVICE_LOG_ID\n"
                "    default: ${CLIENT_ID}-services\n"
                "  - name: SERVICE_LOG_PATH\n"
                "    default: ${SKILLBOX_LOG_ROOT}/clients/${CLIENT_ID}/services\n"
                "client:\n"
                "  default_cwd: ${PRIMARY_REPO_PATH}\n"
                "  repos:\n"
                "    - id: ${PRIMARY_REPO_ID}\n"
                "      kind: repo\n"
                "      path: ${PRIMARY_REPO_PATH}\n"
                "      required: true\n"
                "      profiles:\n"
                "        - core\n"
                "      source:\n"
                "        kind: git\n"
                "        url: ${PRIMARY_REPO_URL}\n"
                "      sync:\n"
                "        mode: clone-if-missing\n"
                "  logs:\n"
                "    - id: ${SERVICE_LOG_ID}\n"
                "      path: ${SERVICE_LOG_PATH}\n"
                "      profiles:\n"
                "        - core\n"
                "  tasks:\n"
                "    - id: ${BOOTSTRAP_TASK_ID}\n"
                "      kind: bootstrap\n"
                "      repo: ${PRIMARY_REPO_ID}\n"
                "      profiles:\n"
                "        - core\n"
                "      command: ${BOOTSTRAP_COMMAND}\n"
                "      outputs:\n"
                "        - ${BOOTSTRAP_SUCCESS_PATH}\n"
                "      success:\n"
                "        type: path_exists\n"
                "        path: ${BOOTSTRAP_SUCCESS_PATH}\n"
                "      log: ${SERVICE_LOG_ID}\n"
                "  services:\n"
                "    - id: ${SERVICE_ID}\n"
                "      kind: http\n"
                "      repo: ${PRIMARY_REPO_ID}\n"
                "      profiles:\n"
                "        - core\n"
                "      bootstrap_tasks:\n"
                "        - ${BOOTSTRAP_TASK_ID}\n"
                "      command: ${SERVICE_COMMAND}\n"
                "      log: ${SERVICE_LOG_ID}\n"
                "      healthcheck:\n"
                "        type: path_exists\n"
                "        path: ${PRIMARY_REPO_PATH}/README.md\n",
            )
            source_repo = self._create_git_source_repo(repo, "fixture-app")

            result = self._run(
                repo,
                "client-init",
                "acme-studio",
                "--blueprint",
                "git-repo-http-service-bootstrap",
                "--set",
                f"PRIMARY_REPO_URL={source_repo}",
                "--set",
                "BOOTSTRAP_COMMAND=python3 -c \"from pathlib import Path; Path('.skillbox').mkdir(exist_ok=True); Path('.skillbox/bootstrap.ok').write_text('ok\\n', encoding='utf-8')\"",
                "--set",
                "SERVICE_COMMAND=python3 -m http.server 4010",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)

            render = self._run(repo, "render", "--client", "acme-studio", "--format", "json")
            self.assertEqual(render.returncode, 0, render.stderr)
            render_payload = json.loads(render.stdout)
            self.assertEqual({item["id"] for item in render_payload["tasks"]}, {"app-bootstrap"})
            service = next(item for item in render_payload["services"] if item["id"] == "app-dev")
            self.assertEqual(service["bootstrap_tasks"], ["app-bootstrap"])

    def test_client_init_with_blueprint_requires_declared_variables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_client_blueprint(
                repo,
                "git-repo",
                "version: 1\n"
                "description: Clone a repo.\n"
                "variables:\n"
                "  - name: PRIMARY_REPO_URL\n"
                "    required: true\n"
                "client:\n"
                "  repos:\n"
                "    - id: app\n"
                "      kind: repo\n"
                "      path: ${CLIENT_ROOT}/app\n"
                "      source:\n"
                "        kind: git\n"
                "        url: ${PRIMARY_REPO_URL}\n"
                "      sync:\n"
                "        mode: clone-if-missing\n",
            )

            result = self._run(
                repo,
                "client-init",
                "acme-studio",
                "--blueprint",
                "git-repo",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("PRIMARY_REPO_URL", payload["error"]["message"])

    def test_render_keeps_supporting_inline_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            runtime_path = repo / "workspace" / "runtime.yaml"
            runtime_path.write_text(
                runtime_path.read_text(encoding="utf-8")
                + "\n"
                + "clients:\n"
                + "  - id: legacy-inline\n"
                + "    label: Legacy Inline\n"
                + "    default_cwd: ${SKILLBOX_MONOSERVER_ROOT}/legacy-inline\n"
                + "    repo_roots:\n"
                + "      - id: legacy-inline-root\n"
                + "        path: ${SKILLBOX_MONOSERVER_ROOT}/legacy-inline\n"
                + "        required: true\n"
                + "        profiles:\n"
                + "          - core\n"
                + "        source:\n"
                + "          kind: bind\n"
                + "        sync:\n"
                + "          mode: external\n",
                encoding="utf-8",
            )

            result = self._run(repo, "render", "--client", "legacy-inline", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("legacy-inline", {client["id"] for client in payload["clients"]})
            self.assertEqual(payload["active_clients"], ["legacy-inline"])
            self.assertEqual(
                {item["id"] for item in payload["repos"]},
                {"skillbox-self", "managed-repos", "legacy-inline-root"},
            )

    def test_client_project_writes_single_client_bundle_with_sanitized_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            (repo / ".env").write_text("SKILLBOX_SWIMMERS_AUTH_TOKEN=top-secret\n", encoding="utf-8")

            result = self._run(repo, "client-project", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            projection_dir = (repo / "builds" / "clients" / "personal").resolve()
            self.assertEqual(payload["output_dir"], str(projection_dir))

            self.assertTrue((projection_dir / "workspace" / "runtime.yaml").is_file())
            self.assertTrue((projection_dir / "projection.json").is_file())
            self.assertTrue((projection_dir / "runtime-model.json").is_file())
            self.assertTrue((projection_dir / "workspace" / "clients" / "personal" / "overlay.yaml").is_file())
            self.assertTrue((projection_dir / "workspace" / "clients" / "personal" / "skills.manifest").is_file())
            self.assertTrue((projection_dir / "workspace" / "clients" / "personal" / "skills.sources.yaml").is_file())
            self.assertTrue((projection_dir / "default-skills" / "sample-skill.skill").is_file())
            self.assertTrue((projection_dir / "default-skills" / "clients" / "personal" / "personal-skill.skill").is_file())

            self.assertFalse((projection_dir / "workspace" / "clients" / "vibe-coding-client").exists())
            self.assertFalse((projection_dir / "default-skills" / "clients" / "vibe-coding-client").exists())

            runtime_doc = MANAGE_MODULE.load_yaml(projection_dir / "workspace" / "runtime.yaml")
            self.assertEqual(runtime_doc["selection"]["default_client"], "personal")
            runtime_text = (projection_dir / "workspace" / "runtime.yaml").read_text(encoding="utf-8")
            self.assertNotIn("vibe-coding-client", runtime_text)

            projection_payload = json.loads((projection_dir / "projection.json").read_text(encoding="utf-8"))
            self.assertEqual(projection_payload["client_id"], "personal")
            self.assertEqual(projection_payload["overlay_mode"], "overlay")
            projected_paths = {item["path"] for item in projection_payload["files"]}
            self.assertIn("workspace/runtime.yaml", projected_paths)
            self.assertNotIn("workspace/clients/vibe-coding-client/overlay.yaml", projected_paths)

            model_text = (projection_dir / "runtime-model.json").read_text(encoding="utf-8")
            model_payload = json.loads(model_text)
            self.assertEqual(model_payload["active_clients"], ["personal"])
            self.assertEqual(model_payload["active_profiles"], ["core"])
            self.assertNotIn("_host_path", model_text)
            self.assertNotIn("SKILLBOX_SWIMMERS_AUTH_TOKEN", model_text)
            self.assertNotIn("SKILLBOX_CLIENTS_HOST_ROOT", model_text)
            self.assertNotIn(str(repo), model_text)

    def test_client_project_supports_custom_output_dir_and_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "client-project",
                "personal",
                "--profile",
                "surfaces",
                "--output-dir",
                "./artifacts/projection-personal",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            projection_dir = (repo / "artifacts" / "projection-personal").resolve()
            self.assertEqual(payload["output_dir"], str(projection_dir))

            model_payload = json.loads((projection_dir / "runtime-model.json").read_text(encoding="utf-8"))
            self.assertEqual(model_payload["active_profiles"], ["core", "surfaces"])
            self.assertEqual(
                {item["id"] for item in model_payload["services"]},
                {"internal-env-manager", "api-stub", "web-stub"},
            )

    def test_client_project_refuses_to_overwrite_non_projection_output_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            existing_dir = repo / "artifacts" / "existing"
            existing_dir.mkdir(parents=True, exist_ok=True)
            (existing_dir / "note.txt").write_text("keep me\n", encoding="utf-8")

            result = self._run(
                repo,
                "client-project",
                "personal",
                "--output-dir",
                "./artifacts/existing",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "conflict")
            self.assertIn("already exists", payload["error"]["message"])

    def test_private_init_creates_attached_private_repo_and_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "private-init",
                "--path",
                "../private-config",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            private_repo = workspace / "private-config"

            self.assertTrue((private_repo / ".git").exists())
            self.assertTrue((private_repo / "clients").is_dir())
            self.assertEqual(payload["target_dir"], str(private_repo.resolve()))
            self.assertEqual(payload["clients_host_root"], str((private_repo / "clients").resolve()))

            env_text = (repo / ".env").read_text(encoding="utf-8")
            self.assertIn("SKILLBOX_CLIENTS_HOST_ROOT=../private-config/clients", env_text)
            self.assertTrue((private_repo / "clients" / "personal" / "overlay.yaml").is_file())
            self.assertTrue((private_repo / "clients" / "personal" / "skills.manifest").is_file())
            self.assertTrue((private_repo / "clients" / "vibe-coding-client" / "overlay.yaml").is_file())

    def test_client_publish_and_diff_use_attached_private_repo_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)
            self._init_git_repo(repo)

            init = self._run(
                repo,
                "private-init",
                "--path",
                "../private-config",
                "--format",
                "json",
            )
            self.assertEqual(init.returncode, 0, init.stderr)

            private_repo = workspace / "private-config"
            publish = self._run(
                repo,
                "client-publish",
                "personal",
                "--format",
                "json",
            )
            self.assertEqual(publish.returncode, 0, publish.stderr)
            publish_payload = json.loads(publish.stdout)
            self.assertEqual(publish_payload["target_dir"], str(private_repo.resolve()))
            self.assertTrue((private_repo / "clients" / "personal" / "current" / "projection.json").is_file())

            diff = self._run(
                repo,
                "client-diff",
                "personal",
                "--format",
                "json",
            )
            self.assertEqual(diff.returncode, 0, diff.stderr)
            diff_payload = json.loads(diff.stdout)
            self.assertFalse(diff_payload["changed"])
            self.assertTrue(diff_payload["current"]["present"])

    def test_client_publish_requires_attached_private_repo_or_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "client-publish",
                "personal",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "missing_target_repo")
            self.assertIn("private-init", payload["error"]["message"])
            self.assertIn("--target-dir", payload["error"]["message"])

    def test_client_diff_rejects_inferred_private_repo_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            private_clients = repo / "private-config" / "clients"
            private_clients.mkdir(parents=True, exist_ok=True)
            shutil.copytree(repo / "workspace" / "clients" / "personal", private_clients / "personal")
            (private_clients / "personal" / "overlay.yaml").write_text(
                (repo / "workspace" / "clients" / "personal" / "overlay.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (repo / ".env").write_text(
                "SKILLBOX_CLIENTS_HOST_ROOT=./private-config/clients\n",
                encoding="utf-8",
            )

            result = self._run(
                repo,
                "client-diff",
                "personal",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "invalid_target_repo")
            self.assertIn("private-config", payload["error"]["message"])

    def test_client_publish_explicit_target_dir_overrides_attached_private_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)
            self._init_git_repo(repo)

            init = self._run(
                repo,
                "private-init",
                "--path",
                "../private-config",
                "--format",
                "json",
            )
            self.assertEqual(init.returncode, 0, init.stderr)

            private_repo = workspace / "private-config"
            control_repo = self._create_git_source_repo(repo, "control-plane")
            result = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["target_dir"], str(control_repo.resolve()))
            self.assertTrue((control_repo / "clients" / "personal" / "current" / "projection.json").is_file())
            self.assertFalse((private_repo / "clients" / "personal" / "current").exists())

    def test_client_publish_writes_current_payload_and_latest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._init_git_repo(repo)
            control_repo = self._create_git_source_repo(repo, "control-plane")

            result = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            current_dir = control_repo / "clients" / "personal" / "current"
            publish_path = control_repo / "clients" / "personal" / "publish.json"

            self.assertTrue(payload["changed"])
            self.assertFalse(payload["committed"])
            self.assertTrue((current_dir / "workspace" / "runtime.yaml").is_file())
            self.assertTrue((current_dir / "projection.json").is_file())
            self.assertTrue((current_dir / "runtime-model.json").is_file())
            self.assertFalse((current_dir / "workspace" / "clients" / "vibe-coding-client").exists())

            publish_payload = json.loads(publish_path.read_text(encoding="utf-8"))
            self.assertEqual(publish_payload["client_id"], "personal")
            self.assertEqual(publish_payload["payload_tree_sha256"], payload["payload_tree_sha256"])
            self.assertEqual(publish_payload["active_profiles"], ["core"])
            self.assertEqual(publish_payload["source_commit"], self._git_head(repo))

    def test_client_publish_can_commit_existing_bundle_to_target_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._init_git_repo(repo)
            control_repo = self._create_git_source_repo(repo, "control-plane")

            project = self._run(repo, "client-project", "personal", "--format", "json")
            self.assertEqual(project.returncode, 0, project.stderr)

            result = self._run(
                repo,
                "client-publish",
                "personal",
                "--from-bundle",
                "./builds/clients/personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--commit",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["changed"])
            self.assertTrue(payload["committed"])
            self.assertTrue(payload["commit_hash"])
            self.assertTrue((control_repo / "clients" / "personal" / "current" / "projection.json").is_file())

            commit_subject = subprocess.run(
                ["git", "log", "--format=%s", "-1"],
                cwd=control_repo,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(commit_subject.returncode, 0, commit_subject.stderr)
            self.assertEqual(commit_subject.stdout.strip(), "chore(client-publish): publish personal bundle")

    def test_client_publish_unknown_client_leaves_target_repo_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            control_repo = self._create_git_source_repo(repo, "control-plane")

            result = self._run(
                repo,
                "client-publish",
                "nonexistent",
                "--target-dir",
                "./fixtures/control-plane",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("Unknown runtime client", payload["error"]["message"])
            self.assertFalse((control_repo / "clients").exists())

    def test_client_publish_rejects_bundle_for_the_wrong_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            control_repo = self._create_git_source_repo(repo, "control-plane")

            project = self._run(repo, "client-project", "personal", "--format", "json")
            self.assertEqual(project.returncode, 0, project.stderr)

            result = self._run(
                repo,
                "client-publish",
                "vibe-coding-client",
                "--from-bundle",
                "./builds/clients/personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("personal", payload["error"]["message"])
            self.assertIn("vibe-coding-client", payload["error"]["message"])
            self.assertFalse((control_repo / "clients").exists())

    def test_client_publish_refuses_dirty_target_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            control_repo = self._create_git_source_repo(repo, "control-plane")
            note_path = control_repo / "note.txt"
            note_path.write_text("keep me\n", encoding="utf-8")

            result = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--commit",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "conflict")
            self.assertTrue(note_path.is_file())
            self.assertFalse((control_repo / "clients" / "personal").exists())

    def test_client_publish_is_idempotent_for_the_same_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._init_git_repo(repo)
            control_repo = self._create_git_source_repo(repo, "control-plane")

            project = self._run(repo, "client-project", "personal", "--format", "json")
            self.assertEqual(project.returncode, 0, project.stderr)

            first = self._run(
                repo,
                "client-publish",
                "personal",
                "--from-bundle",
                "./builds/clients/personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--commit",
                "--format",
                "json",
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            second = self._run(
                repo,
                "client-publish",
                "personal",
                "--from-bundle",
                "./builds/clients/personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--commit",
                "--format",
                "json",
            )

            self.assertEqual(second.returncode, 0, second.stderr)
            second_payload = json.loads(second.stdout)
            self.assertFalse(second_payload["changed"])
            self.assertIsNone(second_payload["commit_hash"])

            commit_count = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=control_repo,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(commit_count.returncode, 0, commit_count.stderr)
            self.assertEqual(commit_count.stdout.strip(), "2")

    def test_client_publish_leaves_other_client_payloads_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._init_git_repo(repo)
            control_repo = self._create_git_source_repo(repo, "control-plane")

            first = self._run(
                repo,
                "client-publish",
                "vibe-coding-client",
                "--target-dir",
                "./fixtures/control-plane",
                "--commit",
                "--format",
                "json",
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            sibling_current = control_repo / "clients" / "vibe-coding-client" / "current"
            sibling_publish = control_repo / "clients" / "vibe-coding-client" / "publish.json"
            sibling_entries = MANAGE_MODULE.directory_file_entries(sibling_current)
            sibling_publish_text = sibling_publish.read_text(encoding="utf-8")

            second = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--format",
                "json",
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            self.assertEqual(MANAGE_MODULE.directory_file_entries(sibling_current), sibling_entries)
            self.assertEqual(sibling_publish.read_text(encoding="utf-8"), sibling_publish_text)

    def test_client_diff_reports_full_addition_when_target_has_no_current_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._init_git_repo(repo)
            self._create_git_source_repo(repo, "control-plane")

            result = self._run(
                repo,
                "client-diff",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["changed"])
            self.assertFalse(payload["current"]["present"])
            self.assertEqual(payload["summary"]["removed"], 0)
            self.assertEqual(payload["summary"]["changed"], 0)
            self.assertEqual(payload["summary"]["added"], payload["candidate"]["file_count"])
            self.assertFalse(payload["publish_metadata"]["matches_candidate"])
            self.assertIn("services", payload["runtime_changes"]["changed_sections"])
            self.assertIn("internal-env-manager", payload["runtime_changes"]["sections"]["services"]["added"])

    def test_client_diff_is_noop_against_matching_publish_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._init_git_repo(repo)
            self._create_git_source_repo(repo, "control-plane")

            publish = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--format",
                "json",
            )
            self.assertEqual(publish.returncode, 0, publish.stderr)

            result = self._run(
                repo,
                "client-diff",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["changed"])
            self.assertTrue(payload["current"]["present"])
            self.assertEqual(payload["summary"]["added"], 0)
            self.assertEqual(payload["summary"]["removed"], 0)
            self.assertEqual(payload["summary"]["changed"], 0)
            self.assertTrue(payload["publish_metadata"]["matches_candidate"])
            self.assertEqual(payload["runtime_changes"]["changed_sections"], [])

    def test_client_diff_reports_runtime_surface_changes_against_existing_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._init_git_repo(repo)
            self._create_git_source_repo(repo, "control-plane")

            publish = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--format",
                "json",
            )
            self.assertEqual(publish.returncode, 0, publish.stderr)

            result = self._run(
                repo,
                "client-diff",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--profile",
                "surfaces",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["changed"])
            self.assertEqual(payload["projection_changes"]["active_profiles"]["added"], ["surfaces"])
            self.assertIn("services", payload["runtime_changes"]["changed_sections"])
            self.assertEqual(
                payload["runtime_changes"]["sections"]["services"]["added"],
                ["api-stub", "web-stub"],
            )
            self.assertEqual(
                payload["runtime_changes"]["sections"]["logs"]["added"],
                ["api", "web"],
            )
            changed_paths = {item["path"] for item in payload["files"]["changed"]}
            self.assertIn("projection.json", changed_paths)
            self.assertIn("runtime-model.json", changed_paths)

    def test_client_open_creates_scoped_surface_with_context_and_mcp_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)

            init = self._run(
                repo,
                "private-init",
                "--path",
                "../private-config",
                "--format",
                "json",
            )
            self.assertEqual(init.returncode, 0, init.stderr)

            result = self._run(
                repo,
                "client-open",
                "personal",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            output_dir = repo / "sand" / "personal"
            self.assertEqual(payload["client_id"], "personal")
            self.assertEqual(payload["output_dir"], str(output_dir.resolve()))
            self.assertEqual(payload["active_profiles"], ["core"])
            self.assertEqual(payload["mcp_servers"], ["skillbox"])
            self.assertTrue((output_dir / "CLAUDE.md").is_file())
            self.assertTrue((output_dir / "AGENTS.md").is_symlink())
            self.assertEqual(os.readlink(str(output_dir / "AGENTS.md")), "CLAUDE.md")
            self.assertTrue((output_dir / ".mcp.json").is_file())
            self.assertTrue((output_dir / "projection.json").is_file())
            self.assertTrue((output_dir / "runtime-model.json").is_file())
            self.assertTrue((output_dir / "workspace" / "clients" / "personal" / "overlay.yaml").is_file())
            self.assertFalse((output_dir / "workspace" / "clients" / "vibe-coding-client").exists())
            self.assertFalse((repo / "home" / ".claude" / "CLAUDE.md").exists())

            mcp_payload = json.loads((output_dir / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(set(mcp_payload["mcpServers"]), {"skillbox"})

            focus_state = json.loads((repo / "workspace" / ".focus.json").read_text(encoding="utf-8"))
            self.assertEqual(focus_state["client_id"], "personal")

    def test_client_open_supports_profiles_and_custom_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)
            self._write_connector_focus_artifacts(repo)
            self.addCleanup(self._run, repo, "down", "--profile", "connectors", "--format", "json")

            init = self._run(
                repo,
                "private-init",
                "--path",
                "../private-config",
                "--format",
                "json",
            )
            self.assertEqual(init.returncode, 0, init.stderr)

            result = self._run(
                repo,
                "client-open",
                "personal",
                "--profile",
                "connectors",
                "--output-dir",
                "./artifacts/open-personal",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            output_dir = repo / "artifacts" / "open-personal"
            self.assertEqual(payload["output_dir"], str(output_dir.resolve()))
            self.assertEqual(payload["active_profiles"], ["connectors", "core"])
            self.assertEqual(set(payload["mcp_servers"]), {"skillbox", "fwc", "dcg"})

            mcp_payload = json.loads((output_dir / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(set(mcp_payload["mcpServers"]), {"skillbox", "fwc", "dcg"})

            runtime_model = json.loads((output_dir / "runtime-model.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime_model["active_profiles"], ["connectors", "core"])
            self.assertEqual(
                {service["id"] for service in runtime_model["services"]},
                {"internal-env-manager", "fwc-mcp", "dcg-mcp"},
            )

    def test_client_open_fails_for_unknown_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "client-open",
                "nonexistent",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("Unknown runtime client", payload["error"]["message"])

    def test_client_open_refuses_to_overwrite_unrelated_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            existing_dir = repo / "artifacts" / "existing-open"
            existing_dir.mkdir(parents=True, exist_ok=True)
            (existing_dir / "note.txt").write_text("keep me\n", encoding="utf-8")

            result = self._run(
                repo,
                "client-open",
                "personal",
                "--output-dir",
                "./artifacts/existing-open",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "conflict")
            self.assertIn("already exists", payload["error"]["message"])

    def test_context_generates_claude_md_and_agents_md_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "context", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            actions = payload["actions"]
            self.assertTrue(any("write-context:" in a for a in actions))

            claude_md = repo / "home" / ".claude" / "CLAUDE.md"
            agents_md = repo / "home" / ".codex" / "AGENTS.md"

            self.assertTrue(claude_md.is_file())
            self.assertTrue(agents_md.is_symlink())
            self.assertEqual(
                os.readlink(str(agents_md)),
                os.path.join("..", ".claude", "CLAUDE.md"),
            )

            content = claude_md.read_text(encoding="utf-8")
            self.assertIn("# skillbox", content)
            self.assertIn("skillbox-self", content)
            self.assertIn("managed-repos", content)
            self.assertIn("internal-env-manager", content)
            self.assertIn("make dev-sanity", content)
            self.assertIn("make runtime-status", content)
            self.assertNotIn("CLIENT=", content)

            agents_content = agents_md.read_text(encoding="utf-8")
            self.assertEqual(content, agents_content)

    def test_context_with_client_includes_client_repos_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "context", "--client", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)

            claude_md = repo / "home" / ".claude" / "CLAUDE.md"
            content = claude_md.read_text(encoding="utf-8")
            self.assertIn("**personal**", content)
            self.assertIn("CLIENT=personal", content)
            self.assertIn("personal-root", content)
            self.assertIn("personal-skills", content)

    def test_context_symlink_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            first = self._run(repo, "context", "--format", "json")
            self.assertEqual(first.returncode, 0, first.stderr)
            first_actions = json.loads(first.stdout)["actions"]
            self.assertTrue(any("symlink-context:" in a for a in first_actions))

            second = self._run(repo, "context", "--format", "json")
            self.assertEqual(second.returncode, 0, second.stderr)
            second_actions = json.loads(second.stdout)["actions"]
            self.assertTrue(any("exists:" in a and "AGENTS.md" in a for a in second_actions))
            self.assertFalse(any("symlink-context:" in a for a in second_actions))

    def test_sync_generates_context_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "sync", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            actions = payload["actions"]
            self.assertTrue(any("write-context:" in a for a in actions))

            claude_md = repo / "home" / ".claude" / "CLAUDE.md"
            agents_md = repo / "home" / ".codex" / "AGENTS.md"

            self.assertTrue(claude_md.is_file())
            self.assertTrue(agents_md.is_symlink())

            content = claude_md.read_text(encoding="utf-8")
            self.assertIn("# skillbox", content)
            self.assertIn("sample-skill", content)

    def test_context_lists_manifest_skill_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "context")

            self.assertEqual(result.returncode, 0, result.stderr)

            claude_md = repo / "home" / ".claude" / "CLAUDE.md"
            content = claude_md.read_text(encoding="utf-8")
            self.assertIn("sample-skill", content)

    def test_context_dry_run_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "context", "--dry-run", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["dry_run"])

            claude_md = repo / "home" / ".claude" / "CLAUDE.md"
            agents_md = repo / "home" / ".codex" / "AGENTS.md"
            self.assertFalse(claude_md.exists())
            self.assertFalse(agents_md.exists())

    def test_context_can_write_into_custom_context_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "context",
                "--client",
                "personal",
                "--context-dir",
                "./sand",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            actions = payload["actions"]
            self.assertTrue(any("write-context: sand/CLAUDE.md" in a for a in actions))

            claude_md = repo / "sand" / "CLAUDE.md"
            agents_md = repo / "sand" / "AGENTS.md"

            self.assertTrue(claude_md.is_file())
            self.assertTrue(agents_md.is_symlink())
            self.assertEqual(os.readlink(str(agents_md)), "CLAUDE.md")
            self.assertFalse((repo / "home" / ".claude" / "CLAUDE.md").exists())
            self.assertFalse((repo / "home" / ".codex" / "AGENTS.md").exists())

            content = claude_md.read_text(encoding="utf-8")
            self.assertIn("CLIENT=personal", content)
            self.assertIn("personal-root", content)

    # -- Structured errors, next_actions, semantic exit codes ------------------

    def test_structured_error_includes_type_message_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "client-init", "Bad Id!", "--format", "json")

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            error = payload["error"]
            self.assertIn("type", error)
            self.assertIn("message", error)
            self.assertIn("recoverable", error)
            self.assertTrue(error["recoverable"])

    def test_doctor_returns_exit_drift_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "doctor", "--client", "vibe-coding-client", "--format", "json")

            self.assertEqual(result.returncode, 2)

    def test_sync_json_includes_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "sync", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("next_actions", payload)
            self.assertIsInstance(payload["next_actions"], list)
            self.assertTrue(len(payload["next_actions"]) > 0)

    def test_status_json_includes_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._run(repo, "sync")

            result = self._run(repo, "status", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("next_actions", payload)
            self.assertIsInstance(payload["next_actions"], list)

    def test_doctor_json_includes_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "doctor", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("next_actions", payload)
            self.assertIn("checks", payload)

    def test_context_json_includes_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "context", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("next_actions", payload)

    def test_client_init_json_includes_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "client-init", "new-project", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("next_actions", payload)
            self.assertTrue(any("sync" in action for action in payload["next_actions"]))

    # -- Onboard macro ---------------------------------------------------------

    def test_onboard_scaffolds_syncs_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            # Create the client root so doctor verify passes
            (repo / "monoserver-host" / "new-project").mkdir(parents=True, exist_ok=True)

            result = self._run(
                repo,
                "onboard",
                "new-project",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["client_id"], "new-project")
            self.assertFalse(payload["dry_run"])
            self.assertIn("steps", payload)
            self.assertIn("next_actions", payload)

            step_names = [s["step"] for s in payload["steps"]]
            self.assertEqual(
                step_names,
                ["scaffold", "sync", "bootstrap", "up", "context", "verify"],
            )
            for s in payload["steps"]:
                self.assertIn(s["status"], ("ok", "skip"), f"step {s['step']} failed: {s}")

            # Verify overlay was created
            overlay = repo / "workspace" / "clients" / "new-project" / "overlay.yaml"
            self.assertTrue(overlay.is_file())

            # Verify context was generated
            claude_md = repo / "home" / ".claude" / "CLAUDE.md"
            self.assertTrue(claude_md.is_file())

    def test_onboard_with_blueprint_runs_full_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            source_repo = self._create_git_source_repo(repo, "fixture-app")
            self._write_client_blueprint(
                repo,
                "git-repo",
                "version: 1\n"
                "description: Clone a repo.\n"
                "variables:\n"
                "  - name: PRIMARY_REPO_URL\n"
                "    required: true\n"
                "  - name: PRIMARY_REPO_PATH\n"
                "    default: ${CLIENT_ROOT}/app\n"
                "client:\n"
                "  default_cwd: ${PRIMARY_REPO_PATH}\n"
                "  repos:\n"
                "    - id: app\n"
                "      kind: repo\n"
                "      path: ${PRIMARY_REPO_PATH}\n"
                "      required: true\n"
                "      profiles:\n"
                "        - core\n"
                "      source:\n"
                "        kind: git\n"
                "        url: ${PRIMARY_REPO_URL}\n"
                "        branch: main\n"
                "      sync:\n"
                "        mode: clone-if-missing\n",
            )
            (repo / "monoserver-host" / "acme-app").mkdir(parents=True, exist_ok=True)

            result = self._run(
                repo,
                "onboard",
                "acme-app",
                "--blueprint",
                "git-repo",
                "--set",
                f"PRIMARY_REPO_URL={source_repo}",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["client_id"], "acme-app")
            step_statuses = {s["step"]: s["status"] for s in payload["steps"]}
            self.assertEqual(step_statuses["scaffold"], "ok")
            self.assertEqual(step_statuses["sync"], "ok")

    def test_onboard_dry_run_does_not_create_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "onboard",
                "dry-test",
                "--dry-run",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["dry_run"])
            # All steps after scaffold should be skipped in dry-run
            step_statuses = {s["step"]: s["status"] for s in payload["steps"]}
            self.assertEqual(step_statuses["scaffold"], "ok")
            for skip_step in ("sync", "bootstrap", "up", "context", "verify"):
                self.assertEqual(step_statuses[skip_step], "skip", f"{skip_step} should be skip")

            overlay = repo / "workspace" / "clients" / "dry-test" / "overlay.yaml"
            self.assertFalse(overlay.exists())

    def test_onboard_fails_with_structured_error_on_invalid_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "onboard",
                "BAD ID",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("error", payload)
            self.assertIn("type", payload["error"])
            self.assertIn("steps", payload)
            self.assertEqual(payload["steps"][0]["step"], "scaffold")
            self.assertEqual(payload["steps"][0]["status"], "fail")

    def _run(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(MANAGER), "--root-dir", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _init_git_repo(self, path: Path) -> None:
        init = subprocess.run(
            ["git", "init"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        branch = subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(branch.returncode, 0, branch.stderr)
        config_email = subprocess.run(
            ["git", "config", "user.email", "tests@example.com"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(config_email.returncode, 0, config_email.stderr)
        config_name = subprocess.run(
            ["git", "config", "user.name", "Runtime Manager Tests"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(config_name.returncode, 0, config_name.stderr)
        add = subprocess.run(
            ["git", "add", "."],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        commit = subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(commit.returncode, 0, commit.stderr)

    def _git_head(self, path: Path) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def _write_client_overlay(
        self,
        repo: Path,
        client_id: str,
        *,
        label: str,
        default_cwd: str,
        root_path: str,
        clients_root: Path | None = None,
        include_context: bool = False,
    ) -> None:
        overlay_parent = clients_root or (repo / "workspace" / "clients")
        overlay_dir = overlay_parent / client_id
        overlay_dir.mkdir(parents=True, exist_ok=True)
        overlay_text = (
            "version: 1\n"
            "client:\n"
            f"  id: {client_id}\n"
            f"  label: {label}\n"
            f"  default_cwd: {default_cwd}\n"
            "  repo_roots:\n"
            f"    - id: {client_id}-root\n"
            "      kind: repo-root\n"
            f"      path: {root_path}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: bind\n"
            "      sync:\n"
            "        mode: external\n"
            "  skills:\n"
            f"    - id: {client_id}-skills\n"
            "      kind: packaged-skill-set\n"
            "      required: false\n"
            "      profiles:\n"
            "        - core\n"
            f"      bundle_dir: ${{SKILLBOX_WORKSPACE_ROOT}}/default-skills/clients/{client_id}\n"
            f"      manifest: ${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skills.manifest\n"
            f"      sources_config: ${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skills.sources.yaml\n"
            f"      lock_path: ${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skills.lock.json\n"
            "      sync:\n"
            "        mode: unpack-bundles\n"
            "      install_targets:\n"
            "        - id: claude\n"
            "          path: ${SKILLBOX_HOME_ROOT}/.claude/skills\n"
            "        - id: codex\n"
            "          path: ${SKILLBOX_HOME_ROOT}/.codex/skills\n"
            "  logs:\n"
            f"    - id: {client_id}\n"
            f"      path: ${{SKILLBOX_LOG_ROOT}}/clients/{client_id}\n"
            "      profiles:\n"
            "        - core\n"
        )
        if include_context:
            overlay_text += (
                "  context:\n"
                "    cwd_match:\n"
                f"      - {default_cwd}\n"
            )
        overlay_text += (
            "  checks:\n"
            f"    - id: {client_id}-root\n"
            "      type: path_exists\n"
            f"      path: {root_path}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
        )
        (overlay_dir / "overlay.yaml").write_text(overlay_text, encoding="utf-8")

    def _write_client_blueprint(self, repo: Path, name: str, content: str) -> None:
        blueprint_dir = repo / "workspace" / "client-blueprints"
        blueprint_dir.mkdir(parents=True, exist_ok=True)
        (blueprint_dir / f"{name}.yaml").write_text(content, encoding="utf-8")

    def _write_env_blueprint(self, repo: Path) -> None:
        self._write_client_blueprint(
            repo,
            "git-repo-http-service-env",
            "version: 1\n"
            "description: Clone a primary repo, hydrate an env file, and wire an HTTP service.\n"
            "variables:\n"
            "  - name: PRIMARY_REPO_ID\n"
            "    default: app\n"
            "  - name: PRIMARY_REPO_URL\n"
            "    required: true\n"
            "  - name: PRIMARY_REPO_BRANCH\n"
            "    default: main\n"
            "  - name: PRIMARY_REPO_PATH\n"
            "    default: ${CLIENT_ROOT}/${PRIMARY_REPO_ID}\n"
            "  - name: SERVICE_ID\n"
            "    default: app-dev\n"
            "  - name: SERVICE_COMMAND\n"
            "    required: true\n"
            "  - name: SERVICE_LOG_ID\n"
            "    default: ${CLIENT_ID}-services\n"
            "  - name: SERVICE_LOG_PATH\n"
            "    default: ${SKILLBOX_LOG_ROOT}/clients/${CLIENT_ID}/services\n"
            "  - name: SERVICE_PORT\n"
            "    default: \"4010\"\n"
            "  - name: ENV_FILE_ID\n"
            "    default: app-env\n"
            "  - name: ENV_FILE_SOURCE_PATH\n"
            "    default: ./workspace/secrets/clients/${CLIENT_ID}/app.env\n"
            "  - name: ENV_FILE_TARGET_PATH\n"
            "    default: ${PRIMARY_REPO_PATH}/.env.local\n"
            "client:\n"
            "  default_cwd: ${PRIMARY_REPO_PATH}\n"
            "  repos:\n"
            "    - id: ${PRIMARY_REPO_ID}\n"
            "      kind: repo\n"
            "      path: ${PRIMARY_REPO_PATH}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: git\n"
            "        url: ${PRIMARY_REPO_URL}\n"
            "        branch: ${PRIMARY_REPO_BRANCH}\n"
            "      sync:\n"
            "        mode: clone-if-missing\n"
            "  env_files:\n"
            "    - id: ${ENV_FILE_ID}\n"
            "      kind: dotenv\n"
            "      repo: ${PRIMARY_REPO_ID}\n"
            "      path: ${ENV_FILE_TARGET_PATH}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: file\n"
            "        path: ${ENV_FILE_SOURCE_PATH}\n"
            "      sync:\n"
            "        mode: write\n"
            "  logs:\n"
            "    - id: ${SERVICE_LOG_ID}\n"
            "      path: ${SERVICE_LOG_PATH}\n"
            "      profiles:\n"
            "        - core\n"
            "  services:\n"
            "    - id: ${SERVICE_ID}\n"
            "      kind: http\n"
            "      repo: ${PRIMARY_REPO_ID}\n"
            "      profiles:\n"
            "        - core\n"
            "      command: ${SERVICE_COMMAND}\n"
            "      log: ${SERVICE_LOG_ID}\n"
            "      healthcheck:\n"
            "        type: http\n"
            "        url: http://127.0.0.1:${SERVICE_PORT}/health\n"
            "        timeout_seconds: 0.5\n",
        )

    def _write_env_source(self, repo: Path, client_id: str, content: str) -> None:
        source_path = repo / "workspace" / "secrets" / "clients" / client_id / "app.env"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(content, encoding="utf-8")

    def _write_fixture(self, repo: Path, include_bundle: bool = True) -> None:
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
            "SKILLBOX_MONOSERVER_HOST_ROOT=./monoserver-host\n"
            "SKILLBOX_API_PORT=8000\n"
            "SKILLBOX_WEB_PORT=3000\n"
            "SKILLBOX_SWIMMERS_PORT=3210\n"
            "SKILLBOX_SWIMMERS_PUBLISH_HOST=127.0.0.1\n"
            "SKILLBOX_SWIMMERS_REPO=/monoserver/swimmers\n"
            "SKILLBOX_SWIMMERS_INSTALL_DIR=/home/sandbox/.local/bin\n"
            "SKILLBOX_SWIMMERS_BIN=/home/sandbox/.local/bin/swimmers\n"
            "SKILLBOX_SWIMMERS_DOWNLOAD_URL=\n"
            "SKILLBOX_SWIMMERS_DOWNLOAD_SHA256=\n"
            "SKILLBOX_SWIMMERS_AUTH_MODE=\n"
            "SKILLBOX_SWIMMERS_AUTH_TOKEN=\n"
            "SKILLBOX_SWIMMERS_OBSERVER_TOKEN=\n"
            "SKILLBOX_DCG_BIN=/home/sandbox/.local/bin/dcg\n"
            "SKILLBOX_DCG_DOWNLOAD_URL=\n"
            "SKILLBOX_DCG_DOWNLOAD_SHA256=\n"
            "SKILLBOX_DCG_PACKS=core.git,core.filesystem\n"
            "SKILLBOX_DCG_MCP_PORT=3220\n"
            "SKILLBOX_FWC_BIN=/home/sandbox/.local/bin/fwc\n"
            "SKILLBOX_FWC_DOWNLOAD_URL=\n"
            "SKILLBOX_FWC_DOWNLOAD_SHA256=\n"
            "SKILLBOX_FWC_MCP_PORT=3221\n"
            "SKILLBOX_FWC_ZONE=work\n"
            "SKILLBOX_FWC_CONNECTORS=github,slack\n"
            "SKILLBOX_PULSE_INTERVAL=30\n",
            encoding="utf-8",
        )

        (repo / "workspace" / "runtime.yaml").parent.mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "runtime.yaml").write_text(
            "version: 2\n"
            "selection: {}\n"
            "core:\n"
            "  repos:\n"
            "    - id: skillbox-self\n"
            "      kind: repo\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: bind\n"
            "        path: ${ROOT_DIR}\n"
            "      sync:\n"
            "        mode: external\n"
            "    - id: managed-repos\n"
            "      kind: workspace-root\n"
            "      path: ${SKILLBOX_REPOS_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: directory\n"
            "      sync:\n"
            "        mode: ensure-directory\n"
            "  artifacts:\n"
            "    - id: swimmers-bin\n"
            "      kind: binary\n"
            "      path: ${SKILLBOX_SWIMMERS_BIN}\n"
            "      required: false\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: file\n"
            "        path: ./artifacts/swimmers.bin\n"
            "        executable: true\n"
            "      sync:\n"
            "        mode: copy-if-missing\n"
            "  skills:\n"
            "    - id: default-skills\n"
            "      kind: packaged-skill-set\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      bundle_dir: ${SKILLBOX_WORKSPACE_ROOT}/default-skills\n"
            "      manifest: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.manifest\n"
            "      sources_config: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.sources.yaml\n"
            "      lock_path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.lock.json\n"
            "      sync:\n"
            "        mode: unpack-bundles\n"
            "      install_targets:\n"
            "        - id: claude\n"
            "          path: ${SKILLBOX_HOME_ROOT}/.claude/skills\n"
            "        - id: codex\n"
            "          path: ${SKILLBOX_HOME_ROOT}/.codex/skills\n"
            "  services:\n"
            "    - id: internal-env-manager\n"
            "      kind: orchestration\n"
            "      repo: skillbox-self\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}/.env-manager\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      command: python3 .env-manager/manage.py\n"
            "      log: runtime\n"
            "    - id: api-stub\n"
            "      kind: http\n"
            "      repo: skillbox-self\n"
            "      required: false\n"
            "      profiles:\n"
            "        - surfaces\n"
            "      command: python3 -m http.server 8000\n"
            "      healthcheck:\n"
            "        type: path_exists\n"
            "        path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "      log: api\n"
            "    - id: web-stub\n"
            "      kind: http\n"
            "      repo: skillbox-self\n"
            "      required: false\n"
            "      profiles:\n"
            "        - surfaces\n"
            "      command: python3 -m http.server 3000\n"
            "      healthcheck:\n"
            "        type: path_exists\n"
            "        path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "      log: web\n"
            "    - id: swimmers-server\n"
            "      kind: tmux-api\n"
            "      artifact: swimmers-bin\n"
            "      required: false\n"
            "      profiles:\n"
            "        - swimmers\n"
            "      command: /workspace/scripts/05-swimmers.sh --inside start\n"
            "      healthcheck:\n"
            "        type: path_exists\n"
            "        path: ${SKILLBOX_LOG_ROOT}/swimmers/swimmers-server.pid\n"
            "      log: swimmers\n"
            "  logs:\n"
            "    - id: runtime\n"
            "      path: ${SKILLBOX_LOG_ROOT}/runtime\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: repos\n"
            "      path: ${SKILLBOX_LOG_ROOT}/repos\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: api\n"
            "      path: ${SKILLBOX_LOG_ROOT}/api\n"
            "      profiles:\n"
            "        - surfaces\n"
            "    - id: web\n"
            "      path: ${SKILLBOX_LOG_ROOT}/web\n"
            "      profiles:\n"
            "        - surfaces\n"
            "    - id: swimmers\n"
            "      path: ${SKILLBOX_LOG_ROOT}/swimmers\n"
            "      profiles:\n"
            "        - swimmers\n"
            "  checks:\n"
            "    - id: workspace-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: repos-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_REPOS_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: skills-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_SKILLS_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: log-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_LOG_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: monoserver-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_MONOSERVER_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: runtime-manager\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}/.env-manager/manage.py\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "connectors:\n"
            "  repos:\n"
            "    - id: flywheel-connectors\n"
            "      kind: repo\n"
            "      path: ${SKILLBOX_REPOS_ROOT}/flywheel_connectors\n"
            "      required: false\n"
            "      source:\n"
            "        kind: directory\n"
            "      sync:\n"
            "        mode: ensure-directory\n"
            "  artifacts:\n"
            "    - id: fwc-bin\n"
            "      kind: binary\n"
            "      path: ${SKILLBOX_FWC_BIN}\n"
            "      required: false\n"
            "      source:\n"
            "        kind: file\n"
            "        path: ./artifacts/fwc.bin\n"
            "        executable: true\n"
            "      sync:\n"
            "        mode: copy-if-missing\n"
            "    - id: dcg-bin\n"
            "      kind: binary\n"
            "      path: ${SKILLBOX_DCG_BIN}\n"
            "      required: false\n"
            "      source:\n"
            "        kind: file\n"
            "        path: ./artifacts/dcg.bin\n"
            "        executable: true\n"
            "      sync:\n"
            "        mode: copy-if-missing\n"
            "  services:\n"
            "    - id: fwc-mcp\n"
            "      kind: mcp\n"
            "      artifact: fwc-bin\n"
            "      required: false\n"
            "      command: ${SKILLBOX_FWC_BIN} serve-mcp --zone ${SKILLBOX_FWC_ZONE} --connectors ${SKILLBOX_FWC_CONNECTORS}\n"
            "      healthcheck:\n"
            "        type: process_running\n"
            "        pattern: fwc serve-mcp\n"
            "      log: connectors\n"
            "    - id: dcg-mcp\n"
            "      kind: mcp\n"
            "      artifact: dcg-bin\n"
            "      required: false\n"
            "      command: ${SKILLBOX_DCG_BIN} mcp\n"
            "      healthcheck:\n"
            "        type: process_running\n"
            "        pattern: dcg mcp\n"
            "      log: connectors\n"
            "  logs:\n"
            "    - id: connectors\n"
            "      path: ${SKILLBOX_LOG_ROOT}/connectors\n"
            "  checks:\n"
            "    - id: fwc-binary\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_FWC_BIN}\n"
            "      required: false\n"
            "    - id: dcg-binary\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_DCG_BIN}\n"
            "      required: false\n",
            encoding="utf-8",
        )
        (repo / "artifacts").mkdir(parents=True, exist_ok=True)
        (repo / "artifacts" / "swimmers.bin").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (repo / "artifacts" / "fwc.bin").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (repo / "artifacts" / "dcg.bin").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

        (repo / "workspace" / "default-skills.manifest").write_text("sample-skill\n", encoding="utf-8")
        (repo / "workspace" / "default-skills.sources.yaml").write_text(
            "version: 1\n"
            "sources:\n"
            "  - kind: local\n"
            "    path: ./skills\n",
            encoding="utf-8",
        )
        (repo / "workspace" / "clients" / "personal").mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "clients" / "personal" / "skills.manifest").write_text(
            "personal-skill\n",
            encoding="utf-8",
        )
        (repo / "workspace" / "clients" / "personal" / "skills.sources.yaml").write_text(
            "version: 1\n"
            "sources:\n"
            "  - kind: local\n"
            "    path: ./skills/clients/personal\n",
            encoding="utf-8",
        )
        (repo / "workspace" / "clients" / "vibe-coding-client").mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "clients" / "vibe-coding-client" / "skills.manifest").write_text("", encoding="utf-8")
        (repo / "workspace" / "clients" / "vibe-coding-client" / "skills.sources.yaml").write_text(
            "version: 1\n"
            "sources:\n"
            "  - kind: local\n"
            "    path: ./skills/clients/vibe-coding-client\n",
            encoding="utf-8",
        )
        self._write_client_overlay(
            repo,
            "personal",
            label="Personal",
            default_cwd="${SKILLBOX_MONOSERVER_ROOT}",
            root_path="${SKILLBOX_MONOSERVER_ROOT}",
        )
        self._write_client_overlay(
            repo,
            "vibe-coding-client",
            label="Vibe Coding Client",
            default_cwd="${SKILLBOX_MONOSERVER_ROOT}/vibe-coding-client",
            root_path="${SKILLBOX_MONOSERVER_ROOT}/vibe-coding-client",
        )

        (repo / "default-skills").mkdir(parents=True, exist_ok=True)
        (repo / "default-skills" / "clients" / "personal").mkdir(parents=True, exist_ok=True)
        (repo / "default-skills" / "clients" / "vibe-coding-client").mkdir(parents=True, exist_ok=True)
        if include_bundle:
            self._write_skill_bundle(repo / "default-skills" / "sample-skill.skill", "sample-skill")
            self._write_skill_bundle(
                repo / "default-skills" / "clients" / "personal" / "personal-skill.skill",
                "personal-skill",
            )

        (repo / "skills").mkdir(parents=True, exist_ok=True)
        (repo / "skills" / "clients" / "personal").mkdir(parents=True, exist_ok=True)
        (repo / "skills" / "clients" / "vibe-coding-client").mkdir(parents=True, exist_ok=True)
        (repo / "logs").mkdir(parents=True, exist_ok=True)
        (repo / "repos").mkdir(parents=True, exist_ok=True)
        (repo / "home" / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / "home" / ".codex").mkdir(parents=True, exist_ok=True)
        (repo / "monoserver-host").mkdir(parents=True, exist_ok=True)
        (repo / ".env-manager").mkdir(parents=True, exist_ok=True)
        (repo / ".env-manager" / "manage.py").write_text(
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "\n"
            "import subprocess\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            f"REAL_MANAGE = {str(MANAGER)!r}\n"
            "ROOT_DIR = Path(__file__).resolve().parent.parent\n"
            "cmd = [sys.executable, REAL_MANAGE, '--root-dir', str(ROOT_DIR), *sys.argv[1:]]\n"
            "raise SystemExit(subprocess.run(cmd, check=False).returncode)\n",
            encoding="utf-8",
        )
        (repo / ".env-manager" / "mcp_server.py").write_text(
            (ROOT_DIR / ".env-manager" / "mcp_server.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (repo / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "skillbox": {
                            "command": "python3",
                            "args": ["./.env-manager/mcp_server.py"],
                        },
                        "fwc": {
                            "command": str((repo / ".mcp-bin" / "fwc").resolve()),
                            "args": ["serve-mcp", "--zone", "work"],
                        },
                        "dcg": {
                            "command": str((repo / ".mcp-bin" / "dcg").resolve()),
                            "args": ["mcp"],
                        },
                    },
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    def _write_connector_focus_artifacts(self, repo: Path) -> None:
        for name in ("fwc.bin", "dcg.bin"):
            (repo / "artifacts" / name).write_text(
                "#!/bin/sh\n"
                "trap 'exit 0' TERM INT\n"
                "while true; do\n"
                "  sleep 1\n"
                "done\n",
                encoding="utf-8",
            )

    def _fixture_mcp_stub_path(self, repo: Path, name: str) -> Path:
        return repo / ".mcp-bin" / name

    def _install_absolute_mcp_stub(
        self,
        path: Path,
        *,
        tool_names: list[str],
        exit_before_tools: bool = False,
    ) -> None:
        original_bytes = path.read_bytes() if path.exists() else None
        original_mode = path.stat().st_mode if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "\n"
            "import json\n"
            "import sys\n"
            "\n"
            f"TOOL_NAMES = {tool_names!r}\n"
            f"EXIT_BEFORE_TOOLS = {exit_before_tools!r}\n"
            "\n"
            "for raw in sys.stdin:\n"
            "    raw = raw.strip()\n"
            "    if not raw:\n"
            "        continue\n"
            "    message = json.loads(raw)\n"
            "    method = message.get('method')\n"
            "    if method == 'initialize':\n"
            "        response = {\n"
            "            'jsonrpc': '2.0',\n"
            "            'id': message.get('id'),\n"
            "            'result': {\n"
            "                'protocolVersion': '2024-11-05',\n"
            "                'capabilities': {'tools': {'listChanged': False}},\n"
            "                'serverInfo': {'name': 'fixture', 'version': '1.0.0'},\n"
            "            },\n"
            "        }\n"
            "        sys.stdout.write(json.dumps(response) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "        if EXIT_BEFORE_TOOLS:\n"
            "            raise SystemExit(0)\n"
            "    elif method == 'tools/list':\n"
            "        response = {\n"
            "            'jsonrpc': '2.0',\n"
            "            'id': message.get('id'),\n"
            "            'result': {\n"
            "                'tools': [\n"
            "                    {'name': name, 'description': name, 'inputSchema': {'type': 'object'}}\n"
            "                    for name in TOOL_NAMES\n"
            "                ]\n"
            "            },\n"
            "        }\n"
            "        sys.stdout.write(json.dumps(response) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif message.get('id') is not None:\n"
            "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {}}) + '\\n')\n"
            "        sys.stdout.flush()\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

        def _restore() -> None:
            if original_bytes is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                return
            path.write_bytes(original_bytes)
            if original_mode is not None:
                path.chmod(original_mode)

        self.addCleanup(_restore)

    def _create_git_source_repo(self, repo: Path, name: str) -> Path:
        source_repo = repo / "fixtures" / name
        source_repo.mkdir(parents=True, exist_ok=True)
        (source_repo / "README.md").write_text("# Fixture Repo\n", encoding="utf-8")
        self._init_git_repo(source_repo)
        return source_repo

    def _install_fixture_daemon(self, repo: Path) -> None:
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "fixture_daemon.py").write_text(
            "from __future__ import annotations\n"
            "\n"
            "import signal\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            "\n"
            "ready_path = Path(sys.argv[1])\n"
            "label = sys.argv[2] if len(sys.argv) > 2 else ready_path.stem\n"
            "ready_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "\n"
            "def shutdown(*_args: object) -> None:\n"
            "    try:\n"
            "        ready_path.unlink()\n"
            "    except FileNotFoundError:\n"
            "        pass\n"
            "    raise SystemExit(0)\n"
            "\n"
            "signal.signal(signal.SIGTERM, shutdown)\n"
            "signal.signal(signal.SIGINT, shutdown)\n"
            "ready_path.write_text('ok\\n', encoding='utf-8')\n"
            "print(f'{label} ready', flush=True)\n"
            "while True:\n"
            "    time.sleep(0.2)\n",
            encoding="utf-8",
        )

        runtime_path = repo / "workspace" / "runtime.yaml"
        runtime_path.write_text(
            runtime_path.read_text(encoding="utf-8").replace(
                "  logs:\n",
                "    - id: fixture-daemon\n"
                "      kind: daemon\n"
                "      repo: skillbox-self\n"
                "      required: false\n"
                "      profiles:\n"
                "        - core\n"
                "      command: python3 scripts/fixture_daemon.py ${SKILLBOX_LOG_ROOT}/runtime/fixture-daemon.ready fixture-daemon\n"
                "      healthcheck:\n"
                "        type: path_exists\n"
                "        path: ${SKILLBOX_LOG_ROOT}/runtime/fixture-daemon.ready\n"
                "      log: runtime\n"
                "  logs:\n",
                1,
            ),
            encoding="utf-8",
        )

    def _install_fixture_dependency_pair(self, repo: Path) -> None:
        self._install_fixture_daemon(repo)

        runtime_path = repo / "workspace" / "runtime.yaml"
        runtime_path.write_text(
            runtime_path.read_text(encoding="utf-8").replace(
                "  logs:\n",
                "    - id: fixture-worker\n"
                "      kind: daemon\n"
                "      repo: skillbox-self\n"
                "      required: false\n"
                "      profiles:\n"
                "        - core\n"
                "      depends_on:\n"
                "        - fixture-daemon\n"
                "      command: python3 scripts/fixture_daemon.py ${SKILLBOX_LOG_ROOT}/runtime/fixture-worker.ready fixture-worker\n"
                "      healthcheck:\n"
                "        type: path_exists\n"
                "        path: ${SKILLBOX_LOG_ROOT}/runtime/fixture-worker.ready\n"
                "      log: runtime\n"
                "  logs:\n",
                1,
            ),
            encoding="utf-8",
        )

    def _install_fixture_bootstrap_graph(self, repo: Path) -> None:
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "fixture_bootstrap.py").write_text(
            "from __future__ import annotations\n"
            "\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "target_path = Path(sys.argv[1])\n"
            "order_path = Path(sys.argv[2])\n"
            "label = sys.argv[3]\n"
            "target_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "order_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "with order_path.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(f'{label}\\n')\n"
            "target_path.write_text('ok\\n', encoding='utf-8')\n"
            "print(f'{label} complete', flush=True)\n",
            encoding="utf-8",
        )

        runtime_path = repo / "workspace" / "runtime.yaml"
        runtime_path.write_text(
            runtime_path.read_text(encoding="utf-8").replace(
                "  services:\n",
                "  tasks:\n"
                "    - id: prepare-assets\n"
                "      kind: bootstrap\n"
                "      repo: skillbox-self\n"
                "      required: false\n"
                "      profiles:\n"
                "        - core\n"
                "      command: python3 scripts/fixture_bootstrap.py ${SKILLBOX_LOG_ROOT}/runtime/prepare-assets.ok ${SKILLBOX_LOG_ROOT}/runtime/bootstrap-order.log prepare-assets\n"
                "      outputs:\n"
                "        - ${SKILLBOX_LOG_ROOT}/runtime/prepare-assets.ok\n"
                "      success:\n"
                "        type: path_exists\n"
                "        path: ${SKILLBOX_LOG_ROOT}/runtime/prepare-assets.ok\n"
                "      log: runtime\n"
                "    - id: build-app\n"
                "      kind: bootstrap\n"
                "      repo: skillbox-self\n"
                "      required: false\n"
                "      profiles:\n"
                "        - core\n"
                "      depends_on:\n"
                "        - prepare-assets\n"
                "      command: python3 scripts/fixture_bootstrap.py ${SKILLBOX_LOG_ROOT}/runtime/build-app.ok ${SKILLBOX_LOG_ROOT}/runtime/bootstrap-order.log build-app\n"
                "      inputs:\n"
                "        - ${SKILLBOX_LOG_ROOT}/runtime/prepare-assets.ok\n"
                "      outputs:\n"
                "        - ${SKILLBOX_LOG_ROOT}/runtime/build-app.ok\n"
                "      success:\n"
                "        type: path_exists\n"
                "        path: ${SKILLBOX_LOG_ROOT}/runtime/build-app.ok\n"
                "      log: runtime\n"
                "  services:\n",
                1,
            ),
            encoding="utf-8",
        )

    def _install_fixture_bootstrap_service(self, repo: Path) -> None:
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "fixture_guarded_daemon.py").write_text(
            "from __future__ import annotations\n"
            "\n"
            "import signal\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            "\n"
            "required_path = Path(sys.argv[1])\n"
            "ready_path = Path(sys.argv[2])\n"
            "label = sys.argv[3]\n"
            "if not required_path.is_file():\n"
            "    print(f'missing required path: {required_path}', flush=True)\n"
            "    raise SystemExit(1)\n"
            "ready_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "\n"
            "def shutdown(*_args: object) -> None:\n"
            "    try:\n"
            "        ready_path.unlink()\n"
            "    except FileNotFoundError:\n"
            "        pass\n"
            "    raise SystemExit(0)\n"
            "\n"
            "signal.signal(signal.SIGTERM, shutdown)\n"
            "signal.signal(signal.SIGINT, shutdown)\n"
            "ready_path.write_text('ok\\n', encoding='utf-8')\n"
            "print(f'{label} ready', flush=True)\n"
            "while True:\n"
            "    time.sleep(0.2)\n",
            encoding="utf-8",
        )

        runtime_path = repo / "workspace" / "runtime.yaml"
        runtime_path.write_text(
            runtime_path.read_text(encoding="utf-8").replace(
                "  logs:\n",
                "    - id: bootstrap-daemon\n"
                "      kind: daemon\n"
                "      repo: skillbox-self\n"
                "      required: false\n"
                "      profiles:\n"
                "        - core\n"
                "      bootstrap_tasks:\n"
                "        - build-app\n"
                "      command: python3 scripts/fixture_guarded_daemon.py ${SKILLBOX_LOG_ROOT}/runtime/build-app.ok ${SKILLBOX_LOG_ROOT}/runtime/bootstrap-daemon.ready bootstrap-daemon\n"
                "      healthcheck:\n"
                "        type: path_exists\n"
                "        path: ${SKILLBOX_LOG_ROOT}/runtime/bootstrap-daemon.ready\n"
                "      log: runtime\n"
                "  logs:\n",
                1,
            ),
            encoding="utf-8",
        )

    def _write_skill_bundle(self, bundle_path: Path, skill_name: str) -> None:
        with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                f"{skill_name}/SKILL.md",
                "---\n"
                f"name: {skill_name}\n"
                f"description: Fixture skill {skill_name} for runtime manager tests.\n"
                "---\n\n"
                "# Sample Skill\n",
            )
            archive.writestr(
                f"{skill_name}/references/overview.md",
                "fixture reference\n",
            )


    # ------------------------------------------------------------------
    # focus command tests
    # ------------------------------------------------------------------

    def test_focus_activates_client_and_generates_live_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            # First sync so directories exist
            self._run(repo, "sync", "--client", "personal")

            result = self._run(repo, "focus", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["client_id"], "personal")

            # Steps should all be ok or skip
            for s in payload["steps"]:
                self.assertIn(s["status"], ("ok", "skip"), f"step {s['step']} failed")
            self.assertEqual(
                [step["step"] for step in payload["steps"]],
                ["compose-override", "sync", "bootstrap", "up", "collect", "skill-context", "context", "persist"],
            )

            # Live state should have repos, services, checks, logs
            live = payload["live_state"]
            self.assertIn("repos", live)
            self.assertIn("services", live)
            self.assertIn("checks", live)
            self.assertIn("logs", live)
            self.assertIn("collected_at", live)

            # Summary should have expected keys
            summary = payload["summary"]
            self.assertIn("repos_present", summary)
            self.assertIn("services_running", summary)
            self.assertIn("checks_passing", summary)

            # CLAUDE.md should have been written with live sections
            claude_md = (repo / "home" / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("## Live Status", claude_md)
            # Repo State only appears when git repos are detected (tmpdir isn't a git repo)
            self.assertIn("personal", claude_md)

            # .focus.json should have been written
            focus_path = repo / "workspace" / ".focus.json"
            self.assertTrue(focus_path.is_file())
            focus_data = json.loads(focus_path.read_text(encoding="utf-8"))
            self.assertEqual(focus_data["client_id"], "personal")
            self.assertEqual(focus_data["version"], 1)
            self.assertEqual(focus_data["active_profiles"], ["core"])

    def test_focus_resume_reuses_previous_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            # Initial focus
            self._run(repo, "sync", "--client", "personal")
            self._run(repo, "focus", "personal", "--format", "json")

            # Resume
            result = self._run(repo, "focus", "--resume", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["client_id"], "personal")

    def test_focus_can_write_live_context_into_custom_context_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "focus",
                "personal",
                "--context-dir",
                "./sand",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["client_id"], "personal")

            claude_md = (repo / "sand" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("## Live Status", claude_md)
            self.assertIn("personal", claude_md)

            agents_md = repo / "sand" / "AGENTS.md"
            self.assertTrue(agents_md.is_symlink())
            self.assertEqual(os.readlink(str(agents_md)), "CLAUDE.md")
            self.assertFalse((repo / "home" / ".claude" / "CLAUDE.md").exists())

    def test_focus_writes_skill_context_to_configured_clients_host_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            external_clients = repo / "private-config" / "clients"
            external_clients.mkdir(parents=True, exist_ok=True)
            (repo / ".env").write_text(
                "SKILLBOX_CLIENTS_HOST_ROOT=./private-config/clients\n",
                encoding="utf-8",
            )
            self._write_client_overlay(
                repo,
                "personal",
                label="Personal",
                default_cwd="${SKILLBOX_MONOSERVER_ROOT}",
                root_path="${SKILLBOX_MONOSERVER_ROOT}",
                clients_root=external_clients,
                include_context=True,
            )
            (external_clients / "personal" / "skills.manifest").write_text("", encoding="utf-8")
            (external_clients / "personal" / "skills.sources.yaml").write_text(
                "version: 1\n"
                "sources:\n"
                "  - kind: local\n"
                "    path: ./skills/clients/personal\n",
                encoding="utf-8",
            )

            result = self._run(repo, "focus", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((external_clients / "personal" / "context.yaml").is_file())
            focus_state = json.loads((repo / "workspace" / ".focus.json").read_text(encoding="utf-8"))
            self.assertEqual(
                focus_state["skill_context_path"],
                "/workspace/workspace/clients/personal/context.yaml",
            )

    def test_focus_resume_fails_without_focus_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "focus", "--resume", "--format", "json")

            self.assertNotEqual(result.returncode, 0)

    def test_focus_fails_for_unknown_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "focus", "nonexistent", "--format", "json")

            self.assertNotEqual(result.returncode, 0)

    def test_focus_without_client_id_or_resume_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "focus", "--format", "json")

            self.assertNotEqual(result.returncode, 0)

    def test_focus_live_context_includes_attention_for_failing_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            # Remove monoserver-host so the personal-root check fails
            import shutil
            monoserver = repo / "monoserver-host"
            if monoserver.exists():
                shutil.rmtree(monoserver)

            result = self._run(repo, "focus", "personal", "--format", "json")

            # Should still complete (checks fail but focus continues)
            payload = json.loads(result.stdout)
            claude_md = (repo / "home" / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("## Attention", claude_md)
            self.assertIn("CHECK FAIL", claude_md)

    def test_focus_detects_recent_errors_in_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._run(repo, "sync", "--client", "personal")

            # Write a fake log file with errors
            log_dir = repo / "logs" / "runtime"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "test.log").write_text(
                "INFO: starting up\n"
                "ERROR: connection refused on port 5432\n"
                "INFO: retrying\n"
                "FATAL: could not connect to database\n",
                encoding="utf-8",
            )

            result = self._run(repo, "focus", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)

            # Check that recent_errors were collected
            logs_with_errors = [
                lg for lg in payload["live_state"]["logs"]
                if lg.get("recent_errors")
            ]
            self.assertTrue(len(logs_with_errors) > 0)

            # Check that Attention section was generated
            claude_md = (repo / "home" / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("RECENT ERRORS", claude_md)

    def test_focus_supports_profiles_and_persists_active_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_connector_focus_artifacts(repo)
            self.addCleanup(self._run, repo, "down", "--profile", "connectors", "--format", "json")

            result = self._run(repo, "focus", "personal", "--profile", "connectors", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            focus_state = json.loads((repo / "workspace" / ".focus.json").read_text(encoding="utf-8"))
            self.assertEqual(focus_state["client_id"], "personal")
            self.assertEqual(focus_state["active_profiles"], ["connectors", "core"])
            self.assertEqual(
                {service["id"] for service in payload["live_state"]["services"]},
                {"internal-env-manager", "fwc-mcp", "dcg-mcp"},
            )

    def test_acceptance_succeeds_for_onboarded_core_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "acceptance", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ready"])
            self.assertEqual(
                [step["step"] for step in payload["steps"]],
                ["doctor-pre", "sync", "focus", "mcp-smoke", "doctor-post"],
            )
            self.assertEqual(payload["active_profiles"], ["core"])
            tool_names = payload["steps"][3]["detail"]["servers"]["skillbox"]["tool_names"]
            self.assertIn("skillbox_status", tool_names)
            self.assertIn("skillbox_focus", tool_names)

    def test_acceptance_succeeds_only_when_connector_mcp_surfaces_are_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_connector_focus_artifacts(repo)
            self._install_absolute_mcp_stub(self._fixture_mcp_stub_path(repo, "fwc"), tool_names=["fwc_ping"])
            self._install_absolute_mcp_stub(self._fixture_mcp_stub_path(repo, "dcg"), tool_names=["dcg_ping"])
            self.addCleanup(self._run, repo, "down", "--profile", "connectors", "--format", "json")

            result = self._run(repo, "acceptance", "personal", "--profile", "connectors", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["active_profiles"], ["connectors", "core"])
            self.assertEqual(payload["steps"][3]["detail"]["servers_ok"], ["skillbox", "fwc", "dcg"])
            self.assertEqual(
                set(payload["steps"][2]["detail"]["services"]),
                {"internal-env-manager", "fwc-mcp", "dcg-mcp"},
            )

    def test_acceptance_fails_before_mutation_when_client_is_not_onboarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "acceptance", "missing-client", "--format", "json")

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ready"])
            self.assertEqual(payload["error"]["type"], "client_not_onboarded")
            self.assertIn("onboard missing-client", payload["error"]["message"])
            self.assertFalse((repo / "workspace" / ".focus.json").exists())

    def test_acceptance_fails_when_mcp_smoke_fails_after_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_connector_focus_artifacts(repo)
            self._install_absolute_mcp_stub(
                self._fixture_mcp_stub_path(repo, "fwc"),
                tool_names=["fwc_ping"],
                exit_before_tools=True,
            )
            self._install_absolute_mcp_stub(self._fixture_mcp_stub_path(repo, "dcg"), tool_names=["dcg_ping"])
            self.addCleanup(self._run, repo, "down", "--profile", "connectors", "--format", "json")

            result = self._run(repo, "acceptance", "personal", "--profile", "connectors", "--format", "json")

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            steps = {step["step"]: step for step in payload["steps"]}
            self.assertEqual(steps["focus"]["status"], "ok")
            self.assertEqual(steps["mcp-smoke"]["status"], "fail")
            self.assertFalse(payload["ready"])
            self.assertEqual(payload["error"]["type"], "mcp_smoke_failed")
            self.assertIn("sync --profile connectors --format json", payload["next_actions"])
            self.assertIn("logs --service fwc-mcp --format json", payload["next_actions"])

    def test_doctor_fails_when_url_artifact_lacks_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            runtime_path = repo / "workspace" / "runtime.yaml"
            runtime_text = runtime_path.read_text(encoding="utf-8")
            runtime_text = runtime_text.replace(
                "      source:\n"
                "        kind: file\n"
                "        path: ./artifacts/swimmers.bin\n"
                "        executable: true\n"
                "      sync:\n"
                "        mode: copy-if-missing\n",
                "      source:\n"
                "        kind: url\n"
                "        url: https://example.com/swimmers\n"
                "        executable: true\n"
                "      sync:\n"
                "        mode: download-if-missing\n",
            )
            runtime_path.write_text(runtime_text, encoding="utf-8")

            result = self._run(repo, "doctor", "--format", "json")

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            issues = [
                issue
                for check in payload["checks"]
                if check["code"] == "runtime-manifest"
                for issue in check.get("details", {}).get("issues", [])
            ]
            self.assertTrue(any("source.sha256" in issue for issue in issues), issues)

    def test_sync_artifact_download_verifies_sha256_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "bin" / "tool"
            payload = b"#!/bin/sh\necho ok\n"
            expected_sha256 = hashlib.sha256(payload).hexdigest()
            artifact = {
                "id": "fixture-bin",
                "host_path": str(target),
                "source": {
                    "kind": "url",
                    "url": "https://example.com/tool",
                    "sha256": expected_sha256,
                    "executable": True,
                },
                "sync": {"mode": "download-if-missing"},
            }

            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = payload

            with mock.patch.object(MANAGE_MODULE.urllib.request, "urlopen", return_value=response) as urlopen:
                actions = MANAGE_MODULE.sync_artifact(artifact, dry_run=False)

            urlopen.assert_called_once()
            self.assertEqual(actions, [f"download-if-missing: https://example.com/tool -> {target}"])
            self.assertEqual(target.read_bytes(), payload)
            self.assertTrue(target.stat().st_mode & 0o111)

    def test_sync_artifact_rejects_non_https_or_mismatched_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "bin" / "tool"
            secure_artifact = {
                "id": "fixture-bin",
                "host_path": str(target),
                "source": {
                    "kind": "url",
                    "url": "https://example.com/tool",
                    "sha256": "0" * 64,
                    "executable": True,
                },
                "sync": {"mode": "download-if-missing"},
            }
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b"#!/bin/sh\necho nope\n"

            with mock.patch.object(MANAGE_MODULE.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                    MANAGE_MODULE.sync_artifact(secure_artifact, dry_run=False)

            insecure_artifact = {
                "id": "fixture-bin",
                "host_path": str(target),
                "source": {
                    "kind": "url",
                    "url": "http://example.com/tool",
                    "sha256": "0" * 64,
                    "executable": True,
                },
                "sync": {"mode": "download-if-missing"},
            }

            with mock.patch.object(MANAGE_MODULE.urllib.request, "urlopen") as urlopen:
                with self.assertRaisesRegex(RuntimeError, "must use https"):
                    MANAGE_MODULE.sync_artifact(insecure_artifact, dry_run=False)
            urlopen.assert_not_called()
            self.assertFalse(target.exists())


    # --- ack tests ---

    def test_ack_writes_to_ack_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._run(repo, "sync", "--client", "personal")

            # Emit a journal event via focus
            self._run(repo, "focus", "personal")

            # Ack all events
            result = self._run(repo, "ack", "--all", "--reason", "reviewed", "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertGreater(payload["count"], 0)

            # Verify ack store file exists
            acks_path = repo / "logs" / "runtime" / "journal.acks.json"
            self.assertTrue(acks_path.is_file())
            acks = json.loads(acks_path.read_text(encoding="utf-8"))
            self.assertGreater(len(acks), 0)
            for entry in acks.values():
                self.assertEqual(entry["reason"], "reviewed")

    def test_ack_filters_events_from_live_context(self) -> None:
        import time as _time

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._run(repo, "sync", "--client", "personal")

            # Write journal events directly so we control exactly what's there
            journal_dir = repo / "logs" / "runtime"
            journal_dir.mkdir(parents=True, exist_ok=True)
            now = _time.time()
            events = [
                {"ts": now - 100, "type": "pulse.service_restarted", "subject": "api-stub", "detail": {}},
                {"ts": now - 50, "type": "agent.note", "subject": "personal", "detail": {}},
                {"ts": now - 10, "type": "focus.activated", "subject": "personal", "detail": {}},
            ]
            with (journal_dir / "journal.jsonl").open("w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")

            # Focus should show all 3 events in Recent Activity
            self._run(repo, "focus", "personal")
            claude_md_1 = (repo / "home" / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("Recent Activity", claude_md_1)
            self.assertIn("pulse.service_restarted", claude_md_1)

            # Ack only the pulse event
            self._run(repo, "ack", "--type", "pulse.service_restarted", "--reason", "fixed")

            # Re-focus — pulse event should be hidden, others visible
            # Overwrite journal to same content (focus appends its own events)
            with (journal_dir / "journal.jsonl").open("w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")

            self._run(repo, "focus", "personal")
            claude_md_2 = (repo / "home" / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("Recent Activity", claude_md_2)
            self.assertNotIn("pulse.service_restarted", claude_md_2)
            self.assertIn("acknowledged events hidden", claude_md_2)

    def test_ack_by_type_filters_matching_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._run(repo, "sync", "--client", "personal")

            # Generate some journal events
            self._run(repo, "focus", "personal")

            # Ack only focus.activated events
            result = self._run(
                repo, "ack", "--type", "focus.activated", "--reason", "noted", "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            for item in payload["acked"]:
                self.assertEqual(item["type"], "focus.activated")

    def test_ack_by_subject_filters_matching_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._run(repo, "sync", "--client", "personal")
            self._run(repo, "focus", "personal")

            result = self._run(
                repo, "ack", "--subject", "personal", "--reason", "handled", "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            for item in payload["acked"]:
                self.assertEqual(item["subject"], "personal")

    def test_ack_list_shows_current_acks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._run(repo, "sync", "--client", "personal")
            self._run(repo, "focus", "personal")
            self._run(repo, "ack", "--all", "--reason", "reviewed")

            result = self._run(repo, "ack", "--list", "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertGreater(payload["count"], 0)

    def test_ack_requires_filter_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "ack", "--format", "json")
            self.assertNotEqual(result.returncode, 0)

    def test_ack_no_matching_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo, "ack", "--type", "nonexistent.type", "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["count"], 0)

    def test_ack_prune_removes_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            # Write a fake ack store with an old entry
            acks_dir = repo / "logs" / "runtime"
            acks_dir.mkdir(parents=True, exist_ok=True)
            import time as _time
            old_ts = _time.time() - (25 * 3600)  # 25h ago — expired
            (acks_dir / "journal.acks.json").write_text(
                json.dumps({"12345.0": {"at": old_ts, "reason": "old"}}),
                encoding="utf-8",
            )

            result = self._run(repo, "ack", "--prune", "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["pruned"], 1)

            # Store should be empty
            acks = json.loads((acks_dir / "journal.acks.json").read_text(encoding="utf-8"))
            self.assertEqual(len(acks), 0)


if __name__ == "__main__":
    unittest.main()
