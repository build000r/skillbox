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
    def test_default_skill_repos_config_matches_hardened_shared_pack(self) -> None:
        config = MANAGE_MODULE.load_skill_repos_config(
            ROOT_DIR / "workspace" / "skill-repos.yaml",
        )
        all_skills: list[str] = []
        for entry in config["skill_repos"]:
            pick = entry.get("pick")
            if pick:
                all_skills.extend(pick)
        self.assertEqual(
            sorted(all_skills),
            sorted(
                MANAGE_MODULE.HARDENED_SHARED_DEFAULT_SKILLS
                + ["cass-memory"]
                + MANAGE_MODULE.HARDENED_CLIENT_PLANNING_SKILLS
                + ["cass", "smart"],
            ),
        )

    def test_sync_creates_core_runtime_state_and_installs_default_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "sync", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            actions = payload["actions"]
            self.assertTrue((repo / "repos").is_dir())
            self.assertTrue((repo / ".skillbox-state" / "logs" / "runtime").is_dir())
            self.assertTrue((repo / ".skillbox-state" / "logs" / "repos").is_dir())
            self.assertFalse((repo / ".skillbox-state" / "logs" / "api").exists())
            self.assertFalse((repo / ".skillbox-state" / "logs" / "web").exists())
            self.assertTrue((repo / ".skillbox-state" / "home" / ".claude" / "skills" / "sample-skill" / "SKILL.md").is_file())
            self.assertTrue((repo / ".skillbox-state" / "home" / ".codex" / "skills" / "sample-skill" / "SKILL.md").is_file())
            self.assertTrue((repo / ".skillbox-state" / "home" / ".local" / "bin" / "swimmers").is_file())
            self.assertFalse((repo / ".skillbox-state" / "home" / ".claude" / "skills" / "personal-skill").exists())
            self.assertTrue((repo / "workspace" / "skill-repos.lock.json").is_file())
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
            self.assertEqual(skills["default-skills"]["kind"], "skill-repo-set")
            self.assertIn("skill-repos.yaml", skills["default-skills"]["skill_repos_config"])
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
            self.assertIn("skill-repo-lock", before_warning_codes)

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
            self.assertNotIn("skill-repo-lock", after_warning_codes)
            self.assertNotIn("skill-repo-install", after_warning_codes)
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
            self.assertEqual(artifact["state"], "ok")
            self.assertEqual(artifact["source_kind"], "file")
            self.assertTrue(bool(artifact["desired_sha256"]))
            self.assertTrue(skillset["lock_present"])
            self.assertEqual(skill_entry["name"], "sample-skill")
            self.assertEqual(target_states["claude"], "ok")
            self.assertEqual(target_states["codex"], "ok")

    def test_sync_ingress_artifacts_writes_route_manifest_and_nginx_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = self._ingress_model(repo)

            actions = MANAGE_MODULE.sync_ingress_artifacts(model, dry_run=False)

            route_file = repo / "logs" / "runtime" / "ingress-routes.json"
            nginx_config = repo / "logs" / "runtime" / "ingress-nginx.conf"
            self.assertTrue(route_file.is_file())
            self.assertTrue(nginx_config.is_file())
            payload = json.loads(route_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["routes"][0]["service_id"], "backend")
            self.assertEqual(payload["routes"][0]["listener"], "public")
            self.assertEqual(payload["routes"][0]["origin_url"], "http://127.0.0.1:9100")
            self.assertIn("location = /v1/report", nginx_config.read_text(encoding="utf-8"))
            self.assertIn("proxy_pass http://127.0.0.1:9100;", nginx_config.read_text(encoding="utf-8"))
            self.assertTrue(any(action.startswith("render-ingress-routes:") for action in actions))
            self.assertTrue(any(action.startswith("render-ingress-nginx:") for action in actions))

    def test_resolved_ingress_routes_order_exact_before_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = self._ingress_model(repo)
            model["ingress_routes"].append(
                {
                    "id": "report-prefix",
                    "service_id": "backend",
                    "listener": "public",
                    "path": "/v1",
                    "match": "prefix",
                    "client": "jeremy",
                    "profiles": ["local-ecom"],
                }
            )

            routes = MANAGE_MODULE.resolved_ingress_routes(model)
            nginx_config = MANAGE_MODULE.render_ingress_nginx_config(model)

            self.assertEqual([route["id"] for route in routes], ["report-command", "report-prefix"])
            self.assertLess(
                nginx_config.index("location = /v1/report"),
                nginx_config.index("location ^~ /v1"),
            )

    def test_runtime_status_includes_resolved_ingress_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = self._ingress_model(repo)

            with mock.patch.object(MANAGE_MODULE, "probe_service", return_value={"state": "running"}):
                status = MANAGE_MODULE.runtime_status(model)

            ingress = status["ingress"]
            self.assertTrue(ingress["route_file"].endswith("ingress-routes.json"))
            self.assertEqual(len(ingress["routes"]), 1)
            route = ingress["routes"][0]
            self.assertIn("service_state", route)
            self.assertEqual(route["request_url"], "https://reports.example.test/v1/report")
            self.assertEqual(route["origin_url"], "http://127.0.0.1:9100")

    def test_check_manifest_requires_service_origin_url_for_ingress_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = self._ingress_model(repo)
            model["services"][0]["origin_url"] = ""

            results = MANAGE_MODULE.check_manifest(model)
            issues = [
                issue
                for result in results
                if result.code == "runtime-manifest"
                for issue in result.details.get("issues", [])
            ]

            self.assertTrue(
                any("without a valid origin_url" in issue for issue in issues)
            )

    def test_validate_ingress_requires_service_origin_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = self._ingress_model(repo)
            model["services"][0]["origin_url"] = ""

            results = MANAGE_MODULE.validate_ingress(model)

            self.assertEqual([item.code for item in results], ["ingress-upstream-missing"])
            self.assertEqual(results[0].details["routes"], ["report-command"])

    def test_probe_service_marks_route_less_ingress_as_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = self._ingress_model(repo)
            ingress_service = {
                "id": "ingress-router",
                "kind": "ingress",
                "command": "python3 scripts/ingress_proxy.py --routes-file ${SKILLBOX_INGRESS_ROUTE_FILE}",
            }
            model["services"].append(ingress_service)
            model["ingress_routes"] = []

            state = MANAGE_MODULE.probe_service(model, ingress_service)

            self.assertEqual(state["state"], "idle")
            self.assertFalse(state["managed"])
            self.assertEqual(state["manager_reason"], "no ingress routes active")

    def test_stop_services_stops_stale_idle_ingress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = self._ingress_model(repo)
            ingress_service = {
                "id": "ingress-router",
                "kind": "ingress",
                "command": "python3 scripts/ingress_proxy.py --routes-file ${SKILLBOX_INGRESS_ROUTE_FILE}",
            }
            model["services"].append(ingress_service)
            model["ingress_routes"] = []

            with (
                mock.patch(
                    "runtime_manager.runtime_ops.live_service_pid",
                    return_value=4321,
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.stop_process",
                    return_value=("stopped", "SIGTERM"),
                ) as stop_process,
            ):
                results = MANAGE_MODULE.stop_services(
                    model,
                    [ingress_service],
                    dry_run=False,
                    wait_seconds=0.1,
                )

            self.assertEqual(results[0]["result"], "stopped")
            self.assertEqual(results[0]["pid"], 4321)
            self.assertEqual(results[0]["signal"], "SIGTERM")
            stop_process.assert_called_once_with(4321, 0.1)

    def test_doctor_warns_and_sync_reconciles_stale_file_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            sync = self._run(repo, "sync", "--format", "json")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            source_path = repo / "artifacts" / "swimmers.bin"
            target_path = repo / ".skillbox-state" / "home" / ".local" / "bin" / "swimmers"
            source_path.write_text("#!/bin/sh\necho updated\n", encoding="utf-8")

            doctor = self._run(repo, "doctor", "--format", "json")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            checks = json.loads(doctor.stdout)["checks"]
            artifact_check = next(item for item in checks if item["code"] == "syncable-artifact-paths")
            self.assertEqual(artifact_check["status"], "warn")
            self.assertIn("stale", artifact_check["details"])
            self.assertTrue(any(".skillbox-state/home/.local/bin/swimmers" in item for item in artifact_check["details"]["stale"]))

            reconcile = self._run(repo, "sync", "--format", "json")
            self.assertEqual(reconcile.returncode, 0, reconcile.stderr)
            actions = json.loads(reconcile.stdout)["actions"]
            self.assertTrue(any("copy-reconcile:" in action for action in actions), actions)
            self.assertEqual(target_path.read_text(encoding="utf-8"), source_path.read_text(encoding="utf-8"))

            status = self._run(repo, "status", "--format", "json")
            self.assertEqual(status.returncode, 0, status.stderr)
            artifact = json.loads(status.stdout)["artifacts"][0]
            self.assertEqual(artifact["state"], "ok")

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
            self.assertTrue((repo / ".skillbox-state" / "logs" / "clients" / "personal").is_dir())
            self.assertTrue((repo / ".skillbox-state" / "clients" / "personal" / "skill-repos.lock.json").is_file())
            self.assertTrue((repo / ".skillbox-state" / "home" / ".claude" / "skills" / "personal-skill" / "SKILL.md").is_file())
            self.assertTrue((repo / ".skillbox-state" / "home" / ".codex" / "skills" / "personal-skill" / "SKILL.md").is_file())

    def test_skills_command_auto_selects_pwd_matched_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            client_root = repo / ".skillbox-state" / "monoserver"
            self._write_client_overlay(
                repo,
                "personal",
                label="Personal",
                default_cwd=str(client_root),
                root_path=str(client_root),
                include_context=True,
            )

            result = self._run(
                repo,
                "skills",
                "--cwd",
                str(client_root / "app"),
                "--no-global",
                "--no-project",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            effective_names = {item["name"] for item in payload["effective"]}
            self.assertEqual(payload["active_clients"], ["personal"])
            self.assertEqual(payload["matched_clients"][0]["id"], "personal")
            self.assertIn("sample-skill", effective_names)
            self.assertIn("personal-skill", effective_names)

    def test_sync_reconciles_safe_git_repo_residue_into_a_real_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            source_repo = self._create_git_source_repo(repo, "fixture-app")

            self._write_client_overlay(
                repo,
                "acme-sync",
                label="Acme Sync",
                default_cwd="${SKILLBOX_MONOSERVER_ROOT}/acme-sync/app",
                root_path="${SKILLBOX_MONOSERVER_ROOT}/acme-sync",
                include_context=True,
            )

            clients_root = self._clients_host_root(repo)
            env_source = clients_root / "acme-sync" / "env" / "app.env"
            env_source.parent.mkdir(parents=True, exist_ok=True)
            env_source.write_text("API_TOKEN=from-source\n", encoding="utf-8")
            (clients_root / "acme-sync" / "skill-repos.yaml").write_text(
                "version: 2\nskill_repos: []\n",
                encoding="utf-8",
            )

            overlay_path = clients_root / "acme-sync" / "overlay.yaml"
            overlay_doc = MANAGE_MODULE.load_yaml(overlay_path)
            overlay_doc["client"]["default_cwd"] = "${SKILLBOX_MONOSERVER_ROOT}/acme-sync/app"
            overlay_doc["client"]["repos"] = [
                {
                    "id": "app",
                    "kind": "repo",
                    "path": "${SKILLBOX_MONOSERVER_ROOT}/acme-sync/app",
                    "repo_path": "${SKILLBOX_MONOSERVER_ROOT}/acme-sync/app",
                    "required": True,
                    "profiles": ["core"],
                    "source": {
                        "kind": "git",
                        "url": str(source_repo),
                        "branch": "main",
                    },
                    "sync": {"mode": "clone-if-missing"},
                }
            ]
            overlay_doc["client"]["env_files"] = [
                {
                    "id": "app-env",
                    "repo_id": "app",
                    "path": "${SKILLBOX_MONOSERVER_ROOT}/acme-sync/app/.env",
                    "target_path": "${SKILLBOX_MONOSERVER_ROOT}/acme-sync/app/.env",
                    "required": True,
                    "profiles": ["core"],
                    "source": {
                        "kind": "file",
                        "path": "${SKILLBOX_CLIENTS_HOST_ROOT}/acme-sync/env/app.env",
                        "source_path": "${SKILLBOX_CLIENTS_HOST_ROOT}/acme-sync/env/app.env",
                    },
                    "sync": {"mode": "write"},
                }
            ]
            overlay_path.write_text(MANAGE_MODULE.render_yaml_document(overlay_doc), encoding="utf-8")

            target_repo = repo / ".skillbox-state" / "monoserver" / "acme-sync" / "app"
            target_repo.mkdir(parents=True, exist_ok=True)
            (target_repo / ".env").write_text("API_TOKEN=stale\n", encoding="utf-8")

            result = self._run(repo, "sync", "--client", "acme-sync", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            actions = json.loads(result.stdout)["actions"]
            self.assertTrue(any("clone-reconcile:" in action for action in actions), actions)
            self.assertTrue((target_repo / ".git").is_dir())
            self.assertTrue((target_repo / "README.md").is_file())
            self.assertEqual((target_repo / ".env").read_text(encoding="utf-8"), "API_TOKEN=from-source\n")

    def test_profile_selection_limits_optional_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            render = self._run(repo, "render", "--profile", "surfaces", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            payload = json.loads(render.stdout)
            self.assertEqual(payload["active_profiles"], ["core", "surfaces"])
            self.assertEqual(
                {item["id"] for item in payload["services"]},
                {"internal-env-manager", "cm-mcp", "api-stub", "web-stub"},
            )
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
            self.assertEqual({item["id"] for item in payload["artifacts"]}, {"swimmers-bin", "cass-bin", "cm-bin"})
            self.assertEqual(
                {item["id"] for item in payload["services"]},
                {"internal-env-manager", "cm-mcp", "swimmers-server"},
            )
            self.assertEqual(
                {item["id"] for item in payload["logs"]},
                {"runtime", "repos", "swimmers"},
            )

            sync = self._run(repo, "sync", "--profile", "swimmers", "--format", "json")
            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / ".skillbox-state" / "logs" / "swimmers").is_dir())

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
                {"skillbox-self", "managed-repos"},
            )
            self.assertEqual(
                {item["id"] for item in payload["artifacts"]},
                {"swimmers-bin", "cass-bin", "cm-bin", "fwc-bin", "dcg-bin"},
            )
            self.assertEqual(
                {item["id"] for item in payload["services"]},
                {"internal-env-manager", "cm-mcp", "fwc-mcp", "dcg-mcp"},
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
            self.assertTrue((repo / ".skillbox-state" / "logs" / "connectors").is_dir())
            self.assertTrue((repo / ".skillbox-state" / "home" / ".local" / "bin" / "fwc").is_file())
            self.assertTrue((repo / ".skillbox-state" / "home" / ".local" / "bin" / "dcg").is_file())

            doctor = self._run(repo, "doctor", "--profile", "connectors", "--format", "json")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            doctor_payload = json.loads(doctor.stdout)
            manifest_check = next(item for item in doctor_payload["checks"] if item["code"] == "runtime-manifest")
            connector_check = next(item for item in doctor_payload["checks"] if item["code"] == "connector-contract")
            self.assertEqual(manifest_check["status"], "pass")
            self.assertEqual(connector_check["status"], "pass")

    def test_profile_selection_activates_connectors_dev_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            render = self._run(repo, "render", "--profile", "connectors-dev", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            payload = json.loads(render.stdout)
            self.assertEqual(payload["active_profiles"], ["connectors-dev", "core"])
            self.assertEqual(
                {item["id"] for item in payload["repos"]},
                {"skillbox-self", "managed-repos", "flywheel-connectors", "destructive-command-guard"},
            )
            self.assertEqual(
                {item["id"] for item in payload["artifacts"]},
                {"swimmers-bin", "cass-bin", "cm-bin"},
            )
            self.assertEqual(
                {item["id"] for item in payload["services"]},
                {"internal-env-manager", "cm-mcp"},
            )
            self.assertEqual(
                {item["id"] for item in payload["logs"]},
                {"runtime", "repos"},
            )

            sync = self._run(repo, "sync", "--profile", "connectors-dev", "--format", "json")
            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / "repos" / "flywheel_connectors").is_dir())
            self.assertTrue((repo / "repos" / "destructive_command_guard").is_dir())

    def test_doctor_fails_when_client_connectors_exceed_box_superset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._set_client_connectors(repo, "personal", ["github", "postgres"])

            result = self._run(repo, "doctor", "--client", "personal", "--profile", "connectors", "--format", "json")

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            connector_failures = [
                item for item in payload["checks"] if item["status"] == "fail" and item["code"] == "connector-contract"
            ]
            self.assertEqual(len(connector_failures), 1, payload["checks"])
            issues = connector_failures[0]["details"]["issues"]
            self.assertTrue(
                any("outside SKILLBOX_FWC_CONNECTORS: postgres" in issue for issue in issues),
                issues,
            )

    def test_doctor_allows_independent_client_connector_subsets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._set_runtime_env_value(repo, "SKILLBOX_FWC_CONNECTORS", "github,slack,linear")
            self._set_client_connectors(repo, "personal", ["github", "slack"])
            self._set_client_connectors(repo, "vibe-coding-client", ["linear"])
            (repo / ".skillbox-state" / "monoserver" / "vibe-coding-client").mkdir(parents=True, exist_ok=True)

            personal_result = self._run(
                repo,
                "doctor",
                "--client",
                "personal",
                "--profile",
                "connectors",
                "--format",
                "json",
            )
            vibe_result = self._run(
                repo,
                "doctor",
                "--client",
                "vibe-coding-client",
                "--profile",
                "connectors",
                "--format",
                "json",
            )

            self.assertEqual(personal_result.returncode, 0, personal_result.stderr)
            self.assertEqual(vibe_result.returncode, 0, vibe_result.stderr)
            for result in (personal_result, vibe_result):
                checks = json.loads(result.stdout)["checks"]
                connector_check = next(item for item in checks if item["code"] == "connector-contract")
                self.assertEqual(connector_check["status"], "pass")

    def test_doctor_ignores_out_of_scope_client_parity_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            overlay_path = self._clients_host_root(repo) / "personal" / "overlay.yaml"
            overlay_doc = MANAGE_MODULE.load_yaml(overlay_path)
            overlay_doc.setdefault("client", {})["parity_ledger"] = [
                {
                    "id": "ghost-local-core-service",
                    "legacy_surface": "ghost-local-core-service",
                    "surface_type": "service",
                    "action": "declare",
                    "ownership_state": "covered",
                    "intended_profiles": ["local-core"],
                    "bridge_dependency": None,
                }
            ]
            overlay_path.write_text(MANAGE_MODULE.render_yaml_document(overlay_doc), encoding="utf-8")

            result = self._run(
                repo,
                "doctor",
                "--client",
                "personal",
                "--profile",
                "connectors",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            coverage_failures = [
                item
                for item in payload["checks"]
                if item["status"] == "fail" and item["code"] == "LOCAL_RUNTIME_COVERAGE_GAP"
            ]
            self.assertEqual(coverage_failures, [], payload["checks"])
            parity_check = next(item for item in payload["checks"] if item["code"] == "parity-ledger")
            self.assertEqual(parity_check["status"], "pass")

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
            self.assertTrue((repo / ".skillbox-state" / "logs" / "runtime" / "fixture-daemon.pid").is_file())
            self.assertTrue((repo / ".skillbox-state" / "logs" / "runtime" / "fixture-daemon.ready").is_file())

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
            self.assertFalse((repo / ".skillbox-state" / "logs" / "runtime" / "fixture-daemon.pid").exists())
            self.assertFalse((repo / ".skillbox-state" / "logs" / "runtime" / "fixture-daemon.ready").exists())

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
            self.assertTrue((repo / ".skillbox-state" / "logs" / "runtime" / "fixture-worker.ready").is_file())

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
            self.assertFalse((repo / ".skillbox-state" / "logs" / "runtime" / "fixture-worker.pid").exists())
            self.assertFalse((repo / ".skillbox-state" / "logs" / "runtime" / "fixture-daemon.pid").exists())

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

    def test_doctor_fails_when_installed_skill_drifts_from_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            sync = self._run(repo, "sync")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            (repo / ".skillbox-state" / "home" / ".claude" / "skills" / "sample-skill" / "SKILL.md").write_text(
                "---\nname: sample-skill\ndescription: drifted\n---\n",
                encoding="utf-8",
            )

            result = self._run(repo, "doctor", "--format", "json")

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            checks = payload["checks"]
            install_failures = [
                item for item in checks if item["status"] == "fail" and item["code"] == "skill-repo-install"
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
            self.assertTrue((repo / ".skillbox-state" / "clients" / "acme-studio" / "overlay.yaml").is_file())
            self.assertTrue((repo / ".skillbox-state" / "clients" / "acme-studio" / "skill-repos.yaml").is_file())
            self.assertTrue((repo / ".skillbox-state" / "clients" / "acme-studio" / "skills" / ".gitkeep").is_file())
            self.assertTrue((repo / ".skillbox-state" / "clients" / "_shared" / "skills" / ".gitkeep").is_file())
            self.assertTrue((repo / ".skillbox-state" / "clients" / "acme-studio" / "plans" / "INDEX.md").is_file())
            self.assertTrue((repo / ".skillbox-state" / "clients" / "acme-studio" / "plans" / "draft" / ".gitkeep").is_file())
            self.assertTrue((repo / ".skillbox-state" / "clients" / "acme-studio" / "plans" / "released" / ".gitkeep").is_file())
            self.assertTrue((repo / ".skillbox-state" / "clients" / "acme-studio" / "plans" / "sessions" / ".gitkeep").is_file())
            self.assertFalse((repo / "default-skills" / "clients" / "acme-studio").exists())
            self.assertFalse((repo / "skills" / "clients" / "acme-studio").exists())
            self.assertEqual(
                payload["next_actions"],
                [
                    "sync --client acme-studio --format json",
                    "focus acme-studio --format json",
                    "client-diff acme-studio --format json",
                    "client-publish acme-studio --acceptance --format json",
                ],
            )

            skill_repos_content = (repo / ".skillbox-state" / "clients" / "acme-studio" / "skill-repos.yaml").read_text(encoding="utf-8")
            self.assertIn("../_shared/skills", skill_repos_content)
            for skill_name in MANAGE_MODULE.HARDENED_CLIENT_PLANNING_SKILLS:
                self.assertIn(skill_name, skill_repos_content)
                self.assertTrue(
                    (repo / ".skillbox-state" / "clients" / "_shared" / "skills" / skill_name / "SKILL.md").is_file()
                )

            overlay_doc = MANAGE_MODULE.load_yaml(
                repo / ".skillbox-state" / "clients" / "acme-studio" / "overlay.yaml",
            )
            client_context = overlay_doc["client"]["context"]
            self.assertEqual(client_context["cwd_match"], ["${SKILLBOX_MONOSERVER_ROOT}"])
            self.assertEqual(client_context["plans"], MANAGE_MODULE.HARDENED_CLIENT_PLAN_PATHS)

            render = self._run(repo, "render", "--client", "acme-studio", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            render_payload = json.loads(render.stdout)
            self.assertIn("acme-studio", {client["id"] for client in render_payload["clients"]})
            self.assertEqual(render_payload["active_clients"], ["acme-studio"])
            self.assertEqual(
                {item["id"] for item in render_payload["repos"]},
                {"skillbox-self", "managed-repos", "acme-studio-root"},
            )
            skillset = next(item for item in render_payload["skills"] if item["id"] == "acme-studio-skills")
            self.assertEqual(skillset["kind"], "skill-repo-set")
            self.assertIn("skill-repos.yaml", skillset["skill_repos_config"])

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / ".skillbox-state" / "logs" / "clients" / "acme-studio").is_dir())
            self.assertTrue((repo / ".skillbox-state" / "clients" / "acme-studio" / "skill-repos.lock.json").is_file())

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
            self.assertTrue((external_clients / "acme-studio" / "skill-repos.yaml").is_file())
            self.assertTrue((external_clients / "acme-studio" / "skills" / ".gitkeep").is_file())
            self.assertTrue((external_clients / "acme-studio" / "plans" / "INDEX.md").is_file())
            self.assertFalse((repo / ".skillbox-state" / "clients" / "acme-studio").exists())
            self.assertFalse((repo / "default-skills" / "clients" / "acme-studio").exists())
            self.assertFalse((repo / "skills" / "clients" / "acme-studio").exists())

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((external_clients / "acme-studio" / "skill-repos.lock.json").is_file())

    def test_client_local_skill_sources_live_under_client_root_and_project_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            # Use default client-init to scaffold with the new skill-repos model
            result = self._run(repo, "client-init", "acme-studio", "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)

            overlay_dir = repo / ".skillbox-state" / "clients" / "acme-studio"
            skill_dir = overlay_dir / "skills" / "custom-skill"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: custom-skill\n"
                "description: Custom local skill.\n"
                "---\n\n"
                "# Custom Skill\n",
                encoding="utf-8",
            )

            # Update skill-repos.yaml to include the custom skill
            (overlay_dir / "skill-repos.yaml").write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - path: ./skills\n"
                "    pick: [custom-skill]\n",
                encoding="utf-8",
            )

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")
            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / ".skillbox-state" / "home" / ".claude" / "skills" / "custom-skill" / "SKILL.md").is_file())

    def test_skill_command_add_links_project_local_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            project = repo / "project"
            project.mkdir()
            source = repo / "skills" / "project-skill"
            self._write_skill_dir(source, "project-skill")

            result = self._run(
                repo,
                "skill",
                "add",
                "project-skill",
                "--source",
                str(source),
                "--to",
                "project",
                "--cwd",
                str(project),
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["summary"]["link"], 2)
            for surface in ("claude", "codex"):
                link = project / f".{surface}" / "skills" / "project-skill"
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), source.resolve())

    def test_skill_command_remove_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "skill",
                "remove",
                "project-skill",
                "--format",
                "json",
            )

            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertIn("may unlink existing installs", payload["error"]["message"])

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
            self.assertIn(".skillbox-state/clients/personal/overlay.yaml", payload["error"]["message"])

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
            self.assertTrue((repo / ".skillbox-state" / "clients" / "acme-studio" / "overlay.yaml").is_file())

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
                {"internal-env-manager", "cm-mcp", "app-dev"},
            )
            self.assertIn("acme-studio-services", {item["id"] for item in render_payload["logs"]})
            self.assertEqual(
                next(client for client in render_payload["clients"] if client["id"] == "acme-studio")["default_cwd"],
                "/monoserver/app",
            )

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / ".skillbox-state" / "monoserver" / "app" / "README.md").is_file())
            self.assertTrue((repo / ".skillbox-state" / "logs" / "clients" / "acme-studio" / "services").is_dir())
            self.assertTrue(any("clone-if-missing:" in action for action in json.loads(sync.stdout)["actions"]))

    def test_client_init_with_blueprint_scaffolds_client_scoped_connectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_client_blueprint(
                repo,
                "git-repo",
                "version: 1\n"
                "description: Clone a repo.\n"
                "variables:\n"
                "  - name: PRIMARY_REPO_ID\n"
                "    default: app\n"
                "  - name: PRIMARY_REPO_URL\n"
                "    required: true\n"
                "  - name: PRIMARY_REPO_BRANCH\n"
                "    default: main\n"
                "  - name: PRIMARY_REPO_PATH\n"
                "    default: ${CLIENT_ROOT}/${PRIMARY_REPO_ID}\n"
                "  - name: CONNECTORS\n"
                "    default: \"\"\n"
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
                "  connectors: ${CONNECTORS}\n",
            )
            source_repo = self._create_git_source_repo(repo, "fixture-app")

            result = self._run(
                repo,
                "client-init",
                "acme-studio",
                "--blueprint",
                "git-repo",
                "--set",
                f"PRIMARY_REPO_URL={source_repo}",
                "--set",
                "CONNECTORS=github,slack",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            overlay_doc = MANAGE_MODULE.load_yaml(repo / ".skillbox-state" / "clients" / "acme-studio" / "overlay.yaml")
            self.assertEqual(
                overlay_doc["client"]["connectors"],
                [{"id": "github"}, {"id": "slack"}],
            )
            env_example = (repo / ".env.example").read_text(encoding="utf-8")
            self.assertIn("SKILLBOX_FWC_CONNECTORS=github,slack", env_example)

    def test_client_init_with_skill_builder_blueprint_scaffolds_skill_builder_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_skill_builder_blueprint(repo)

            result = self._run(
                repo,
                "client-init",
                "acme-builder",
                "--blueprint",
                "skill-builder-fwc",
                "--set",
                "CONNECTORS=github,slack",
                "--set",
                "SLACK_CHANNELS=#alerts",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["blueprint"]["id"], "skill-builder-fwc")

            overlay_dir = repo / ".skillbox-state" / "clients" / "acme-builder"
            overlay_doc = MANAGE_MODULE.load_yaml(overlay_dir / "overlay.yaml")
            client_doc = overlay_doc["client"]
            self.assertEqual(client_doc["scaffold"]["pack"], "skill-builder")
            self.assertEqual(
                client_doc["context"]["workflow_builder"],
                MANAGE_MODULE.HARDENED_CLIENT_SKILL_BUILDER_CONTEXT["workflow_builder"],
            )
            self.assertNotIn("plans", client_doc["context"])
            self.assertEqual(
                client_doc["connectors"],
                [
                    {"id": "github"},
                    {
                        "id": "slack",
                        "capabilities": ["read", "write"],
                        "scopes": {"channels": ["#alerts"]},
                    },
                ],
            )
            skill_repos_content = (overlay_dir / "skill-repos.yaml").read_text(encoding="utf-8")
            for skill_name in MANAGE_MODULE.HARDENED_CLIENT_SKILL_BUILDER_SKILLS:
                self.assertIn(skill_name, skill_repos_content)
            self.assertTrue((overlay_dir / "workflows" / "INDEX.md").is_file())
            self.assertTrue((overlay_dir / "workflows" / "EXTRACTION.md").is_file())
            self.assertTrue((overlay_dir / "evaluations" / "README.md").is_file())
            self.assertTrue((overlay_dir / "invocations" / "README.md").is_file())
            self.assertTrue((overlay_dir / "observability" / "README.md").is_file())
            self.assertTrue((overlay_dir / "skills" / "skill-issue" / "SKILL.md").is_file())
            self.assertTrue((overlay_dir / "skills" / "prompt-reviewer" / "SKILL.md").is_file())
            self.assertFalse((overlay_dir / "plans").exists())

            sync = self._run(repo, "sync", "--client", "acme-builder", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / ".skillbox-state" / "logs" / "clients" / "acme-builder").is_dir())
            self.assertTrue((repo / ".skillbox-state" / "logs" / "clients" / "acme-builder" / "invocations").is_dir())
            self.assertTrue((repo / ".skillbox-state" / "logs" / "clients" / "acme-builder" / "evaluations").is_dir())
            self.assertTrue((repo / ".skillbox-state" / "logs" / "clients" / "acme-builder" / "observability").is_dir())
            self.assertTrue((overlay_dir / "skill-repos.lock.json").is_file())

            project = self._run(repo, "client-project", "acme-builder", "--format", "json")

            self.assertEqual(project.returncode, 0, project.stderr)
            projection_dir = repo / "builds" / "clients" / "acme-builder"
            self.assertTrue((projection_dir / "workspace" / "clients" / "acme-builder" / "workflows" / "INDEX.md").is_file())
            self.assertTrue((projection_dir / "workspace" / "clients" / "acme-builder" / "evaluations" / "README.md").is_file())
            self.assertTrue((projection_dir / "workspace" / "clients" / "acme-builder" / "skills" / "skill-issue" / "SKILL.md").is_file())
            self.assertTrue((projection_dir / "workspace" / "clients" / "acme-builder" / "skills" / "prompt-reviewer" / "SKILL.md").is_file())
            self.assertFalse((projection_dir / "workspace" / "clients" / "acme-builder" / "plans").exists())

    def test_client_open_from_bundle_with_skill_builder_blueprint_materializes_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_skill_builder_blueprint(repo)

            init = self._run(
                repo,
                "client-init",
                "acme-builder",
                "--blueprint",
                "skill-builder-fwc",
                "--format",
                "json",
            )
            self.assertEqual(init.returncode, 0, init.stderr)

            project = self._run(repo, "client-project", "acme-builder", "--format", "json")
            self.assertEqual(project.returncode, 0, project.stderr)

            result = self._run(
                repo,
                "client-open",
                "acme-builder",
                "--from-bundle",
                "./builds/clients/acme-builder",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            output_dir = repo / "sand" / "acme-builder"
            self.assertEqual(payload["focus"]["status"], "skip")
            self.assertTrue((output_dir / "workspace" / "clients" / "acme-builder" / "context.yaml").is_file())
            self.assertTrue((output_dir / "workspace" / "clients" / "acme-builder" / "workflows" / "INDEX.md").is_file())
            self.assertTrue((output_dir / "workspace" / "clients" / "acme-builder" / "skills" / "skill-issue" / "SKILL.md").is_file())
            self.assertFalse((output_dir / "workspace" / "clients" / "acme-builder" / "plans").exists())

            context_doc = MANAGE_MODULE.load_yaml(
                output_dir / "workspace" / "clients" / "acme-builder" / "context.yaml"
            )
            self.assertEqual(
                Path(str(context_doc["workflow_builder"]["workflow_index"])).resolve(),
                (output_dir / "workspace" / "clients" / "acme-builder" / "workflows" / "INDEX.md").resolve(),
            )

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
            (repo / ".skillbox-state" / "monoserver" / "acme-studio").mkdir(parents=True, exist_ok=True)

            before = self._run(repo, "doctor", "--client", "acme-studio", "--format", "json")
            self.assertEqual(before.returncode, 0, before.stderr)
            before_payload = json.loads(before.stdout)["checks"]
            warning_codes = {item["code"] for item in before_payload if item["status"] == "warn"}
            self.assertIn("syncable-env-files", warning_codes)

            sync = self._run(repo, "sync", "--client", "acme-studio", "--format", "json")
            self.assertEqual(sync.returncode, 0, sync.stderr)
            sync_payload = json.loads(sync.stdout)
            self.assertTrue(any("hydrate-env:" in action for action in sync_payload["actions"]))

            env_target = repo / ".skillbox-state" / "monoserver" / "app" / ".env.local"
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
            (repo / ".skillbox-state" / "monoserver" / "acme-studio").mkdir(parents=True, exist_ok=True)

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
            pid_file = repo / ".skillbox-state" / "logs" / "clients" / "acme-studio" / "services" / "app-dev.pid"
            self.assertFalse(pid_file.exists())

    def test_translated_runtime_command_includes_item_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            model = MANAGE_MODULE.build_runtime_model(repo)
            command, env = MANAGE_MODULE.translated_runtime_command(
                model,
                {
                    "id": "scoped-pulse",
                    "command": "python3 .env-manager/pulse.py run",
                    "env": {
                        "SKILLBOX_PULSE_CLIENTS": "personal",
                        "SKILLBOX_LOG_ROOT_COPY": "/workspace/logs",
                    },
                },
            )
            self.assertEqual(command, "python3 .env-manager/pulse.py run")
            self.assertEqual(env["SKILLBOX_PULSE_CLIENTS"], "personal")
            self.assertEqual(
                env["SKILLBOX_LOG_ROOT_COPY"],
                str((repo / ".skillbox-state" / "logs").resolve()),
            )

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
            env_target = repo / ".skillbox-state" / "monoserver" / "app" / ".env.local"
            self.assertTrue(env_target.is_file())
            self.assertEqual(env_target.read_text(encoding="utf-8"), "PORT=4010\nAPI_KEY=acme\n")
            self.assertEqual(sorted((repo / ".skillbox-state" / "monoserver").rglob(".env.local")), [env_target])

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

            order_file = repo / ".skillbox-state" / "logs" / "runtime" / "bootstrap-order.log"
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
            self.assertTrue((repo / ".skillbox-state" / "logs" / "runtime" / "build-app.ok").is_file())
            self.assertTrue((repo / ".skillbox-state" / "logs" / "runtime" / "bootstrap-daemon.ready").is_file())

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

    def test_client_init_with_spaps_auth_blueprint_wires_auth_service_and_fixture_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_client_blueprint(
                repo,
                "git-repo-http-service-bootstrap-spaps-auth",
                (
                    ROOT_DIR
                    / "workspace"
                    / "client-blueprints"
                    / "git-repo-http-service-bootstrap-spaps-auth.yaml"
                ).read_text(encoding="utf-8"),
            )
            source_repo = self._create_git_source_repo(repo, "fixture-app")

            result = self._run(
                repo,
                "client-init",
                "acme-studio",
                "--blueprint",
                "git-repo-http-service-bootstrap-spaps-auth",
                "--set",
                f"PRIMARY_REPO_URL={source_repo}",
                "--set",
                "BOOTSTRAP_COMMAND=python3 -c \"from pathlib import Path; Path('.skillbox').mkdir(exist_ok=True); Path('.skillbox/bootstrap.ok').write_text('ok\\n', encoding='utf-8')\"",
                "--set",
                "SERVICE_COMMAND=python3 -m http.server 5173",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)

            render = self._run(repo, "render", "--client", "acme-studio", "--format", "json")
            self.assertEqual(render.returncode, 0, render.stderr)
            render_payload = json.loads(render.stdout)

            task_ids = {item["id"] for item in render_payload["tasks"]}
            self.assertEqual(task_ids, {"app-bootstrap", "spaps-fixtures"})

            services = {item["id"]: item for item in render_payload["services"]}
            self.assertIn("auth-api", services)
            self.assertIn("app-dev", services)
            self.assertEqual(services["app-dev"]["depends_on"], ["auth-api"])
            self.assertEqual(
                services["app-dev"]["bootstrap_tasks"],
                ["app-bootstrap", "spaps-fixtures"],
            )
            self.assertIn("npx spaps local", services["auth-api"]["command"])

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

    def test_client_init_with_unknown_scaffold_pack_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._write_client_blueprint(
                repo,
                "invalid-pack",
                "version: 1\n"
                "description: Invalid scaffold pack.\n"
                "scaffold:\n"
                "  pack: unsupported-pack\n"
                "client:\n"
                "  logs: []\n",
            )

            result = self._run(
                repo,
                "client-init",
                "acme-builder",
                "--blueprint",
                "invalid-pack",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "invalid_scaffold_pack")
            self.assertIn("unsupported-pack", payload["error"]["message"])

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
            self.assertTrue((projection_dir / "workspace" / "persistence.yaml").is_file())
            self.assertTrue((projection_dir / "projection.json").is_file())
            self.assertTrue((projection_dir / "runtime-model.json").is_file())
            self.assertTrue((projection_dir / "workspace" / "clients" / "personal" / "overlay.yaml").is_file())
            self.assertTrue((projection_dir / "workspace" / "clients" / "personal" / "skill-repos.yaml").is_file())

            self.assertFalse((projection_dir / "workspace" / "clients" / "vibe-coding-client").exists())

            runtime_doc = MANAGE_MODULE.load_yaml(projection_dir / "workspace" / "runtime.yaml")
            self.assertEqual(runtime_doc["selection"]["default_client"], "personal")
            runtime_text = (projection_dir / "workspace" / "runtime.yaml").read_text(encoding="utf-8")
            self.assertNotIn("vibe-coding-client", runtime_text)

            projection_payload = json.loads((projection_dir / "projection.json").read_text(encoding="utf-8"))
            self.assertEqual(projection_payload["client_id"], "personal")
            self.assertEqual(projection_payload["overlay_mode"], "overlay")
            projected_paths = {item["path"] for item in projection_payload["files"]}
            self.assertIn("workspace/runtime.yaml", projected_paths)
            self.assertIn("workspace/persistence.yaml", projected_paths)
            self.assertNotIn("workspace/clients/vibe-coding-client/overlay.yaml", projected_paths)

            model_text = (projection_dir / "runtime-model.json").read_text(encoding="utf-8")
            model_payload = json.loads(model_text)
            self.assertEqual(model_payload["active_clients"], ["personal"])
            self.assertEqual(model_payload["active_profiles"], ["core"])
            self.assertEqual(model_payload["persistence_manifest_file"], "/workspace/persistence.yaml")
            self.assertEqual(model_payload["storage"]["state_root"], "./.skillbox-state")
            self.assertNotIn("_host_path", model_text)
            self.assertNotIn("SKILLBOX_SWIMMERS_AUTH_TOKEN", model_text)
            self.assertNotIn("SKILLBOX_CLIENTS_HOST_ROOT", model_payload["env"])
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
                {"internal-env-manager", "cm-mcp", "api-stub", "web-stub"},
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
            self.assertTrue((private_repo / "clients" / "personal" / "skill-repos.yaml").is_file())
            self.assertTrue((private_repo / "clients" / "vibe-coding-client" / "overlay.yaml").is_file())
            self.assertEqual(
                payload["next_actions"],
                [
                    "client-init <client> --format json",
                    "client-diff <client> --format json",
                ],
            )

    def test_private_init_migrates_legacy_client_skill_trees_into_private_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)
            self._write_client_overlay(
                repo,
                "acme-studio",
                label="Acme Studio",
                default_cwd="${SKILLBOX_MONOSERVER_ROOT}/acme-studio",
                root_path="${SKILLBOX_MONOSERVER_ROOT}/acme-studio",
            )
            (repo / ".skillbox-state" / "clients" / "acme-studio" / "skill-repos.yaml").write_text(
                "version: 2\nskill_repos:\n  - path: ./skills\n",
                encoding="utf-8",
            )

            result = self._run(
                repo,
                "private-init",
                "--path",
                "../private-config",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            private_repo = workspace / "private-config"
            self.assertTrue((private_repo / "clients" / "acme-studio" / "skill-repos.yaml").is_file())
            self.assertTrue((private_repo / "clients" / "acme-studio" / "skills" / ".gitkeep").is_file())

    def test_private_init_normalizes_overlay_shape_in_private_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)

            legacy_overlay = repo / ".skillbox-state" / "clients" / "personal" / "overlay.yaml"
            legacy_overlay.write_text(
                "version: 1\n"
                "client:\n"
                "  id: personal\n"
                "  label: Personal\n"
                "  default_cwd: ${SKILLBOX_MONOSERVER_ROOT}\n"
                "  repo_roots:\n"
                "    - id: personal-root\n"
                "      kind: repo-root\n"
                "      path: ${SKILLBOX_MONOSERVER_ROOT}\n",
                encoding="utf-8",
            )

            result = self._run(
                repo,
                "private-init",
                "--path",
                "../private-config",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            private_client = workspace / "private-config" / "clients" / "personal"
            overlay_doc = MANAGE_MODULE.load_yaml(private_client / "overlay.yaml")
            client_doc = overlay_doc["client"]
            skillset = client_doc["skills"][0]

            self.assertEqual(skillset["kind"], "skill-repo-set")
            self.assertEqual(skillset["skill_repos_config"], "${SKILLBOX_CLIENTS_ROOT}/personal/skill-repos.yaml")
            self.assertEqual(skillset["lock_path"], "${SKILLBOX_CLIENTS_ROOT}/personal/skill-repos.lock.json")
            self.assertEqual(client_doc["context"]["plans"], MANAGE_MODULE.HARDENED_CLIENT_PLAN_PATHS)
            self.assertTrue((private_client / "plans" / "INDEX.md").is_file())
            self.assertTrue((private_client / "plans" / "draft" / ".gitkeep").is_file())
            skill_repos_content = (private_client / "skill-repos.yaml").read_text(encoding="utf-8")
            self.assertIn("../_shared/skills", skill_repos_content)
            for skill_name in MANAGE_MODULE.HARDENED_CLIENT_PLANNING_SKILLS:
                self.assertIn(skill_name, skill_repos_content)
                self.assertTrue(
                    (private_client.parent / "_shared" / "skills" / skill_name / "SKILL.md").is_file()
                )

    def test_private_init_preserves_skill_builder_scaffold_pack_and_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)
            self._write_skill_builder_blueprint(repo)

            init = self._run(
                repo,
                "client-init",
                "acme-builder",
                "--blueprint",
                "skill-builder-fwc",
                "--format",
                "json",
            )
            self.assertEqual(init.returncode, 0, init.stderr)

            overlay_dir = repo / ".skillbox-state" / "clients" / "acme-builder"
            custom_files = {
                overlay_dir / "workflows" / "INDEX.md": "# Custom workflow index\n",
                overlay_dir / "workflows" / "EXTRACTION.md": "# Keep this extraction note\n",
                overlay_dir / "evaluations" / "README.md": "# Existing evaluation notes\n",
                overlay_dir / "invocations" / "README.md": "# Existing invocation notes\n",
                overlay_dir / "observability" / "README.md": "# Existing observability notes\n",
            }
            for path, content in custom_files.items():
                path.write_text(content, encoding="utf-8")

            result = self._run(
                repo,
                "private-init",
                "--path",
                "../private-config",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            private_client = workspace / "private-config" / "clients" / "acme-builder"
            overlay_doc = MANAGE_MODULE.load_yaml(private_client / "overlay.yaml")
            client_doc = overlay_doc["client"]

            self.assertEqual(client_doc["scaffold"]["pack"], "skill-builder")
            self.assertNotIn("plans", client_doc["context"])
            self.assertEqual(
                client_doc["context"]["workflow_builder"],
                MANAGE_MODULE.HARDENED_CLIENT_SKILL_BUILDER_CONTEXT["workflow_builder"],
            )
            skill_repos_content = (private_client / "skill-repos.yaml").read_text(encoding="utf-8")
            for skill_name in MANAGE_MODULE.HARDENED_CLIENT_SKILL_BUILDER_SKILLS:
                self.assertIn(skill_name, skill_repos_content)
            self.assertFalse((private_client / "plans").exists())
            self.assertTrue((private_client / "skills" / "skill-issue" / "SKILL.md").is_file())
            for relative_path, expected in {
                Path("workflows/INDEX.md"): "# Custom workflow index\n",
                Path("workflows/EXTRACTION.md"): "# Keep this extraction note\n",
                Path("evaluations/README.md"): "# Existing evaluation notes\n",
                Path("invocations/README.md"): "# Existing invocation notes\n",
                Path("observability/README.md"): "# Existing observability notes\n",
            }.items():
                self.assertEqual((private_client / relative_path).read_text(encoding="utf-8"), expected)

    def test_private_init_preserves_hybrid_scaffold_pack_and_seeds_both_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)
            self._init_git_repo(repo)

            self._write_client_overlay(
                repo,
                "acme-hybrid",
                label="Acme Hybrid",
                default_cwd="${SKILLBOX_MONOSERVER_ROOT}/acme-hybrid",
                root_path="${SKILLBOX_MONOSERVER_ROOT}/acme-hybrid",
                include_context=True,
            )

            overlay_dir = repo / ".skillbox-state" / "clients" / "acme-hybrid"
            overlay_doc = MANAGE_MODULE.load_yaml(overlay_dir / "overlay.yaml")
            overlay_doc["client"]["scaffold"] = {"pack": "hybrid"}
            (overlay_dir / "overlay.yaml").write_text(
                MANAGE_MODULE.render_yaml_document(overlay_doc),
                encoding="utf-8",
            )
            (overlay_dir / "plans").mkdir(parents=True, exist_ok=True)
            (overlay_dir / "workflows").mkdir(parents=True, exist_ok=True)
            (overlay_dir / "plans" / "INDEX.md").write_text("# Custom plan index\n", encoding="utf-8")
            (overlay_dir / "workflows" / "INDEX.md").write_text("# Custom workflow index\n", encoding="utf-8")

            result = self._run(
                repo,
                "private-init",
                "--path",
                "../private-config",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            private_client = workspace / "private-config" / "clients" / "acme-hybrid"
            private_overlay = MANAGE_MODULE.load_yaml(private_client / "overlay.yaml")
            client_doc = private_overlay["client"]

            self.assertEqual(client_doc["scaffold"]["pack"], "hybrid")
            self.assertEqual(client_doc["context"]["plans"], MANAGE_MODULE.HARDENED_CLIENT_PLAN_PATHS)
            self.assertEqual(
                client_doc["context"]["workflow_builder"],
                MANAGE_MODULE.HARDENED_CLIENT_SKILL_BUILDER_CONTEXT["workflow_builder"],
            )
            skill_repos_content = (private_client / "skill-repos.yaml").read_text(encoding="utf-8")
            for skill_name in MANAGE_MODULE.HARDENED_CLIENT_HYBRID_SKILLS:
                self.assertIn(skill_name, skill_repos_content)
            self.assertEqual((private_client / "plans" / "INDEX.md").read_text(encoding="utf-8"), "# Custom plan index\n")
            self.assertEqual(
                (private_client / "workflows" / "INDEX.md").read_text(encoding="utf-8"),
                "# Custom workflow index\n",
            )
            self.assertIn("../_shared/skills", skill_repos_content)
            self.assertTrue((private_client.parent / "_shared" / "skills" / "domain-planner" / "SKILL.md").is_file())
            self.assertTrue((private_client / "skills" / "skill-issue" / "SKILL.md").is_file())

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
            shutil.copytree(repo / ".skillbox-state" / "clients" / "personal", private_clients / "personal")
            (private_clients / "personal" / "overlay.yaml").write_text(
                (repo / ".skillbox-state" / "clients" / "personal" / "overlay.yaml").read_text(encoding="utf-8"),
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

    def test_client_publish_with_acceptance_persists_acceptance_metadata(self) -> None:
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
                "--acceptance",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            publish_path = control_repo / "clients" / "personal" / "publish.json"
            acceptance_path = control_repo / "clients" / "personal" / "acceptance.json"

            self.assertTrue(payload["acceptance"]["present"])
            self.assertTrue(acceptance_path.is_file())

            acceptance_payload = json.loads(acceptance_path.read_text(encoding="utf-8"))
            self.assertEqual(acceptance_payload["client_id"], "personal")
            self.assertTrue(acceptance_payload["ready"])
            self.assertEqual(acceptance_payload["source_commit"], self._git_head(repo))
            self.assertEqual(acceptance_payload["payload_tree_sha256"], payload["payload_tree_sha256"])
            self.assertEqual(acceptance_payload["active_profiles"], ["core"])
            self.assertIn("skillbox", acceptance_payload["mcp_servers"])

            publish_payload = json.loads(publish_path.read_text(encoding="utf-8"))
            self.assertTrue(publish_payload["acceptance_present"])
            self.assertEqual(publish_payload["acceptance"], "clients/personal/acceptance.json")
            self.assertEqual(publish_payload["acceptance_source_commit"], self._git_head(repo))
            self.assertEqual(publish_payload["acceptance_profiles"], ["core"])

    def test_client_publish_with_acceptance_is_noop_when_payload_is_already_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._init_git_repo(repo)
            control_repo = self._create_git_source_repo(repo, "control-plane")

            first = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--acceptance",
                "--format",
                "json",
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            acceptance_path = control_repo / "clients" / "personal" / "acceptance.json"
            first_acceptance = acceptance_path.read_text(encoding="utf-8")

            second = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--acceptance",
                "--format",
                "json",
            )

            self.assertEqual(second.returncode, 0, second.stderr)
            payload = json.loads(second.stdout)
            self.assertFalse(payload["changed"])
            self.assertEqual(acceptance_path.read_text(encoding="utf-8"), first_acceptance)

    def test_client_publish_with_deploy_artifact_persists_release_metadata(self) -> None:
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
                "--acceptance",
                "--deploy-artifact",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            deploy_path = control_repo / "clients" / "personal" / "deploy.json"

            self.assertTrue(payload["deploy"]["present"])
            self.assertTrue(deploy_path.is_file())

            deploy_payload = json.loads(deploy_path.read_text(encoding="utf-8"))
            archive_path = control_repo / "clients" / "personal" / deploy_payload["archive"]
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            self.assertEqual(deploy_payload["client_id"], "personal")
            self.assertEqual(deploy_payload["source_commit"], self._git_head(repo))
            self.assertEqual(deploy_payload["payload_tree_sha256"], payload["payload_tree_sha256"])
            self.assertEqual(deploy_payload["archive_sha256"], archive_sha256)
            self.assertTrue(archive_path.is_file())

    def test_client_publish_with_deploy_artifact_is_noop_when_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._init_git_repo(repo)
            control_repo = self._create_git_source_repo(repo, "control-plane")

            first = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--deploy-artifact",
                "--format",
                "json",
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            deploy_path = control_repo / "clients" / "personal" / "deploy.json"
            first_deploy = deploy_path.read_bytes()
            first_payload = json.loads(first.stdout)
            first_archive = control_repo / Path(first_payload["deploy"]["archive"])
            first_archive_bytes = first_archive.read_bytes()

            second = self._run(
                repo,
                "client-publish",
                "personal",
                "--target-dir",
                "./fixtures/control-plane",
                "--deploy-artifact",
                "--format",
                "json",
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            second_payload = json.loads(second.stdout)

            self.assertFalse(second_payload["changed"])
            self.assertEqual(deploy_path.read_bytes(), first_deploy)
            self.assertEqual(first_archive.read_bytes(), first_archive_bytes)

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
            self.assertEqual(set(payload["mcp_servers"]), {"skillbox", "cm"})
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
            self.assertEqual(set(mcp_payload["mcpServers"]), {"skillbox", "cm"})

            focus_state = json.loads((repo / "workspace" / ".focus.json").read_text(encoding="utf-8"))
            self.assertEqual(focus_state["client_id"], "personal")

    def test_client_open_can_materialize_existing_bundle_without_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            project = self._run(repo, "client-project", "personal", "--format", "json")
            self.assertEqual(project.returncode, 0, project.stderr)

            result = self._run(
                repo,
                "client-open",
                "personal",
                "--from-bundle",
                "./builds/clients/personal",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            output_dir = repo / "sand" / "personal"
            self.assertEqual(payload["client_id"], "personal")
            self.assertEqual(payload["output_dir"], str(output_dir.resolve()))
            self.assertEqual(payload["active_profiles"], ["core"])
            self.assertEqual(set(payload["mcp_servers"]), {"skillbox", "cm"})
            self.assertEqual(payload["focus"]["status"], "skip")
            self.assertEqual(payload["focus"]["step_names"], [])
            self.assertTrue((output_dir / "CLAUDE.md").is_file())
            self.assertTrue((output_dir / "AGENTS.md").is_symlink())
            self.assertEqual(os.readlink(str(output_dir / "AGENTS.md")), "CLAUDE.md")
            self.assertTrue((output_dir / ".mcp.json").is_file())
            self.assertTrue((output_dir / "projection.json").is_file())
            self.assertTrue((output_dir / "runtime-model.json").is_file())
            self.assertTrue((output_dir / "workspace" / "clients" / "personal" / "overlay.yaml").is_file())
            self.assertFalse((repo / "workspace" / ".focus.json").exists())

            mcp_payload = json.loads((output_dir / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(set(mcp_payload["mcpServers"]), {"skillbox", "cm"})

    def test_client_open_from_bundle_supports_custom_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            project = self._run(
                repo,
                "client-project",
                "personal",
                "--profile",
                "connectors",
                "--output-dir",
                "./builds/clients/personal-connectors",
                "--format",
                "json",
            )
            self.assertEqual(project.returncode, 0, project.stderr)

            result = self._run(
                repo,
                "client-open",
                "personal",
                "--from-bundle",
                "./builds/clients/personal-connectors",
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
            self.assertEqual(set(payload["mcp_servers"]), {"skillbox", "cm", "fwc", "dcg"})
            self.assertEqual(payload["focus"]["status"], "skip")
            self.assertFalse((repo / "workspace" / ".focus.json").exists())

            mcp_payload = json.loads((output_dir / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(set(mcp_payload["mcpServers"]), {"skillbox", "cm", "fwc", "dcg"})

            runtime_model = json.loads((output_dir / "runtime-model.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime_model["active_profiles"], ["connectors", "core"])
            self.assertEqual(
                {service["id"] for service in runtime_model["services"]},
                {"internal-env-manager", "cm-mcp", "fwc-mcp", "dcg-mcp"},
            )

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
            self.assertEqual(set(payload["mcp_servers"]), {"skillbox", "cm", "fwc", "dcg"})

            mcp_payload = json.loads((output_dir / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(set(mcp_payload["mcpServers"]), {"skillbox", "cm", "fwc", "dcg"})

            runtime_model = json.loads((output_dir / "runtime-model.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime_model["active_profiles"], ["connectors", "core"])
            self.assertEqual(
                {service["id"] for service in runtime_model["services"]},
                {"internal-env-manager", "cm-mcp", "fwc-mcp", "dcg-mcp"},
            )

    def test_client_open_rejects_bundle_with_profile_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            project = self._run(repo, "client-project", "personal", "--format", "json")
            self.assertEqual(project.returncode, 0, project.stderr)

            result = self._run(
                repo,
                "client-open",
                "personal",
                "--from-bundle",
                "./builds/clients/personal",
                "--profile",
                "connectors",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("cannot combine --from-bundle with --profile", payload["error"]["message"])

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

    def test_client_open_rejects_bundle_for_the_wrong_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            project = self._run(repo, "client-project", "personal", "--format", "json")
            self.assertEqual(project.returncode, 0, project.stderr)

            result = self._run(
                repo,
                "client-open",
                "vibe-coding-client",
                "--from-bundle",
                "./builds/clients/personal",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("personal", payload["error"]["message"])
            self.assertIn("vibe-coding-client", payload["error"]["message"])
            self.assertFalse((repo / "sand" / "vibe-coding-client").exists())

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
            self.assertIn("use `gh-axi` for GitHub operations", content)
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

    def test_context_lists_installed_skill_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            self._run(repo, "sync", "--format", "json")
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
            (repo / ".skillbox-state" / "monoserver" / "new-project").mkdir(parents=True, exist_ok=True)

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
                self.assertIn(s["status"], ("ok", "skip", "warn"), f"step {s['step']} failed: {s}")

            # Verify overlay was created
            overlay = repo / ".skillbox-state" / "clients" / "new-project" / "overlay.yaml"
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
            (repo / ".skillbox-state" / "monoserver" / "acme-app").mkdir(parents=True, exist_ok=True)

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

            overlay = repo / ".skillbox-state" / "clients" / "dry-test" / "overlay.yaml"
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

    # -- First-box macro ------------------------------------------------------

    def test_first_box_completes_existing_personal_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)

            result = self._run(repo, "first-box", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            private_repo = workspace / "skillbox-config"
            output_dir = repo / "sand" / "personal"
            step_statuses = {step["step"]: step["status"] for step in payload["steps"]}

            self.assertEqual(payload["client_id"], "personal")
            self.assertFalse(payload["created_client"])
            self.assertEqual(payload["private_repo"]["target_dir"], str(private_repo.resolve()))
            self.assertEqual(
                [step["step"] for step in payload["steps"]],
                ["private-init", "onboard", "acceptance", "open"],
            )
            self.assertEqual(step_statuses["private-init"], "ok")
            self.assertEqual(step_statuses["onboard"], "skip")
            self.assertEqual(step_statuses["acceptance"], "ok")
            self.assertEqual(step_statuses["open"], "ok")

            env_text = (repo / ".env").read_text(encoding="utf-8")
            self.assertIn("SKILLBOX_CLIENTS_HOST_ROOT=../skillbox-config/clients", env_text)
            self.assertTrue((private_repo / ".git").exists())
            self.assertTrue((private_repo / "clients" / "personal" / "overlay.yaml").is_file())
            self.assertTrue((output_dir / "CLAUDE.md").is_file())
            self.assertTrue((output_dir / "AGENTS.md").is_symlink())
            self.assertTrue((output_dir / ".mcp.json").is_file())
            self.assertEqual(os.readlink(str(output_dir / "AGENTS.md")), "CLAUDE.md")

            mcp_payload = json.loads((output_dir / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(set(mcp_payload["mcpServers"]), {"skillbox", "cm"})

    def test_first_box_scaffolds_missing_client_with_blueprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
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
            (repo / ".skillbox-state" / "monoserver" / "acme-app").mkdir(parents=True, exist_ok=True)

            result = self._run(
                repo,
                "first-box",
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
            private_repo = workspace / "skillbox-config"
            output_dir = repo / "sand" / "acme-app"
            step_statuses = {step["step"]: step["status"] for step in payload["steps"]}

            self.assertTrue(payload["created_client"])
            self.assertEqual(step_statuses["private-init"], "ok")
            self.assertEqual(step_statuses["onboard"], "ok")
            self.assertEqual(step_statuses["acceptance"], "ok")
            self.assertEqual(step_statuses["open"], "ok")
            self.assertTrue((private_repo / "clients" / "acme-app" / "overlay.yaml").is_file())
            self.assertTrue((output_dir / "CLAUDE.md").is_file())
            self.assertTrue((output_dir / "workspace" / "clients" / "acme-app" / "overlay.yaml").is_file())

    def test_first_box_surfaces_blueprint_validation_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
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

            result = self._run(
                repo,
                "first-box",
                "acme-app",
                "--blueprint",
                "git-repo",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            step_statuses = {step["step"]: step["status"] for step in payload["steps"]}

            self.assertEqual(payload["error"]["type"], "missing_variable")
            self.assertTrue(payload["created_client"])
            self.assertEqual(step_statuses["private-init"], "ok")
            self.assertEqual(step_statuses["onboard"], "fail")
            self.assertEqual(step_statuses["acceptance"], "skip")
            self.assertEqual(step_statuses["open"], "skip")
            self.assertFalse((repo / "sand" / "acme-app").exists())
            self.assertTrue((workspace / "skillbox-config" / ".git").exists())

    def test_first_box_refuses_to_overwrite_unrelated_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)
            existing_dir = repo / "artifacts" / "existing-open"
            existing_dir.mkdir(parents=True, exist_ok=True)
            (existing_dir / "note.txt").write_text("keep me\n", encoding="utf-8")

            result = self._run(
                repo,
                "first-box",
                "personal",
                "--output-dir",
                "./artifacts/existing-open",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            step_statuses = {step["step"]: step["status"] for step in payload["steps"]}

            self.assertEqual(payload["error"]["type"], "conflict")
            self.assertEqual(step_statuses["private-init"], "ok")
            self.assertEqual(step_statuses["acceptance"], "ok")
            self.assertEqual(step_statuses["open"], "fail")
            self.assertEqual((existing_dir / "note.txt").read_text(encoding="utf-8"), "keep me\n")

    def test_first_box_runs_connectors_acceptance_and_open_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)
            self._set_client_connectors(repo, "personal", ["github", "slack"])
            self._write_connector_focus_artifacts(repo)
            self._install_absolute_mcp_stub(self._fixture_mcp_stub_path(repo, "fwc"), tool_names=["fwc_ping"])
            self._install_absolute_mcp_stub(self._fixture_mcp_stub_path(repo, "dcg"), tool_names=["dcg_ping"])
            self.addCleanup(self._run, repo, "down", "--profile", "connectors", "--format", "json")

            result = self._run(
                repo,
                "first-box",
                "personal",
                "--profile",
                "connectors",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            output_dir = repo / "sand" / "personal"

            self.assertEqual(payload["active_profiles"], ["connectors", "core"])
            self.assertEqual(set(payload["mcp_servers"]), {"skillbox", "cm", "fwc", "dcg"})
            mcp_payload = json.loads((output_dir / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(set(mcp_payload["mcpServers"]), {"skillbox", "cm", "fwc", "dcg"})

    def test_first_box_forwards_wait_seconds_to_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)

            seen_args: list[list[str]] = []

            def fake_run_manage_json_command(root_dir: Path, args: list[str]) -> tuple[int, dict[str, object]]:
                del root_dir
                seen_args.append(args)
                if args[0] == "acceptance":
                    return MANAGE_MODULE.EXIT_OK, {
                        "client_id": "personal",
                        "active_profiles": ["core"],
                        "ready": True,
                        "steps": [],
                    }
                if args[0] == "client-open":
                    return MANAGE_MODULE.EXIT_OK, {
                        "output_dir": str(repo / "sand" / "personal"),
                        "active_profiles": ["core"],
                        "mcp_servers": ["skillbox", "cm"],
                    }
                raise AssertionError(f"Unexpected command: {args}")

            with mock.patch.dict(
                MANAGE_MODULE.run_first_box.__globals__,
                {
                    "init_private_repo": lambda _root_dir, target_dir_arg=None: {
                        "target_dir": str((workspace / "skillbox-config").resolve()),
                        "clients_host_root": str((repo / ".skillbox-state" / "clients").resolve()),
                    },
                    "run_manage_json_command": fake_run_manage_json_command,
                    "emit_json": lambda _payload: None,
                },
            ):
                result = MANAGE_MODULE.run_first_box(
                    root_dir=repo,
                    client_id="personal",
                    private_path_arg=None,
                    profiles=[],
                    output_dir_arg=None,
                    label=None,
                    default_cwd=None,
                    root_path=None,
                    blueprint_name=None,
                    set_args=[],
                    force=False,
                    wait_seconds=42.0,
                    fmt="json",
                )

            self.assertEqual(result, MANAGE_MODULE.EXIT_OK)
            acceptance_args = next(args for args in seen_args if args[0] == "acceptance")
            self.assertEqual(
                acceptance_args,
                ["acceptance", "personal", "--wait-seconds", "42.0", "--format", "json"],
            )

    def test_first_box_fails_when_open_surface_is_missing_required_inner_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            repo = workspace / "skillbox"
            repo.mkdir(parents=True, exist_ok=True)
            self._write_fixture(repo)

            def fake_run_manage_json_command(root_dir: Path, args: list[str]) -> tuple[int, dict[str, object]]:
                del root_dir
                if args[0] == "acceptance":
                    return MANAGE_MODULE.EXIT_OK, {
                        "client_id": "personal",
                        "active_profiles": ["core"],
                        "ready": True,
                        "steps": [],
                    }
                if args[0] == "client-open":
                    return MANAGE_MODULE.EXIT_OK, {
                        "output_dir": str(repo / "sand" / "personal"),
                        "active_profiles": ["core"],
                        "mcp_servers": ["skillbox"],
                    }
                raise AssertionError(f"Unexpected command: {args}")

            captured_payloads: list[dict[str, object]] = []
            with mock.patch.dict(
                MANAGE_MODULE.run_first_box.__globals__,
                {
                    "init_private_repo": lambda _root_dir, target_dir_arg=None: {
                        "target_dir": str((workspace / "skillbox-config").resolve()),
                        "clients_host_root": str((repo / ".skillbox-state" / "clients").resolve()),
                    },
                    "run_manage_json_command": fake_run_manage_json_command,
                    "emit_json": captured_payloads.append,
                },
            ):
                result = MANAGE_MODULE.run_first_box(
                    root_dir=repo,
                    client_id="personal",
                    private_path_arg=None,
                    profiles=[],
                    output_dir_arg=None,
                    label=None,
                    default_cwd=None,
                    root_path=None,
                    blueprint_name=None,
                    set_args=[],
                    force=False,
                    wait_seconds=45.0,
                    fmt="json",
                )

            self.assertEqual(result, MANAGE_MODULE.EXIT_ERROR)
            payload = captured_payloads[0]
            self.assertEqual(payload["error"]["type"], "missing_mcp_surface")
            self.assertIn("cm", payload["error"]["message"])
            open_step = next(step for step in payload["steps"] if step["step"] == "open")
            self.assertEqual(open_step["status"], "fail")
            self.assertEqual(open_step["detail"]["missing_mcp_servers"], ["cm"])

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
        overlay_parent = clients_root or self._clients_host_root(repo)
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
            "      kind: skill-repo-set\n"
            "      required: false\n"
            "      profiles:\n"
            "        - core\n"
            f"      skill_repos_config: ${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skill-repos.yaml\n"
            f"      lock_path: ${{SKILLBOX_CLIENTS_ROOT}}/{client_id}/skill-repos.lock.json\n"
            "      clone_root: ${SKILLBOX_WORKSPACE_ROOT}/workspace/skill-repos\n"
            "      sync:\n"
            "        mode: clone-and-install\n"
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
        (overlay_dir / "bundles").mkdir(parents=True, exist_ok=True)
        (overlay_dir / "skills").mkdir(parents=True, exist_ok=True)

    def _set_client_connectors(self, repo: Path, client_id: str, connectors: list[str]) -> None:
        overlay_path = self._clients_host_root(repo) / client_id / "overlay.yaml"
        overlay_doc = MANAGE_MODULE.load_yaml(overlay_path)
        overlay_doc.setdefault("client", {})["connectors"] = connectors
        overlay_path.write_text(MANAGE_MODULE.render_yaml_document(overlay_doc), encoding="utf-8")

    def _set_client_acceptance_probe(
        self,
        repo: Path,
        client_id: str,
        *,
        command: list[str],
        cwd: str | None = None,
        profiles: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        overlay_path = self._clients_host_root(repo) / client_id / "overlay.yaml"
        overlay_doc = MANAGE_MODULE.load_yaml(overlay_path)
        probe: dict[str, object] = {"command": command}
        if cwd is not None:
            probe["cwd"] = cwd
        if profiles:
            probe["profiles"] = profiles
        if env:
            probe["env"] = env
        if timeout_seconds is not None:
            probe["timeout_seconds"] = timeout_seconds
        overlay_doc.setdefault("client", {})["acceptance_probe"] = probe
        overlay_path.write_text(MANAGE_MODULE.render_yaml_document(overlay_doc), encoding="utf-8")

    def _clients_host_root(self, repo: Path) -> Path:
        return repo / ".skillbox-state" / "clients"

    def _set_runtime_env_value(self, repo: Path, key: str, value: str) -> None:
        env_path = repo / ".env.example"
        lines = env_path.read_text(encoding="utf-8").splitlines()
        prefix = f"{key}="
        for index, line in enumerate(lines):
            if line.startswith(prefix):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_client_blueprint(self, repo: Path, name: str, content: str) -> None:
        blueprint_dir = repo / "workspace" / "client-blueprints"
        blueprint_dir.mkdir(parents=True, exist_ok=True)
        (blueprint_dir / f"{name}.yaml").write_text(content, encoding="utf-8")

    def _write_skill_builder_blueprint(self, repo: Path) -> None:
        self._write_client_blueprint(
            repo,
            "skill-builder-fwc",
            (ROOT_DIR / "workspace" / "client-blueprints" / "skill-builder-fwc.yaml").read_text(encoding="utf-8"),
        )

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
        (repo / "workspace").mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "persistence.yaml").write_text(
            "version: 1\n"
            "state_root_env: SKILLBOX_STATE_ROOT\n"
            "targets:\n"
            "  local:\n"
            "    provider: local\n"
            "    default_state_root: ./.skillbox-state\n"
            "  digitalocean:\n"
            "    provider: digitalocean\n"
            "    default_state_root: /srv/skillbox\n"
            "bindings:\n"
            "  - id: workspace-root\n"
            "    runtime_path: /workspace\n"
            "    storage_class: external\n"
            "    source_ref: root_dir\n"
            "  - id: claude-home\n"
            "    runtime_path: /home/sandbox/.claude\n"
            "    storage_class: persistent\n"
            "    relative_path: home/.claude\n"
            "  - id: codex-home\n"
            "    runtime_path: /home/sandbox/.codex\n"
            "    storage_class: persistent\n"
            "    relative_path: home/.codex\n"
            "  - id: local-home\n"
            "    runtime_path: /home/sandbox/.local\n"
            "    storage_class: persistent\n"
            "    relative_path: home/.local\n"
            "  - id: clients-root\n"
            "    runtime_path: /workspace/workspace/clients\n"
            "    storage_class: persistent\n"
            "    relative_path: clients\n"
            "  - id: logs-root\n"
            "    runtime_path: /workspace/logs\n"
            "    storage_class: persistent\n"
            "    relative_path: logs\n"
            "  - id: monoserver-root\n"
            "    runtime_path: /monoserver\n"
            "    storage_class: persistent\n"
            "    relative_path: monoserver\n",
            encoding="utf-8",
        )
        (repo / ".env.example").write_text(
            "SKILLBOX_NAME=skillbox\n"
            "SKILLBOX_STORAGE_PROVIDER=local\n"
            "SKILLBOX_STATE_ROOT=./.skillbox-state\n"
            "SKILLBOX_STORAGE_FILESYSTEM=\n"
            "SKILLBOX_STORAGE_REQUIRED=false\n"
            "SKILLBOX_STORAGE_MIN_FREE_GB=0\n"
            "SKILLBOX_WORKSPACE_ROOT=/workspace\n"
            "SKILLBOX_REPOS_ROOT=/workspace/repos\n"
            "SKILLBOX_SKILLS_ROOT=/workspace/skills\n"
            "SKILLBOX_LOG_ROOT=/workspace/logs\n"
            "SKILLBOX_HOME_ROOT=/home/sandbox\n"
            "SKILLBOX_MONOSERVER_ROOT=/monoserver\n"
            "SKILLBOX_CLIENTS_ROOT=/workspace/workspace/clients\n"
            "SKILLBOX_CLIENTS_HOST_ROOT=./.skillbox-state/clients\n"
            "SKILLBOX_MONOSERVER_HOST_ROOT=./.skillbox-state/monoserver\n"
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
            "SKILLBOX_CASS_BIN=/home/sandbox/.local/bin/cass\n"
            "SKILLBOX_CASS_DOWNLOAD_URL=\n"
            "SKILLBOX_CASS_DOWNLOAD_SHA256=\n"
            "SKILLBOX_APR_BIN=/home/sandbox/.local/bin/apr\n"
            "SKILLBOX_APR_DOWNLOAD_URL=\n"
            "SKILLBOX_APR_DOWNLOAD_SHA256=\n"
            "SKILLBOX_CM_BIN=/home/sandbox/.local/bin/cm\n"
            "SKILLBOX_CM_DOWNLOAD_URL=\n"
            "SKILLBOX_CM_DOWNLOAD_SHA256=\n"
            "SKILLBOX_CM_MCP_PORT=3222\n"
            "SKILLBOX_FWC_BIN=/home/sandbox/.local/bin/fwc\n"
            "SKILLBOX_FWC_DOWNLOAD_URL=\n"
            "SKILLBOX_FWC_DOWNLOAD_SHA256=\n"
            "SKILLBOX_FWC_MCP_PORT=3221\n"
            "SKILLBOX_FWC_ZONE=work\n"
            "SKILLBOX_FWC_CONNECTORS=github,slack\n"
            "SKILLBOX_PULSE_INTERVAL=30\n"
            "SKILLBOX_PULSE_CLIENTS=\n"
            "SKILLBOX_PULSE_PROFILES=\n"
            "SKILLBOX_PULSE_UNHEALTHY_GRACE_SECONDS=60\n",
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
            "    - id: cass-bin\n"
            "      kind: binary\n"
            "      path: ${SKILLBOX_CASS_BIN}\n"
            "      required: false\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: file\n"
            "        path: ./artifacts/cass.bin\n"
            "        executable: true\n"
            "      sync:\n"
            "        mode: copy-if-missing\n"
            "    - id: cm-bin\n"
            "      kind: binary\n"
            "      path: ${SKILLBOX_CM_BIN}\n"
            "      required: false\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: file\n"
            "        path: ./artifacts/cm.bin\n"
            "        executable: true\n"
            "      sync:\n"
            "        mode: copy-if-missing\n"
            "  skills:\n"
            "    - id: default-skills\n"
            "      kind: skill-repo-set\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      skill_repos_config: ${SKILLBOX_WORKSPACE_ROOT}/workspace/skill-repos.yaml\n"
            "      lock_path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/skill-repos.lock.json\n"
            "      clone_root: ${SKILLBOX_WORKSPACE_ROOT}/workspace/skill-repos\n"
            "      sync:\n"
            "        mode: clone-and-install\n"
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
            "    - id: cm-mcp\n"
            "      kind: mcp\n"
            "      artifact: cm-bin\n"
            "      required: false\n"
            "      profiles:\n"
            "        - core\n"
            "      command: ${SKILLBOX_CM_BIN} serve --port ${SKILLBOX_CM_MCP_PORT}\n"
            "      env:\n"
            "        CASS_MEMORY_LLM: none\n"
            "        CASS_PATH: ${SKILLBOX_CASS_BIN}\n"
            "      healthcheck:\n"
            "        type: http\n"
            "        url: http://127.0.0.1:${SKILLBOX_CM_MCP_PORT}/health\n"
            "        timeout_seconds: 0.5\n"
            "      depends_on:\n"
            "        - cass-bin\n"
            "        - cm-bin\n"
            "      log: runtime\n"
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
            "  artifacts:\n"
            "    - id: fwc-bin\n"
            "      kind: binary\n"
            "      path: ${SKILLBOX_FWC_BIN}\n"
            "      required: false\n"
            "      profiles:\n"
            "        - connectors\n"
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
            "      profiles:\n"
            "        - connectors\n"
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
            "      profiles:\n"
            "        - connectors\n"
            "      command: ${SKILLBOX_FWC_BIN} serve-mcp --zone ${SKILLBOX_FWC_ZONE} --connectors ${SKILLBOX_FWC_CONNECTORS}\n"
            "      healthcheck:\n"
            "        type: process_running\n"
            "        pattern: fwc serve-mcp\n"
            "      log: connectors\n"
            "    - id: dcg-mcp\n"
            "      kind: mcp\n"
            "      artifact: dcg-bin\n"
            "      required: false\n"
            "      profiles:\n"
            "        - connectors\n"
            "      command: ${SKILLBOX_DCG_BIN} mcp\n"
            "      healthcheck:\n"
            "        type: process_running\n"
            "        pattern: dcg mcp\n"
            "      log: connectors\n"
            "  logs:\n"
            "    - id: connectors\n"
            "      path: ${SKILLBOX_LOG_ROOT}/connectors\n"
            "      profiles:\n"
            "        - connectors\n"
            "  checks:\n"
            "    - id: fwc-binary\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_FWC_BIN}\n"
            "      required: false\n"
            "      profiles:\n"
            "        - connectors\n"
            "    - id: dcg-binary\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_DCG_BIN}\n"
            "      required: false\n"
            "      profiles:\n"
            "        - connectors\n"
            "connectors-dev:\n"
            "  repos:\n"
            "    - id: flywheel-connectors\n"
            "      kind: repo\n"
            "      path: ${SKILLBOX_REPOS_ROOT}/flywheel_connectors\n"
            "      required: false\n"
            "      profiles:\n"
            "        - connectors-dev\n"
            "      source:\n"
            "        kind: directory\n"
            "      sync:\n"
            "        mode: ensure-directory\n"
            "    - id: destructive-command-guard\n"
            "      kind: repo\n"
            "      path: ${SKILLBOX_REPOS_ROOT}/destructive_command_guard\n"
            "      required: false\n"
            "      profiles:\n"
            "        - connectors-dev\n"
            "      source:\n"
            "        kind: directory\n"
            "      sync:\n"
            "        mode: ensure-directory\n",
            encoding="utf-8",
        )
        (repo / "artifacts").mkdir(parents=True, exist_ok=True)
        (repo / "artifacts" / "swimmers.bin").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (repo / "artifacts" / "cass.bin").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (repo / "artifacts" / "cm.bin").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (repo / "artifacts" / "fwc.bin").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (repo / "artifacts" / "dcg.bin").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

        (repo / "workspace" / "skill-repos.yaml").write_text(
            "version: 2\n"
            "skill_repos:\n"
            "  - path: ../skills\n"
            "    pick: [sample-skill]\n",
            encoding="utf-8",
        )
        clients_root = self._clients_host_root(repo)
        (clients_root / "personal").mkdir(parents=True, exist_ok=True)
        (clients_root / "personal" / "skill-repos.yaml").write_text(
            "version: 2\n"
            "skill_repos:\n"
            "  - path: ./skills\n"
            "    pick: [personal-skill]\n",
            encoding="utf-8",
        )
        (clients_root / "vibe-coding-client").mkdir(parents=True, exist_ok=True)
        (clients_root / "vibe-coding-client" / "skill-repos.yaml").write_text(
            "version: 2\n"
            "skill_repos:\n"
            "  - path: ./skills\n",
            encoding="utf-8",
        )
        self._write_client_overlay(
            repo,
            "personal",
            label="Personal",
            default_cwd="${SKILLBOX_MONOSERVER_ROOT}",
            root_path="${SKILLBOX_MONOSERVER_ROOT}",
            clients_root=clients_root,
        )
        self._write_client_overlay(
            repo,
            "vibe-coding-client",
            label="Vibe Coding Client",
            default_cwd="${SKILLBOX_MONOSERVER_ROOT}/vibe-coding-client",
            root_path="${SKILLBOX_MONOSERVER_ROOT}/vibe-coding-client",
            clients_root=clients_root,
        )

        (clients_root / "personal" / "skills").mkdir(parents=True, exist_ok=True)
        (clients_root / "vibe-coding-client" / "skills").mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "skill-repos").mkdir(parents=True, exist_ok=True)

        self._write_skill_dir(repo / "skills" / "sample-skill", "sample-skill")
        self._write_skill_dir(
            clients_root / "personal" / "skills" / "personal-skill",
            "personal-skill",
        )

        (repo / "skills").mkdir(parents=True, exist_ok=True)
        (repo / ".skillbox-state" / "logs").mkdir(parents=True, exist_ok=True)
        (repo / "repos").mkdir(parents=True, exist_ok=True)
        (repo / "home" / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / "home" / ".codex").mkdir(parents=True, exist_ok=True)
        (repo / ".skillbox-state" / "monoserver").mkdir(parents=True, exist_ok=True)
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
                        "cm": {
                            "command": str((repo / ".mcp-bin" / "cm").resolve()),
                            "args": ["serve", "--port", "3222"],
                            "env": {
                                "CASS_MEMORY_LLM": "none",
                                "CASS_PATH": "/home/sandbox/.local/bin/cass",
                            },
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
        self._install_absolute_mcp_stub(self._fixture_mcp_stub_path(repo, "cm"), tool_names=["memory_search"])

    def _ingress_model(self, repo: Path) -> dict:
        return {
            "root_dir": str(repo),
            "env": {
                "SKILLBOX_WORKSPACE_ROOT": "/workspace",
                "SKILLBOX_LOG_ROOT": "/workspace/logs",
                "SKILLBOX_HOME_ROOT": "/home/sandbox",
                "SKILLBOX_MONOSERVER_ROOT": "/monoserver",
                "SKILLBOX_CLIENTS_ROOT": "/workspace/workspace/clients",
                "SKILLBOX_INGRESS_PUBLIC_HOST": "127.0.0.1",
                "SKILLBOX_INGRESS_PUBLIC_PORT": "8080",
                "SKILLBOX_INGRESS_PUBLIC_BASE_URL": "https://reports.example.test",
                "SKILLBOX_INGRESS_PRIVATE_HOST": "127.0.0.1",
                "SKILLBOX_INGRESS_PRIVATE_PORT": "9080",
                "SKILLBOX_INGRESS_PRIVATE_BASE_URL": "http://tailnet.example.test:9080",
                "SKILLBOX_INGRESS_ROUTE_FILE": "/workspace/logs/runtime/ingress-routes.json",
                "SKILLBOX_INGRESS_NGINX_CONFIG": "/workspace/logs/runtime/ingress-nginx.conf",
            },
            "storage": None,
            "logs": [],
            "services": [
                {
                    "id": "backend",
                    "kind": "http",
                    "origin_url": "http://127.0.0.1:9100",
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:8001/v1/reports"},
                }
            ],
            "ingress_routes": [
                {
                    "id": "report-command",
                    "service_id": "backend",
                    "listener": "public",
                    "path": "/v1/report",
                    "match": "exact",
                    "client": "jeremy",
                    "profiles": ["local-ecom"],
                }
            ],
            "repos": [],
            "artifacts": [],
            "env_files": [],
            "skills": [],
            "tasks": [],
            "checks": [],
            "clients": [],
            "active_clients": ["jeremy"],
            "active_profiles": ["core", "local-ecom"],
        }

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

    def _write_skill_dir(self, skill_dir: Path, skill_name: str) -> None:
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            f"name: {skill_name}\n"
            f"description: Fixture skill {skill_name} for runtime manager tests.\n"
            "---\n\n"
            "# Sample Skill\n",
            encoding="utf-8",
        )
        refs_dir = skill_dir / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)
        (refs_dir / "overview.md").write_text("fixture reference\n", encoding="utf-8")

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

    def test_session_start_creates_durable_session_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "session-start",
                "personal",
                "--label",
                "Tutoring run",
                "--cwd",
                "/monoserver/personal-app",
                "--goal",
                "Ship the next slice",
                "--actor",
                "coach",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            session = payload["session"]
            session_id = session["session_id"]
            session_dir = repo / ".skillbox-state" / "logs" / "clients" / "personal" / "sessions" / session_id

            self.assertEqual(payload["client_id"], "personal")
            self.assertEqual(session["status"], "active")
            self.assertEqual(session["label"], "Tutoring run")
            self.assertEqual(session["cwd"], "/monoserver/personal-app")
            self.assertEqual(session["goal"], "Ship the next slice")
            self.assertTrue((session_dir / "meta.json").is_file())
            self.assertTrue((session_dir / "events.jsonl").is_file())
            self.assertTrue((session_dir / "handoff.md").is_file())

            meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["session_id"], session_id)
            self.assertEqual(meta["event_count"], 1)
            self.assertEqual(meta["last_event_type"], "session.started")
            self.assertNotIn("paths", meta)
            self.assertNotIn("recent_events", meta)

            events = (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 1)
            started_event = json.loads(events[0])
            self.assertEqual(started_event["type"], "session.started")
            self.assertEqual(started_event["detail"]["actor"], "coach")

    def test_session_start_merges_mcp_event_context_into_durable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            env = os.environ.copy()
            env["SKILLBOX_MCP_EVENT_CONTEXT"] = json.dumps(
                {
                    "mcp_request_id": "req-42",
                    "mcp_tool_name": "skillbox_session_start",
                }
            )

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(repo),
                    "session-start",
                    "personal",
                    "--actor",
                    "coach",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            session_id = payload["session"]["session_id"]
            session_dir = repo / ".skillbox-state" / "logs" / "clients" / "personal" / "sessions" / session_id

            started_event = json.loads(
                (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(started_event["detail"]["mcp_request_id"], "req-42")
            self.assertEqual(started_event["detail"]["mcp_tool_name"], "skillbox_session_start")

            runtime_log = (repo / "logs" / "runtime" / "runtime.log").read_text(encoding="utf-8")
            self.assertIn('"mcp_request_id":"req-42"', runtime_log)
            self.assertIn('"mcp_tool_name":"skillbox_session_start"', runtime_log)

    def test_session_event_appends_and_updates_heartbeat_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            started = self._run(repo, "session-start", "personal", "--format", "json")
            session_id = json.loads(started.stdout)["session"]["session_id"]

            result = self._run(
                repo,
                "session-event",
                "personal",
                "--session-id",
                session_id,
                "--event-type",
                "note",
                "--message",
                "Student is taking over implementation",
                "--actor",
                "coach",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            session = payload["session"]
            self.assertEqual(session["last_event_type"], "session.note")
            self.assertEqual(session["last_message"], "Student is taking over implementation")
            self.assertEqual(session["event_count"], 2)

            meta_path = repo / ".skillbox-state" / "logs" / "clients" / "personal" / "sessions" / session_id / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["last_event_type"], "session.note")
            self.assertEqual(meta["last_message"], "Student is taking over implementation")
            self.assertGreaterEqual(meta["last_heartbeat_at"], meta["started_at"])
            self.assertNotIn("paths", meta)
            self.assertNotIn("recent_events", meta)

    def test_session_end_and_resume_transition_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            started = self._run(repo, "session-start", "personal", "--format", "json")
            session_id = json.loads(started.stdout)["session"]["session_id"]

            ended = self._run(
                repo,
                "session-end",
                "personal",
                "--session-id",
                session_id,
                "--status",
                "failed",
                "--summary",
                "Container crashed mid-run",
                "--format",
                "json",
            )
            self.assertEqual(ended.returncode, 0, ended.stderr)
            ended_payload = json.loads(ended.stdout)
            self.assertEqual(ended_payload["session"]["status"], "failed")
            self.assertEqual(ended_payload["session"]["summary"], "Container crashed mid-run")

            resumed = self._run(
                repo,
                "session-resume",
                "personal",
                "--session-id",
                session_id,
                "--actor",
                "coach",
                "--message",
                "Recovered after crash",
                "--format",
                "json",
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resumed_payload = json.loads(resumed.stdout)
            self.assertEqual(resumed_payload["session"]["status"], "active")
            self.assertEqual(resumed_payload["session"]["resume_count"], 1)
            self.assertEqual(resumed_payload["session"]["last_event_type"], "session.resumed")
            self.assertEqual(resumed_payload["session"]["last_message"], "Recovered after crash")

    def test_session_status_lists_recent_sessions_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            first = self._run(repo, "session-start", "personal", "--label", "First", "--format", "json")
            first_id = json.loads(first.stdout)["session"]["session_id"]
            second = self._run(repo, "session-start", "personal", "--label", "Second", "--format", "json")
            second_id = json.loads(second.stdout)["session"]["session_id"]

            result = self._run(repo, "session-status", "personal", "--limit", "2", "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)

            self.assertEqual(payload["count"], 2)
            self.assertEqual([item["session_id"] for item in payload["sessions"]], [second_id, first_id])
            self.assertEqual(payload["sessions"][0]["label"], "Second")

    def test_session_event_errors_for_missing_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(
                repo,
                "session-event",
                "personal",
                "--session-id",
                "missing-session",
                "--event-type",
                "note",
                "--message",
                "noop",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "session_not_found")

    def test_session_end_errors_when_session_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            started = self._run(repo, "session-start", "personal", "--format", "json")
            session_id = json.loads(started.stdout)["session"]["session_id"]
            ended = self._run(repo, "session-end", "personal", "--session-id", session_id, "--format", "json")
            self.assertEqual(ended.returncode, 0, ended.stderr)

            again = self._run(repo, "session-end", "personal", "--session-id", session_id, "--format", "json")
            self.assertEqual(again.returncode, 1, again.stderr)
            payload = json.loads(again.stdout)
            self.assertEqual(payload["error"]["type"], "session_state_conflict")

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
            self.assertIn("use `gh-axi` for GitHub operations", claude_md)
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
            (external_clients / "personal" / "skill-repos.yaml").write_text(
                "version: 2\n"
                "skill_repos: []\n",
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

    def test_focus_skill_context_mangling_urls_or_tilde_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            overlay_path = self._clients_host_root(repo) / "personal" / "overlay.yaml"
            overlay_doc = MANAGE_MODULE.load_yaml(overlay_path)
            overlay_doc.setdefault("client", {})["context"] = {
                "cwd_match": [
                    "repos/sweet-potato",
                    "~/repos/opensource",
                    "/monoserver",
                ],
                "plans": {
                    "plan_root": "plans/released",
                    "plan_index": "plans/INDEX.md",
                },
                "workflow_builder": {
                    "workflow_index": "workflows/INDEX.md",
                },
                "domains": {
                    "frontends": {
                        "buildooor": {
                            "local": "http://localhost:3000",
                            "repo_slug": "build000r/buildooor",
                        }
                    }
                },
                "deploy": {
                    "legacy_ssh_key": "~/.ssh/do_terra_cc",
                },
            }
            overlay_path.write_text(
                MANAGE_MODULE.render_yaml_document(overlay_doc),
                encoding="utf-8",
            )

            result = self._run(repo, "focus", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            client_dir = self._clients_host_root(repo) / "personal"
            context_doc = MANAGE_MODULE.load_yaml(client_dir / "context.yaml")
            self.assertEqual(
                Path(str(context_doc["cwd_match"][0])).resolve(),
                (client_dir / "repos/sweet-potato").resolve(),
            )
            self.assertEqual(context_doc["cwd_match"][1], "~/repos/opensource")
            self.assertEqual(context_doc["cwd_match"][2], "/monoserver")
            self.assertEqual(
                Path(str(context_doc["plans"]["plan_root"])).resolve(),
                (client_dir / "plans/released").resolve(),
            )
            self.assertEqual(
                Path(str(context_doc["plans"]["plan_index"])).resolve(),
                (client_dir / "plans/INDEX.md").resolve(),
            )
            self.assertEqual(
                Path(str(context_doc["workflow_builder"]["workflow_index"])).resolve(),
                (client_dir / "workflows/INDEX.md").resolve(),
            )
            self.assertEqual(
                context_doc["domains"]["frontends"]["buildooor"]["local"],
                "http://localhost:3000",
            )
            self.assertEqual(
                context_doc["domains"]["frontends"]["buildooor"]["repo_slug"],
                "build000r/buildooor",
            )
            self.assertEqual(
                context_doc["deploy"]["legacy_ssh_key"],
                "~/.ssh/do_terra_cc",
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

            # Remove the persistent monoserver root so the personal-root check fails
            import shutil
            monoserver = repo / ".skillbox-state" / "monoserver"
            if monoserver.exists():
                shutil.rmtree(monoserver)

            result = self._run(repo, "focus", "personal", "--format", "json")

            # Should still complete (checks fail but focus continues)
            payload = json.loads(result.stdout)
            claude_md = (repo / "home" / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("## Attention", claude_md)
            self.assertIn("CHECK FAIL", claude_md)

    def test_focus_fails_before_bootstrap_when_post_sync_doctor_has_fatal_task_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            self._write_client_overlay(
                repo,
                "acme-runtime",
                label="Acme Runtime",
                default_cwd="${SKILLBOX_MONOSERVER_ROOT}/acme-runtime",
                root_path="${SKILLBOX_MONOSERVER_ROOT}/acme-runtime",
                include_context=True,
            )
            (repo / ".skillbox-state" / "monoserver" / "acme-runtime").mkdir(parents=True, exist_ok=True)
            (self._clients_host_root(repo) / "acme-runtime" / "skill-repos.yaml").write_text(
                "version: 2\nskill_repos: []\n",
                encoding="utf-8",
            )

            overlay_path = self._clients_host_root(repo) / "acme-runtime" / "overlay.yaml"
            overlay_doc = MANAGE_MODULE.load_yaml(overlay_path)
            client_doc = overlay_doc["client"]
            client_doc["repos"] = [
                {
                    "id": "app",
                    "kind": "repo",
                    "path": "${SKILLBOX_MONOSERVER_ROOT}/acme-runtime/app",
                    "repo_path": "${SKILLBOX_MONOSERVER_ROOT}/acme-runtime/app",
                    "required": True,
                    "profiles": ["core"],
                    "source": {"kind": "directory"},
                    "sync": {"mode": "ensure-directory"},
                }
            ]
            client_doc["tasks"] = [
                {
                    "id": "app-bootstrap",
                    "kind": "bootstrap",
                    "repo_id": "app",
                    "log": "acme-runtime",
                    "profiles": ["core"],
                    "command": "touch .bootstrap.ok",
                    "success": {
                        "type": "path_exists",
                        "path": "${SKILLBOX_MONOSERVER_ROOT}/acme-runtime/app/.bootstrap.ok",
                    },
                }
            ]
            client_doc.setdefault("checks", []).append(
                {
                    "id": "app-package-json",
                    "type": "path_exists",
                    "path": "${SKILLBOX_MONOSERVER_ROOT}/acme-runtime/app/package.json",
                    "required": True,
                    "profiles": ["core"],
                }
            )
            overlay_path.write_text(MANAGE_MODULE.render_yaml_document(overlay_doc), encoding="utf-8")

            result = self._run(repo, "focus", "acme-runtime", "--format", "json")

            self.assertEqual(result.returncode, 1, result.stdout)
            payload = json.loads(result.stdout)
            steps = {step["step"]: step for step in payload["steps"]}
            self.assertEqual(steps["sync"]["status"], "ok")
            self.assertEqual(steps["bootstrap"]["status"], "fail")
            self.assertEqual(payload["error"]["type"], "pre_bootstrap_doctor_failed")
            failing_codes = {
                check["code"]
                for check in steps["bootstrap"]["detail"]["checks"]
                if check["status"] == "fail"
            }
            self.assertIn("required-runtime-checks", failing_codes)
            self.assertFalse((repo / "workspace" / ".focus.json").exists())

    def test_focus_detects_recent_errors_in_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._run(repo, "sync", "--client", "personal")

            # Write a fake log file with errors
            log_dir = repo / ".skillbox-state" / "logs" / "runtime"
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

    def test_focus_live_context_includes_sessions_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            started = self._run(
                repo,
                "session-start",
                "personal",
                "--label",
                "Tutor handoff",
                "--goal",
                "Leave the student to vibe code safely",
                "--format",
                "json",
            )
            session_id = json.loads(started.stdout)["session"]["session_id"]
            self._run(
                repo,
                "session-event",
                "personal",
                "--session-id",
                session_id,
                "--event-type",
                "note",
                "--message",
                "Waiting on student changes",
                "--format",
                "json",
            )

            result = self._run(repo, "focus", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("sessions", payload["live_state"])
            self.assertTrue(payload["live_state"]["sessions"])
            self.assertEqual(payload["live_state"]["sessions"][0]["session_id"], session_id)

            claude_md = (repo / "home" / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("## Sessions", claude_md)
            self.assertIn("Tutor handoff", claude_md)
            self.assertIn("session.note", claude_md)

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
                {"internal-env-manager", "cm-mcp", "fwc-mcp", "dcg-mcp"},
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

    def test_acceptance_forwards_wait_seconds_to_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            captured_payloads: list[dict[str, object]] = []
            seen_args: list[list[str]] = []

            def fake_run_manage_json_command(root_dir: Path, args: list[str]) -> tuple[int, dict[str, object]]:
                del root_dir
                seen_args.append(list(args))
                command = args[0]
                if command == "doctor":
                    return MANAGE_MODULE.EXIT_OK, {"checks": []}
                if command == "sync":
                    return MANAGE_MODULE.EXIT_OK, {"actions": []}
                if command == "focus":
                    return MANAGE_MODULE.EXIT_OK, {"live_state": {"services": []}, "steps": []}
                raise AssertionError(f"Unexpected command: {args}")

            with mock.patch.dict(
                MANAGE_MODULE.run_acceptance.__globals__,
                {
                    "run_manage_json_command": fake_run_manage_json_command,
                    "smoke_requested_mcp_servers": lambda _root_dir, _model: (
                        True,
                        {"servers": {}, "servers_ok": ["skillbox"], "servers_failed": []},
                        [],
                    ),
                    "build_runtime_model": lambda _root_dir: {"clients": []},
                    "filter_model": lambda model, _profiles, _clients: model,
                    "normalize_active_clients": lambda _model, clients: clients,
                    "emit_json": captured_payloads.append,
                },
            ):
                result = MANAGE_MODULE.run_acceptance(
                    root_dir=repo,
                    client_id="personal",
                    profiles=[],
                    wait_seconds=42.5,
                    fmt="json",
                )

            self.assertEqual(result, MANAGE_MODULE.EXIT_OK)
            focus_args = next(args for args in seen_args if args[0] == "focus")
            self.assertEqual(focus_args, ["focus", "personal", "--wait-seconds", "42.5", "--format", "json"])
            self.assertTrue(captured_payloads[0]["ready"])

    def test_acceptance_succeeds_only_when_connector_mcp_surfaces_are_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._set_client_connectors(repo, "personal", ["github", "slack"])
            self._write_connector_focus_artifacts(repo)
            self._install_absolute_mcp_stub(self._fixture_mcp_stub_path(repo, "fwc"), tool_names=["fwc_ping"])
            self._install_absolute_mcp_stub(self._fixture_mcp_stub_path(repo, "dcg"), tool_names=["dcg_ping"])
            self.addCleanup(self._run, repo, "down", "--profile", "connectors", "--format", "json")

            result = self._run(repo, "acceptance", "personal", "--profile", "connectors", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["active_profiles"], ["connectors", "core"])
            self.assertEqual(payload["steps"][3]["detail"]["servers_ok"], ["skillbox", "cm", "fwc", "dcg"])
            self.assertEqual(
                set(payload["steps"][2]["detail"]["services"]),
                {"internal-env-manager", "cm-mcp", "fwc-mcp", "dcg-mcp"},
            )

    def test_acceptance_runs_configured_workflow_probe_after_mcp_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            probe_script = repo / "acceptance_probe.py"
            probe_script.write_text(
                "from __future__ import annotations\n"
                "\n"
                "import json\n"
                "import os\n"
                "from pathlib import Path\n"
                "\n"
                "payload = {\n"
                "    'cwd': os.getcwd(),\n"
                "    'client_id': os.environ.get('SKILLBOX_ACCEPTANCE_CLIENT_ID'),\n"
                "    'profiles': os.environ.get('SKILLBOX_ACCEPTANCE_PROFILES'),\n"
                "}\n"
                "Path(os.environ['PROBE_OUTPUT_PATH']).write_text(json.dumps(payload), encoding='utf-8')\n",
                encoding="utf-8",
            )
            probe_output = repo / "workspace" / "probe-output.json"
            self._set_client_acceptance_probe(
                repo,
                "personal",
                command=["python3", "${ROOT_DIR}/acceptance_probe.py"],
                cwd="${ROOT_DIR}/workspace",
                env={"PROBE_OUTPUT_PATH": "${ROOT_DIR}/workspace/probe-output.json"},
            )

            result = self._run(repo, "acceptance", "personal", "--wait-seconds", "17.5", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            step_names = [step["step"] for step in payload["steps"]]
            self.assertTrue(payload["ready"])
            self.assertEqual(
                step_names,
                ["doctor-pre", "sync", "focus", "mcp-smoke", "workflow-probe", "doctor-post"],
            )
            probe_step = payload["steps"][4]
            self.assertEqual(probe_step["status"], "ok")
            self.assertEqual(probe_step["detail"]["cwd"], str((repo / "workspace").resolve()))
            probe_payload = json.loads(probe_output.read_text(encoding="utf-8"))
            self.assertEqual(probe_payload["cwd"], str((repo / "workspace").resolve()))
            self.assertEqual(probe_payload["client_id"], "personal")
            self.assertEqual(probe_payload["profiles"], "core")

    def test_acceptance_translates_runtime_paths_before_running_workflow_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            probe_script = self._clients_host_root(repo) / "personal" / "scripts" / "acceptance_probe.py"
            probe_script.parent.mkdir(parents=True, exist_ok=True)
            probe_script.write_text(
                "from __future__ import annotations\n"
                "\n"
                "import json\n"
                "import os\n"
                "from pathlib import Path\n"
                "\n"
                "payload = {\n"
                "    'cwd': os.getcwd(),\n"
                "    'client_id': os.environ.get('SKILLBOX_ACCEPTANCE_CLIENT_ID'),\n"
                "    'profiles': os.environ.get('SKILLBOX_ACCEPTANCE_PROFILES'),\n"
                "}\n"
                "Path(os.environ['PROBE_OUTPUT_PATH']).write_text(json.dumps(payload), encoding='utf-8')\n",
                encoding="utf-8",
            )
            probe_output = repo / ".skillbox-state" / "monoserver" / "probe-output.json"
            self._set_client_acceptance_probe(
                repo,
                "personal",
                command=["python3", "${SKILLBOX_CLIENTS_ROOT}/personal/scripts/acceptance_probe.py"],
                cwd="${SKILLBOX_MONOSERVER_ROOT}",
                env={"PROBE_OUTPUT_PATH": "${SKILLBOX_MONOSERVER_ROOT}/probe-output.json"},
            )

            result = self._run(repo, "acceptance", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            steps = {step["step"]: step for step in payload["steps"]}
            self.assertTrue(payload["ready"])
            self.assertEqual(steps["workflow-probe"]["status"], "ok")
            self.assertEqual(
                steps["workflow-probe"]["detail"]["cwd"],
                str((repo / ".skillbox-state" / "monoserver").resolve()),
            )
            self.assertEqual(
                steps["workflow-probe"]["detail"]["command"][1],
                str(probe_script.resolve()),
            )
            probe_payload = json.loads(probe_output.read_text(encoding="utf-8"))
            self.assertEqual(probe_payload["cwd"], str((repo / ".skillbox-state" / "monoserver").resolve()))
            self.assertEqual(probe_payload["client_id"], "personal")
            self.assertEqual(probe_payload["profiles"], "core")

    def test_acceptance_exposes_runtime_ingress_env_to_workflow_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            (repo / ".env").write_text(
                "SKILLBOX_INGRESS_PUBLIC_BASE_URL=https://reports.example.test\n"
                "SKILLBOX_INGRESS_PUBLIC_HOST=127.0.0.1\n"
                "SKILLBOX_INGRESS_PUBLIC_PORT=8443\n",
                encoding="utf-8",
            )

            probe_script = repo / "acceptance_probe_env.py"
            probe_script.write_text(
                "from __future__ import annotations\n"
                "\n"
                "import json\n"
                "import os\n"
                "from pathlib import Path\n"
                "\n"
                "payload = {\n"
                "    'public_base_url': os.environ.get('SKILLBOX_INGRESS_PUBLIC_BASE_URL'),\n"
                "    'public_host': os.environ.get('SKILLBOX_INGRESS_PUBLIC_HOST'),\n"
                "    'public_port': os.environ.get('SKILLBOX_INGRESS_PUBLIC_PORT'),\n"
                "}\n"
                "Path(os.environ['PROBE_OUTPUT_PATH']).write_text(json.dumps(payload), encoding='utf-8')\n",
                encoding="utf-8",
            )
            probe_output = repo / "workspace" / "probe-output.json"
            self._set_client_acceptance_probe(
                repo,
                "personal",
                command=["python3", "${ROOT_DIR}/acceptance_probe_env.py"],
                cwd="${ROOT_DIR}/workspace",
                env={"PROBE_OUTPUT_PATH": "${ROOT_DIR}/workspace/probe-output.json"},
            )

            result = self._run(repo, "acceptance", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            probe_payload = json.loads(probe_output.read_text(encoding="utf-8"))
            self.assertEqual(probe_payload["public_base_url"], "https://reports.example.test")
            self.assertEqual(probe_payload["public_host"], "127.0.0.1")
            self.assertEqual(probe_payload["public_port"], "8443")

    def test_acceptance_skips_profile_gated_probe_when_profile_is_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            probe_script = repo / "acceptance_probe_should_not_run.py"
            probe_script.write_text(
                "from __future__ import annotations\n"
                "\n"
                "raise SystemExit('probe should not have run')\n",
                encoding="utf-8",
            )
            self._set_client_acceptance_probe(
                repo,
                "personal",
                command=["python3", "${ROOT_DIR}/acceptance_probe_should_not_run.py"],
                cwd="${ROOT_DIR}",
                profiles=["connectors"],
            )

            result = self._run(repo, "acceptance", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ready"])
            self.assertEqual(
                [step["step"] for step in payload["steps"]],
                ["doctor-pre", "sync", "focus", "mcp-smoke", "doctor-post"],
            )

    def test_acceptance_fails_before_activation_when_client_connectors_exceed_box_superset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            self._set_client_connectors(repo, "personal", ["github", "postgres"])

            result = self._run(repo, "acceptance", "personal", "--profile", "connectors", "--format", "json")

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            steps = {step["step"]: step for step in payload["steps"]}
            self.assertFalse(payload["ready"])
            self.assertEqual(payload["error"]["type"], "doctor_pre_failed")
            self.assertEqual(steps["doctor-pre"]["status"], "fail")
            self.assertEqual(steps["sync"]["status"], "skip")
            self.assertEqual(steps["focus"]["status"], "skip")
            self.assertEqual(steps["mcp-smoke"]["status"], "skip")
            connector_failures = [
                check
                for check in steps["doctor-pre"]["detail"]["checks"]
                if check["status"] == "fail" and check["code"] == "connector-contract"
            ]
            self.assertEqual(len(connector_failures), 1, steps["doctor-pre"]["detail"]["checks"])
            self.assertFalse((repo / "workspace" / ".focus.json").exists())

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

    def test_acceptance_fails_when_workflow_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            probe_script = repo / "acceptance_probe_fail.py"
            probe_script.write_text(
                "from __future__ import annotations\n"
                "\n"
                "import sys\n"
                "\n"
                "print('probe exploded')\n"
                "sys.exit(3)\n",
                encoding="utf-8",
            )
            self._set_client_acceptance_probe(
                repo,
                "personal",
                command=["python3", "${ROOT_DIR}/acceptance_probe_fail.py"],
                cwd="${ROOT_DIR}",
            )

            result = self._run(repo, "acceptance", "personal", "--format", "json")

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            steps = {step["step"]: step for step in payload["steps"]}
            self.assertFalse(payload["ready"])
            self.assertEqual(steps["mcp-smoke"]["status"], "ok")
            self.assertEqual(steps["workflow-probe"]["status"], "fail")
            self.assertEqual(steps["doctor-post"]["status"], "ok")
            self.assertEqual(payload["error"]["type"], "acceptance_probe_failed")
            self.assertIn("logs --client personal --format json", payload["next_actions"])
            self.assertEqual(steps["workflow-probe"]["detail"]["exit_code"], 3)
            self.assertEqual(steps["workflow-probe"]["detail"]["stdout_tail"], ["probe exploded"])

    def test_acceptance_translates_runtime_mcp_paths_before_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            mcp_payload = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))
            mcp_payload["mcpServers"]["skillbox"]["args"] = ["/workspace/.env-manager/mcp_server.py"]
            (repo / ".mcp.json").write_text(json.dumps(mcp_payload, indent=2) + "\n", encoding="utf-8")

            result = self._run(repo, "acceptance", "personal", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            steps = {step["step"]: step for step in payload["steps"]}
            self.assertTrue(payload["ready"])
            self.assertEqual(steps["mcp-smoke"]["status"], "ok")

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

    def test_sync_artifact_download_reconciles_stale_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "bin" / "tool"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"#!/bin/sh\necho old\n")

            payload = b"#!/bin/sh\necho new\n"
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
            self.assertEqual(actions, [f"download-reconcile: https://example.com/tool -> {target}"])
            self.assertEqual(target.read_bytes(), payload)

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


# ---------------------------------------------------------------------------
# WG-007: local-core mode-aware up orchestration and topology tests
# ---------------------------------------------------------------------------
#
# Reuses the in-memory local-core model builder from test_local_runtime so
# we assert against the exact same six-service graph that US-1 is checked
# against.  Tests here exercise US-2 (mode-aware up), US-3 (bootstrap +
# topology), and the US-4 deferred-surface pre-mutation rejection path for
# the run_up workflow.
from tests.test_local_runtime import (  # noqa: E402
    LOCAL_CORE_SERVICE_IDS as _LOCAL_CORE_IDS,
    _build_local_core_model as _build_local_core,
)


class LocalCoreModeAwareUpUS2Tests(unittest.TestCase):
    """WG-007 / US-2: Mode-aware local-core up orchestration."""

    def _fresh_model(self, tmpdir: str) -> dict[str, Any]:
        return _build_local_core(Path(tmpdir).resolve())

    def test_up_mode_default_resolves_to_reuse(self) -> None:
        # Confirms that run_up treats an empty requested_mode string as
        # ``reuse`` without any mutation (backend.md Rule 2).  We use
        # dry_run so no actual service processes are started.
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._fresh_model(tmpdir)
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="",
                dry_run=True,
            )
            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["effective_mode"], "reuse")
            self.assertEqual(payload["requested_mode"], "")
            self.assertTrue(payload.get("dry_run"))
            # All six services planned with the reuse command
            service_ids = [s["id"] for s in payload["services"]]
            self.assertEqual(service_ids, list(_LOCAL_CORE_IDS))
            for entry in payload["services"]:
                self.assertEqual(entry["mode"], "reuse")
                self.assertIn("reuse", entry["command"])

    def test_up_mode_reuse_picks_reuse_per_service_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._fresh_model(tmpdir)
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="reuse",
                dry_run=True,
            )
            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["effective_mode"], "reuse")
            for entry in payload["services"]:
                self.assertIn("local-up-reuse", entry["command"])

    def test_up_mode_prod_picks_prod_per_service_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._fresh_model(tmpdir)
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="prod",
                dry_run=True,
            )
            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["effective_mode"], "prod")
            for entry in payload["services"]:
                self.assertIn("local-up-prod", entry["command"])

    def test_up_mode_fresh_picks_fresh_per_service_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._fresh_model(tmpdir)
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="fresh",
                dry_run=True,
            )
            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["effective_mode"], "fresh")
            for entry in payload["services"]:
                self.assertIn("local-up-fresh", entry["command"])

    def test_up_unknown_mode_rejects_pre_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._fresh_model(tmpdir)
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="danger-mode",
                dry_run=True,
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(
                payload["error"]["type"], "LOCAL_RUNTIME_MODE_UNSUPPORTED",
            )
            # Whole request is rejected (no partial service start list).
            self.assertEqual(payload["services"], [])

    def test_up_mixed_mode_support_rejects_whole_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._fresh_model(tmpdir)
            # svc-finance only supports reuse; requesting prod must reject the
            # whole graph per backend.md Rule 2 / shared.md US-2.
            for service in model["services"]:
                if service["id"] == "svc-finance":
                    service["commands"] = {"reuse": "make local-up-reuse # svc-finance"}
                    break
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="prod",
                dry_run=True,
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(
                payload["error"]["type"], "LOCAL_RUNTIME_MODE_UNSUPPORTED",
            )
            self.assertIn("svc-finance", payload["error"]["blocked_services"])
            # No services were started, not even the five that do support prod
            self.assertEqual(payload["services"], [])

    def test_up_topological_order_is_six_service_graph(self) -> None:
        # Confirms svc-web's dual dependency (svc-auth+svc-api) is enforced,
        # and the five siblings depend only on svc-auth.  The planned order
        # emitted by run_up is produced by resolve_services_for_start.
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._fresh_model(tmpdir)
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="reuse",
                dry_run=True,
            )
            self.assertEqual(exit_code, 0, payload)
            ordered = [s["id"] for s in payload["services"]]
            pos = {sid: index for index, sid in enumerate(ordered)}
            # svc-auth strictly precedes each dependent service
            for dependent in (
                "svc-api",
                "svc-worker",
                "svc_feedback",
                "svc-finance",
                "svc-web",
            ):
                self.assertLess(pos["svc-auth"], pos[dependent])
            # svc-web depends on BOTH svc-auth and svc-api
            self.assertLess(pos["svc-api"], pos["svc-web"])
            self.assertEqual(set(ordered), set(_LOCAL_CORE_IDS))

    def test_up_identical_mode_commands_are_accepted(self) -> None:
        # shared.md US-2 AC: "identical commands across modes are allowed
        # but must still be explicit".  Validates service_supports_mode
        # accepts the explicit duplicate.
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._fresh_model(tmpdir)
            for service in model["services"]:
                service["commands"] = {
                    mode: "make local-up-same"
                    for mode in ("reuse", "prod", "fresh")
                }
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="fresh",
                dry_run=True,
            )
            self.assertEqual(exit_code, 0, payload)
            for entry in payload["services"]:
                self.assertEqual(entry["command"], "make local-up-same")


class LocalCoreBootstrapAndBlockedUS3Tests(unittest.TestCase):
    """WG-007 / US-3: Bootstrap ordering and START_BLOCKED."""

    def test_svc_feedback_db_bootstrap_ordered_before_service_start(self) -> None:
        # resolve_tasks_for_services returns bootstrap tasks in dependency
        # order for the resolved service list; the svc-feedback-db
        # task must appear in that list when svc_feedback is
        # being started.
        with tempfile.TemporaryDirectory() as tmpdir:
            model = _build_local_core(Path(tmpdir).resolve())
            services = MANAGE_MODULE.select_local_runtime_services(
                model, "local-core",
            )
            ordered_services = MANAGE_MODULE.resolve_services_for_start(
                model, services, mode="reuse",
            )
            ordered_ids = [s["id"] for s in ordered_services]
            self.assertEqual(ordered_ids[0], "svc-auth")
            task_specs = MANAGE_MODULE.resolve_tasks_for_services(
                model, ordered_services,
            )
            task_ids = [t["id"] for t in task_specs]
            self.assertIn("svc-feedback-db-bootstrap", task_ids)
            self.assertIn("env-bridge-local-core", task_ids)
            # The bootstrap task is resolved BEFORE we start
            # svc_feedback (it is the bootstrap the service
            # declares as a dependency).  run_up runs tasks before
            # start_services; here we simply confirm the service
            # declares it in ``bootstrap_tasks``.
            feedback = next(
                s for s in ordered_services
                if s["id"] == "svc_feedback"
            )
            self.assertIn(
                "svc-feedback-db-bootstrap",
                feedback["bootstrap_tasks"],
            )

    def test_up_db_bootstrap_failure_returns_start_blocked(self) -> None:
        # Simulate the svc-feedback DB bootstrap failing to reach
        # port 5436 -- run_tasks raises and run_up translates it into
        # LOCAL_RUNTIME_START_BLOCKED with blocked_services populated.
        with tempfile.TemporaryDirectory() as tmpdir:
            model = _build_local_core(Path(tmpdir).resolve())
            import runtime_manager.workflows as workflows_mod
            original = workflows_mod.run_tasks

            def _boom(model_arg, task_specs, *, dry_run, mode=None):  # type: ignore[no-untyped-def]
                raise RuntimeError(
                    "Task svc-feedback-db-bootstrap failed: "
                    "port 5436 never became ready"
                )

            workflows_mod.run_tasks = _boom  # type: ignore[assignment]
            try:
                exit_code, payload = MANAGE_MODULE.run_up(
                    model=model,
                    client_id="personal",
                    profile="local-core",
                    requested_mode="reuse",
                    dry_run=False,
                )
            finally:
                workflows_mod.run_tasks = original  # type: ignore[assignment]

            self.assertEqual(exit_code, 1)
            self.assertEqual(
                payload["error"]["type"], "LOCAL_RUNTIME_START_BLOCKED",
            )
            self.assertIn(
                "svc_feedback",
                payload["error"]["blocked_services"],
            )

    def test_up_wrong_env_target_blocks_before_mutation(self) -> None:
        # US-3: wrong env target path -> reconcile returns
        # LOCAL_RUNTIME_ENV_OUTPUT_MISSING and run_up surfaces it with no
        # mutation.  Confirms the pre-mutation ordering (reconcile runs
        # before mode validation and start).
        with tempfile.TemporaryDirectory() as tmpdir:
            model = _build_local_core(
                Path(tmpdir).resolve(),
                feedback_env_filename=".env.local",
            )
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="reuse",
                dry_run=True,
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(
                payload["error"]["type"], "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            )
            self.assertEqual(payload["services"], [])


class LocalCoreParityLedgerUS4RunUpTests(unittest.TestCase):
    """WG-007 / US-4: run_up rejects deferred surfaces pre-mutation."""

    def test_up_deferred_service_rejected_pre_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = _build_local_core(Path(tmpdir).resolve())
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="reuse",
                service_filter=["legacy-builder"],
                dry_run=True,
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(
                payload["error"]["type"], "LOCAL_RUNTIME_SERVICE_DEFERRED",
            )
            self.assertEqual(payload["services"], [])
            self.assertEqual(payload["bootstrap_tasks"], [])

    def test_up_bridge_only_seam_rejected_as_service_deferred(self) -> None:
        # Bridge-only seams route through LOCAL_RUNTIME_SERVICE_DEFERRED
        # (NOT LOCAL_RUNTIME_ENV_BRIDGE_FAILED) per shared.md US-4.
        with tempfile.TemporaryDirectory() as tmpdir:
            model = _build_local_core(Path(tmpdir).resolve())
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-core",
                requested_mode="reuse",
                service_filter=["sync-sh-env-compilation"],
                dry_run=True,
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(
                payload["error"]["type"], "LOCAL_RUNTIME_SERVICE_DEFERRED",
            )
            self.assertNotEqual(
                payload["error"]["type"], "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
            )


class LocalMinimalRegressionTests(unittest.TestCase):
    """WG-007: Regression — the local-minimal subset still resolves and
    orchestrates under the new contract."""

    def test_local_minimal_focus_resolves_three_service_subset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core(repo)
            # Promote the three-service subset into local-minimal as well,
            # so the same overlay answers both profile ids.  This mirrors
            # shared.md Rule 1: local-minimal is a subset of local-core,
            # not a competing bridge path.
            for service in model["services"]:
                if service["id"] in ("svc-auth", "svc-api", "svc-web"):
                    service["profiles"] = sorted(
                        set(service["profiles"]) | {"local-minimal"}
                    )
            for bridge in model["bridges"]:
                bridge["profiles"] = sorted(
                    set(bridge["profiles"]) | {"local-minimal"}
                )
            for task in model["tasks"]:
                if task["id"] == "env-bridge-local-core":
                    task["profiles"] = sorted(
                        set(task["profiles"]) | {"local-minimal"}
                    )
            model["active_profiles"] = ["core", "local-minimal"]

            services = MANAGE_MODULE.select_local_runtime_services(
                model, "local-minimal",
            )
            ids = [s["id"] for s in services]
            self.assertEqual(ids, ["svc-auth", "svc-api", "svc-web"])

            # focus reconciliation still passes against the same bridge outputs
            result = MANAGE_MODULE.reconcile_local_runtime_env(
                model, "local-minimal", overlay_path=None, dry_run=True,
            )
            self.assertEqual(result["status"], "ready", result)

            # up --mode reuse (dry-run) plans the three-service topo order
            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="personal",
                profile="local-minimal",
                requested_mode="reuse",
                dry_run=True,
            )
            self.assertEqual(exit_code, 0, payload)
            ordered = [s["id"] for s in payload["services"]]
            self.assertEqual(ordered, ["svc-auth", "svc-api", "svc-web"])


if __name__ == "__main__":
    unittest.main()
