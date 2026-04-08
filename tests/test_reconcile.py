from __future__ import annotations

import io
import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
RECONCILE = SourceFileLoader(
    "skillbox_reconcile",
    str((ROOT_DIR / "scripts" / "04-reconcile.py").resolve()),
).load_module()


class ReconcileTests(unittest.TestCase):
    def test_build_model_uses_client_override_mounts_when_focus_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            workspace = repo / "workspace"
            override = workspace / ".compose-overrides" / "docker-compose.client-personal.yml"
            override.parent.mkdir(parents=True, exist_ok=True)
            (workspace / ".focus.json").write_text('{"client_id":"personal"}', encoding="utf-8")
            override.write_text(
                "services:\n"
                "  workspace:\n"
                "    volumes:\n"
                "      - ./repos/personal:/monoserver/personal\n",
                encoding="utf-8",
            )

            sandbox_doc, dependencies_doc, persistence_doc, skill_repos_doc, runtime_model = self._model_inputs()

            with self._patch_roots(repo), \
                mock.patch.object(RECONCILE, "load_yaml", side_effect=[sandbox_doc, dependencies_doc, persistence_doc, skill_repos_doc]), \
                mock.patch.object(RECONCILE, "load_json", return_value={"skills": [{"name": "sample-skill"}]}), \
                mock.patch.object(RECONCILE, "build_runtime_model", return_value=runtime_model), \
                mock.patch.object(RECONCILE, "load_env_defaults", return_value={"SKILLBOX_CLIENTS_HOST_ROOT": "./workspace/clients"}):
                model = RECONCILE.build_model()

            self.assertEqual(model["expected_mounts"][-1], {"source": "./repos/personal", "target": "/monoserver/personal"})
            self.assertEqual(model["runtime_env"]["SKILLBOX_CLIENTS_HOST_ROOT"], "/workspace/workspace/clients")
            self.assertEqual(model["skill_sync"]["declared_skills"], ["sample-skill"])

    def test_build_model_falls_back_to_parent_mount_when_override_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            workspace = repo / "workspace"
            override = workspace / ".compose-overrides" / "docker-compose.client-personal.yml"
            override.parent.mkdir(parents=True, exist_ok=True)
            (workspace / ".focus.json").write_text('{"client_id":"personal"}', encoding="utf-8")
            override.write_text("not: [valid", encoding="utf-8")

            sandbox_doc, dependencies_doc, persistence_doc, skill_repos_doc, runtime_model = self._model_inputs()

            with self._patch_roots(repo), \
                mock.patch.object(RECONCILE, "load_yaml", side_effect=[sandbox_doc, dependencies_doc, persistence_doc, skill_repos_doc]), \
                mock.patch.object(RECONCILE, "load_json", return_value={}), \
                mock.patch.object(RECONCILE, "build_runtime_model", return_value=runtime_model), \
                mock.patch.object(RECONCILE, "load_env_defaults", return_value={"SKILLBOX_CLIENTS_HOST_ROOT": "./workspace/clients"}):
                model = RECONCILE.build_model()

            self.assertEqual(model["expected_mounts"][-1], {"source": "/state-root/monoserver", "target": "/monoserver"})

    def test_check_compose_model_reports_workspace_surface_and_swimmers_drift(self) -> None:
        model = {
            "expected_env": {
                "SKILLBOX_NAME": "skillbox",
                "SKILLBOX_SWIMMERS_PUBLISH_HOST": "127.0.0.1",
            },
            "runtime_env": {"A": "1"},
            "sandbox": {
                "paths": {"workspace_root": "/workspace"},
                "ports": {"api": 8000, "web": 3000, "swimmers": 3210},
            },
            "expected_mounts": [{"source": "/repo", "target": "/workspace"}],
        }
        base = {
            "name": "wrong",
            "x-runtime-env": {"A": "0"},
            "services": {
                "workspace": {
                    "working_dir": "/wrong",
                    "tty": False,
                    "stdin_open": False,
                    "environment": {"A": "0"},
                    "volumes": [{"source": "/other", "target": "/workspace"}],
                }
            },
        }
        surfaces = {
            "services": {
                "api": {
                    "profiles": [],
                    "environment": {"A": "0"},
                    "ports": [{"host_ip": "0.0.0.0", "target": 9000, "published": 9001}],
                },
                "web": {
                    "profiles": [],
                    "environment": {"A": "0"},
                    "ports": [],
                },
            }
        }
        swimmers = {
            "services": {
                "workspace": {
                    "environment": {"A": "0"},
                    "ports": [{"host_ip": "0.0.0.0", "target": 9000, "published": 9001}],
                }
            }
        }

        with mock.patch.object(RECONCILE, "compose_config", side_effect=[base, surfaces, swimmers]):
            results = RECONCILE.check_compose_model(model)

        by_code = {result.code: result for result in results}
        self.assertEqual(by_code["compose-config"].status, "pass")
        self.assertEqual(by_code["compose-workspace"].status, "fail")
        self.assertEqual(by_code["compose-surfaces"].status, "fail")
        self.assertEqual(by_code["compose-swimmers"].status, "fail")

    def test_compose_summary_and_render_output_include_compose_details(self) -> None:
        model = {"runtime_env": {"A": "1"}}
        configs = [
            {"name": "skillbox", "services": {"workspace": {"working_dir": "/workspace", "environment": {"A": "1"}}}},
            {"services": {"api": {"ports": [{"published": 8000}]}, "web": {"ports": [{"published": 3000}]}}},
            {"services": {"workspace": {"ports": [{"published": 3210}]}}},
        ]

        with mock.patch.object(RECONCILE, "compose_config", side_effect=configs):
            summary = RECONCILE.compose_summary(model)

        self.assertEqual(summary["project_name"], "skillbox")
        payload = {
            "sandbox": {"name": "skillbox", "purpose": "test", "runtime": {"mode": "container", "agent_user": "sandbox"}, "entrypoints": ["workspace"]},
            "expected_env": {"A": "1"},
            "expected_mounts": [{"source": "/repo", "target": "/workspace"}],
            "skill_sync": {
                "config_file": str(ROOT_DIR / "workspace" / "skill-repos.yaml"),
                "lock_file": str(ROOT_DIR / "workspace" / "skill-repos.lock.json"),
                "clone_root": str(ROOT_DIR / "workspace" / "skill-repos"),
                "declared_skills": ["sample-skill"],
                "locked_skills": ["sample-skill"],
            },
            "runtime_manager": {
                "script": str(ROOT_DIR / ".env-manager" / "manage.py"),
                "manifest_file": str(ROOT_DIR / "workspace" / "runtime.yaml"),
                "persistence_manifest_file": str(ROOT_DIR / "workspace" / "persistence.yaml"),
                "clients": [],
                "repos": [{}],
                "skills": [{}],
                "services": [{}],
                "logs": [{}],
                "checks": [{}],
            },
            "compose": summary,
        }

        buf = io.StringIO()
        with redirect_stdout(buf):
            RECONCILE.print_render_text(payload)
        output = buf.getvalue()

        self.assertIn("compose:", output)
        self.assertIn("project: skillbox", output)
        self.assertIn("swimmers workspace ports", output)

    def test_doctor_text_reference_drift_and_runtime_manager_doctor_helpers(self) -> None:
        buf = io.StringIO()
        results = [
            RECONCILE.CheckResult(status="pass", code="ok", message="all good"),
            RECONCILE.CheckResult(status="warn", code="warn-code", message="warning", details={"items": ["a", "b"]}),
        ]
        with redirect_stdout(buf):
            RECONCILE.print_doctor_text(results)
        output = buf.getvalue()
        self.assertIn("PASS ok", output)
        self.assertIn("summary:", output)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            (repo / "docs").mkdir()
            legacy_script = "00" "-skill-sync.sh"
            (repo / "docs" / "note.txt").write_text(f"use {legacy_script}\n", encoding="utf-8")
            (repo / ".cache").mkdir()
            (repo / ".cache" / "ignored.txt").write_text(f"{legacy_script}\n", encoding="utf-8")

            with self._patch_roots(repo):
                drift = RECONCILE.check_reference_drift()

        self.assertEqual(drift.status, "fail")
        self.assertEqual(drift.details["hits"], ["docs/note.txt:1"])

        process = mock.Mock(returncode=0, stdout=json.dumps({"checks": [{"status": "warn", "code": "skill-repo-lock-state"}]}), stderr="")
        with mock.patch.object(RECONCILE, "run_command", return_value=process):
            doctor = RECONCILE.check_runtime_manager_doctor()
        self.assertEqual(doctor.status, "pass")
        self.assertEqual(doctor.details["warning_codes"], ["skill-repo-lock-state"])

    def test_skill_repo_lock_state_and_sync_dry_run_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            lock_path = repo / "workspace" / "skill-repos.lock.json"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text("{}", encoding="utf-8")
            model = {
                "skill_sync": {
                    "lock_file": str(lock_path),
                    "declared_skills": ["ask-cascade", "describe"],
                    "locked_skills": ["ask-cascade"],
                }
            }

            lock_state = RECONCILE.check_bundle_state(model)

        self.assertEqual(lock_state.status, "warn")
        self.assertEqual(lock_state.code, "skill-repo-lock-state")
        self.assertEqual(lock_state.details["missing"], ["describe"])

        process = mock.Mock(
            returncode=0,
            stdout=json.dumps({"actions": ["skill-repo-fetched: build000r/skills"]}),
            stderr="",
        )
        with mock.patch.object(RECONCILE, "run_command", return_value=process):
            dry_run = RECONCILE.check_skill_sync_dry_run({})
        self.assertEqual(dry_run.status, "pass")
        self.assertEqual(dry_run.code, "skill-repo-sync-dry-run")
        self.assertEqual(dry_run.details["preview"], ["skill-repo-fetched: build000r/skills"])

    def test_check_manifest_alignment_and_compose_config_helpers(self) -> None:
        model = {
            "dependencies": {
                "home_mounts": [{"id": "claude-config", "path": "/workspace/home/.claude"}],
                "repo_workspaces": [
                    {"id": "sandbox-root", "path": "/workspace/repos"},
                    {"id": "monoserver-root", "path": "/monoserver"},
                ],
                "skill_roots": [{"id": "local-skills", "path": "/workspace/skills"}],
            },
            "sandbox": {
                "paths": {
                    "claude_root": "/workspace/home/.claude",
                    "codex_root": "/workspace/home/.codex",
                    "repos_root": "/workspace/repos",
                    "monoserver_root": "/monoserver",
                    "skills_root": "/workspace/skills",
                    "workspace_root": "/workspace",
                }
            },
            "skill_sync": {
                "runtime_skillset": {
                    "kind": "skill-repo-set",
                    "skill_repos_config": "/workspace/workspace/skill-repos.yaml",
                    "lock_path": "/workspace/workspace/skill-repos.lock.json",
                    "clone_root": "/workspace/workspace/skill-repos",
                    "sync": {"mode": "clone-and-install"},
                },
            },
        }

        self.assertEqual(RECONCILE.check_manifest_alignment(model).status, "fail")
        model["dependencies"]["home_mounts"].append({"id": "codex-config", "path": "/workspace/home/.codex"})
        self.assertEqual(RECONCILE.check_manifest_alignment(model).status, "pass")

        with mock.patch.object(RECONCILE.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "docker"):
                RECONCILE.compose_config(include_surfaces=False)

        ok_result = mock.Mock(returncode=0, stdout='{"services":{}}', stderr="")
        with mock.patch.object(RECONCILE.shutil, "which", return_value="/usr/bin/docker"), \
            mock.patch.object(RECONCILE, "run_command", return_value=ok_result):
            self.assertEqual(RECONCILE.compose_config(include_surfaces=True), {"services": {}})

        bad_result = mock.Mock(returncode=1, stdout="", stderr="compose failed")
        with mock.patch.object(RECONCILE.shutil, "which", return_value="/usr/bin/docker"), \
            mock.patch.object(RECONCILE, "run_command", return_value=bad_result):
            with self.assertRaisesRegex(RuntimeError, "compose failed"):
                RECONCILE.compose_config(include_surfaces=False)

    def test_load_yaml_and_main_cover_render_and_doctor_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            valid = repo / "valid.yaml"
            invalid = repo / "invalid.yaml"
            valid.write_text("key: value\n", encoding="utf-8")
            invalid.write_text("- not-a-mapping\n", encoding="utf-8")

            with self._patch_roots(repo):
                self.assertEqual(RECONCILE.load_yaml(valid), {"key": "value"})
                with self.assertRaisesRegex(RuntimeError, "Expected a YAML object"):
                    RECONCILE.load_yaml(invalid)

        render_payload = {"ok": True}
        doctor_results = [RECONCILE.CheckResult(status="fail", code="drift", message="broken")]
        with mock.patch.object(RECONCILE, "build_render_payload", return_value=render_payload), \
            mock.patch.object(RECONCILE, "doctor_results", return_value=doctor_results), \
            mock.patch.object(RECONCILE, "emit_json") as emit_json, \
            mock.patch.object(RECONCILE, "print_render_text") as print_render_text, \
            mock.patch.object(RECONCILE, "print_doctor_text") as print_doctor_text, \
            mock.patch("sys.argv", ["04-reconcile.py", "render", "--format", "json"]):
            self.assertEqual(RECONCILE.main(), 0)
        emit_json.assert_called_once_with(render_payload)
        print_render_text.assert_not_called()

        with mock.patch.object(RECONCILE, "build_render_payload", return_value=render_payload), \
            mock.patch.object(RECONCILE, "doctor_results", return_value=doctor_results), \
            mock.patch.object(RECONCILE, "emit_json") as emit_json, \
            mock.patch.object(RECONCILE, "print_render_text") as print_render_text, \
            mock.patch.object(RECONCILE, "print_doctor_text") as print_doctor_text, \
            mock.patch("sys.argv", ["04-reconcile.py", "doctor", "--format", "text"]):
            self.assertEqual(RECONCILE.main(), 1)
        print_doctor_text.assert_called_once_with(doctor_results)
        emit_json.assert_not_called()
        print_render_text.assert_not_called()

    def _model_inputs(
        self,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
        sandbox_doc = {
            "sandbox": {
                "name": "skillbox",
                "purpose": "test",
                "runtime": {"mode": "container", "agent_user": "sandbox"},
                "paths": {
                    "workspace_root": "/workspace",
                    "repos_root": "/workspace/repos",
                    "skills_root": "/workspace/skills",
                    "log_root": "/workspace/logs",
                    "claude_root": "/workspace/home/.claude",
                    "codex_root": "/workspace/home/.codex",
                    "monoserver_root": "/monoserver",
                },
                "ports": {"api": 8000, "web": 3000, "swimmers": 3210},
                "entrypoints": ["workspace"],
            }
        }
        dependencies_doc = {}
        persistence_doc = {
            "state_root_env": "SKILLBOX_STATE_ROOT",
            "targets": {
                "local": {
                    "provider": "local",
                    "default_state_root": "./.skillbox-state",
                }
            },
        }
        skill_repos_doc = {"skill_repos": [{"path": "../skills", "pick": ["sample-skill"]}]}
        runtime_model = {
            "manifest_file": "/workspace/runtime.yaml",
            "persistence_manifest_file": "/workspace/persistence.yaml",
            "env": {"SKILLBOX_CLIENTS_HOST_ROOT": "./workspace/clients"},
            "storage": {
                "provider": "local",
                "state_root": "/state-root",
                "bindings": [
                    {
                        "id": "workspace-root",
                        "runtime_path": "/workspace",
                        "storage_class": "external",
                        "resolved_host_path": str(ROOT_DIR),
                    },
                    {
                        "id": "clients-root",
                        "runtime_path": "/workspace/workspace/clients",
                        "storage_class": "persistent",
                        "resolved_host_path": "/state-root/clients",
                    },
                    {
                        "id": "claude-home",
                        "runtime_path": "/workspace/home/.claude",
                        "storage_class": "persistent",
                        "resolved_host_path": "/state-root/home/.claude",
                    },
                    {
                        "id": "codex-home",
                        "runtime_path": "/workspace/home/.codex",
                        "storage_class": "persistent",
                        "resolved_host_path": "/state-root/home/.codex",
                    },
                    {
                        "id": "logs-root",
                        "runtime_path": "/workspace/logs",
                        "storage_class": "persistent",
                        "resolved_host_path": "/state-root/logs",
                    },
                    {
                        "id": "monoserver-root",
                        "runtime_path": "/monoserver",
                        "storage_class": "persistent",
                        "resolved_host_path": "/state-root/monoserver",
                    },
                ],
            },
            "clients": [],
            "repos": [],
            "skills": [
                {
                    "id": "default-skills",
                    "kind": "skill-repo-set",
                    "skill_repos_config": "/workspace/workspace/skill-repos.yaml",
                    "lock_path": "/workspace/workspace/skill-repos.lock.json",
                    "clone_root": "/workspace/workspace/skill-repos",
                    "sync": {"mode": "clone-and-install"},
                    "client": "",
                }
            ],
            "services": [],
            "logs": [],
            "checks": [],
        }
        return sandbox_doc, dependencies_doc, persistence_doc, skill_repos_doc, runtime_model

    def _patch_roots(self, repo: Path):
        return mock.patch.multiple(RECONCILE, ROOT_DIR=repo, WORKSPACE_DIR=repo / "workspace")


if __name__ == "__main__":
    unittest.main()
