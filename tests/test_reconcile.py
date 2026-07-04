from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
    def test_public_contract_surfaces_do_not_advertise_retired_activity_flow(self) -> None:
        surfaces = [
            ROOT_DIR / "README.md",
            ROOT_DIR / "docs" / "ROADMAP.md",
            ROOT_DIR / "Makefile",
            ROOT_DIR / ".env-manager" / "mcp_server.py",
        ]
        forbidden_terms = [
            "skillbox_ack",
            "skillbox_journal",
            "JSONL journal",
            "separate journal",
            "journal sidecar",
            "`ack`",
            "Recent Activity",
        ]

        hits: list[str] = []
        for surface in surfaces:
            text = surface.read_text(encoding="utf-8")
            for term in forbidden_terms:
                if term in text:
                    hits.append(f"{surface.relative_to(ROOT_DIR)}: {term}")

        self.assertEqual(hits, [])

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

    def test_build_model_uses_state_root_mounts_for_persistent_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            sandbox_doc, dependencies_doc, persistence_doc, skill_repos_doc, runtime_model = self._model_inputs()
            runtime_model["storage"]["bindings"][1]["resolved_host_path"] = str(repo / "private-config" / "clients")
            runtime_model["storage"]["bindings"][1]["relative_path"] = "clients"

            with self._patch_roots(repo), \
                mock.patch.object(RECONCILE, "load_yaml", side_effect=[sandbox_doc, dependencies_doc, persistence_doc, skill_repos_doc]), \
                mock.patch.object(RECONCILE, "load_json", return_value={}), \
                mock.patch.object(RECONCILE, "build_runtime_model", return_value=runtime_model), \
                mock.patch.object(RECONCILE, "load_env_defaults", return_value={"SKILLBOX_CLIENTS_HOST_ROOT": "./private-config/clients"}):
                model = RECONCILE.build_model()

            mounts = {item["target"]: item["source"] for item in model["expected_mounts"]}
            self.assertEqual(mounts["/workspace/workspace/clients"], "/state-root/clients")

    def test_build_model_overlays_local_dotenv_overrides_on_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            (repo / ".env").write_text(
                "SKILLBOX_PULSE_INTERVAL=10\n"
                "# comment line\n"
                "SKILLBOX_MONOSERVER_ROOT=/mnt/skillbox/repos\n"
                "SKILLBOX_EMPTY_OVERRIDE=\n"
                "SKILLBOX_CLIENTS_HOST_ROOT=/mnt/skillbox-config/clients\n"
                "SKILLBOX_NAME=ignored-not-runtime-env\n",
                encoding="utf-8",
            )

            sandbox_doc, dependencies_doc, persistence_doc, skill_repos_doc, runtime_model = self._model_inputs()

            with self._patch_roots(repo), \
                mock.patch.object(RECONCILE, "load_yaml", side_effect=[sandbox_doc, dependencies_doc, persistence_doc, skill_repos_doc]), \
                mock.patch.object(RECONCILE, "load_json", return_value={}), \
                mock.patch.object(RECONCILE, "build_runtime_model", return_value=runtime_model), \
                mock.patch.object(RECONCILE, "load_env_defaults", return_value={"SKILLBOX_CLIENTS_HOST_ROOT": "./workspace/clients"}):
                model = RECONCILE.build_model()

            runtime_env = model["runtime_env"]
            self.assertEqual(runtime_env["SKILLBOX_PULSE_INTERVAL"], "10")
            self.assertEqual(runtime_env["SKILLBOX_MONOSERVER_ROOT"], "/mnt/skillbox/repos")
            self.assertEqual(runtime_env["SKILLBOX_CASS_BIN"], "/workspace/home/.local/bin/cass")
            self.assertEqual(runtime_env["SKILLBOX_CM_MCP_PORT"], "3222")
            self.assertEqual(
                runtime_env["SKILLBOX_PULSE_UNHEALTHY_GRACE_SECONDS"],
                "60",
            )
            # Empty overrides fall back to manifest defaults so `${KEY:-default}` substitution
            # in compose still resolves consistently.
            self.assertNotIn("SKILLBOX_EMPTY_OVERRIDE", runtime_env)
            # CLIENTS_HOST_ROOT has dual semantics; the in-container view stays pinned to
            # CLIENTS_ROOT regardless of the host-side `.env` override.
            self.assertEqual(
                runtime_env["SKILLBOX_CLIENTS_HOST_ROOT"],
                runtime_env["SKILLBOX_CLIENTS_ROOT"],
            )
            # SKILLBOX_NAME is excluded from runtime_env, so .env overrides on it are irrelevant.
            self.assertNotIn("SKILLBOX_NAME", runtime_env)

    def test_load_runtime_env_overrides_returns_empty_when_dotenv_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(
                RECONCILE.load_runtime_env_overrides(Path(tmpdir) / ".env", {"SKILLBOX_FOO"}),
                {},
            )

    def test_check_secrets_visible_passes_on_migrated_layout(self) -> None:
        # Binds whose host dirs contain no secret files -> pass.
        with tempfile.TemporaryDirectory() as tmp:
            host_dir = Path(tmp)
            config = {
                "services": {
                    "workspace": {
                        "volumes": [
                            {"type": "bind", "source": str(host_dir), "target": "/workspace"},
                            {"type": "volume", "source": "named", "target": "/data"},
                        ]
                    }
                }
            }
            with mock.patch.object(RECONCILE, "compose_config", return_value=config):
                result = RECONCILE.check_secrets_visible_in_workspace()
            self.assertEqual(result.status, "pass")
            self.assertEqual(result.code, "secrets-visible-in-workspace")
            self.assertEqual(result.details, {"exposed": []})

    def test_check_secrets_visible_fails_with_planted_env_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host_dir = Path(tmp)
            (host_dir / ".env.box").write_text("SKILLBOX_DO_TOKEN=secret\n", encoding="utf-8")
            config = {
                "services": {
                    "workspace": {
                        "volumes": [
                            {"type": "bind", "source": str(host_dir), "target": "/workspace"},
                        ]
                    }
                }
            }
            with mock.patch.object(RECONCILE, "compose_config", return_value=config):
                result = RECONCILE.check_secrets_visible_in_workspace()
            self.assertEqual(result.status, "fail")
            self.assertEqual(result.code, "secrets-visible-in-workspace")
            self.assertIn(".env.box", result.details["exposed"])
            self.assertIsNotNone(result.fix_command)
            self.assertIn("mv ./.env.box ./.skillbox-state/operator/.env.box", result.fix_command)
            self.assertIn("mkdir -p ./.skillbox-state/operator", result.fix_command)
            # Only the present file should appear in the migration command.
            self.assertNotIn("mv ./.env ", result.fix_command)

    def test_check_secrets_visible_handles_compose_config_failure(self) -> None:
        with mock.patch.object(
            RECONCILE, "compose_config", side_effect=RuntimeError("docker missing")
        ):
            result = RECONCILE.check_secrets_visible_in_workspace()
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.code, "secrets-visible-in-workspace")
        self.assertIn("docker missing", result.details["error"])
        self.assertEqual(result.fix_command, "docker compose config")

    def test_compose_yaml_does_not_bind_mount_secret_files(self) -> None:
        # Parse compose YAML directly (no docker dependency) and assert no volume
        # `source` is literally a secret file. Mirrors the compose helper test approach.
        if RECONCILE.yaml is None:
            self.skipTest("PyYAML not available")
        for compose_name in ("docker-compose.yml", "docker-compose.monoserver.yml"):
            compose = RECONCILE.yaml.safe_load(
                (ROOT_DIR / compose_name).read_text(encoding="utf-8")
            )
            for service in (compose.get("services") or {}).values():
                for entry in service.get("volumes") or []:
                    if isinstance(entry, str):
                        source = entry.split(":", 1)[0]
                    else:
                        source = entry.get("source", "")
                    base = source.rstrip("/").rsplit("/", 1)[-1]
                    self.assertNotIn(
                        base,
                        RECONCILE.OPERATOR_SECRET_FILENAMES,
                        f"{compose_name} bind-mounts secret file {source}",
                    )

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
            RECONCILE.CheckResult(
                status="warn",
                code="warn-code",
                message="warning",
                details={"items": ["a", "b"]},
                fix_command="make render",
            ),
        ]
        with redirect_stdout(buf):
            RECONCILE.print_doctor_text(results)
        output = buf.getvalue()
        self.assertIn("PASS ok", output)
        self.assertIn("fix: make render", output)
        self.assertIn("summary:", output)
        self.assertEqual(RECONCILE.asdict(results[1])["fix_command"], "make render")

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
        # Build the expected fix_command without a literal sentinel so this
        # test file does not itself trip check_reference_drift (mirrors the
        # split-string convention used for `legacy_script` above).
        self.assertEqual(drift.fix_command, f'rg "{legacy_script}" .')

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
        self.assertEqual(lock_state.fix_command, RECONCILE.SKILL_SYNC_FIX_COMMAND)

        process = mock.Mock(
            returncode=0,
            stdout=json.dumps({"actions": ["skill-repo-fetched: example/skills"]}),
            stderr="",
        )
        with mock.patch.object(RECONCILE, "run_command", return_value=process):
            dry_run = RECONCILE.check_skill_sync_dry_run({})
        self.assertEqual(dry_run.status, "pass")
        self.assertEqual(dry_run.code, "skill-repo-sync-dry-run")
        self.assertEqual(dry_run.details["preview"], ["skill-repo-fetched: example/skills"])

        failed_process = mock.Mock(returncode=1, stdout="", stderr="sync failed")
        with mock.patch.object(RECONCILE, "run_command", return_value=failed_process):
            failed_dry_run = RECONCILE.check_skill_sync_dry_run({})
        self.assertEqual(failed_dry_run.status, "fail")
        self.assertEqual(failed_dry_run.fix_command, RECONCILE.SKILL_SYNC_FIX_COMMAND)

    def test_beads_state_reports_copy_pasteable_fix_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            with self._patch_roots(repo):
                missing = RECONCILE.check_beads_state()
        self.assertEqual(missing.status, "warn")
        self.assertEqual(missing.code, "beads-state")
        self.assertEqual(missing.fix_command, RECONCILE.BEADS_INIT_FIX_COMMAND)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            beads = repo / ".beads"
            beads.mkdir()
            (beads / "beads.db").write_text("", encoding="utf-8")
            with self._patch_roots(repo):
                unsynced = RECONCILE.check_beads_state()
        self.assertEqual(unsynced.status, "warn")
        self.assertEqual(unsynced.fix_command, RECONCILE.BEADS_SYNC_FIX_COMMAND)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            beads = repo / ".beads"
            beads.mkdir()
            (beads / "beads.db").write_text("", encoding="utf-8")
            (beads / "issues.jsonl").write_text("", encoding="utf-8")
            process = mock.Mock(
                returncode=0,
                stdout=json.dumps({"dirty_count": 1, "db_newer": False, "jsonl_exists": True}),
                stderr="",
            )
            with self._patch_roots(repo), \
                mock.patch.object(RECONCILE.shutil, "which", return_value="/usr/bin/br"), \
                mock.patch.object(RECONCILE, "run_command", return_value=process):
                dirty = RECONCILE.check_beads_state()
        self.assertEqual(dirty.status, "warn")
        self.assertEqual(dirty.fix_command, RECONCILE.BEADS_SYNC_FIX_COMMAND)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            beads = repo / ".beads"
            beads.mkdir()
            (beads / "beads.db").write_text("", encoding="utf-8")
            (beads / "issues.jsonl").write_text("", encoding="utf-8")
            process = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "dirty_count": 0,
                        "db_newer": False,
                        "jsonl_exists": True,
                        "last_export_time": "2026-06-19T15:05:46Z",
                    }
                ),
                stderr="",
            )
            with self._patch_roots(repo), \
                mock.patch.object(RECONCILE.shutil, "which", return_value="/usr/bin/br"), \
                mock.patch.object(RECONCILE, "run_command", return_value=process):
                synced = RECONCILE.check_beads_state()
        self.assertEqual(synced.status, "pass")
        self.assertIsNone(synced.fix_command)

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

    def test_agent_contract_commands_are_machine_readable(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(RECONCILE.main(["capabilities", "--json"]), 0)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tool"], "skillbox-reconcile")
        self.assertIn("stdout_stderr_contract", payload)
        self.assertEqual(payload["doctor_remediation"]["json_field"], "fix_command")
        self.assertIn("--jsno", payload["agent_surfaces"]["json_aliases"])
        self.assertIn(
            "python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync",
            payload["safe_previews"],
        )

        docs = io.StringIO()
        with redirect_stdout(docs):
            self.assertEqual(RECONCILE.main(["robot-docs", "guide"]), 0)
        self.assertIn("Skillbox reconcile agent guide", docs.getvalue())
        self.assertIn("fix_command", docs.getvalue())
        self.assertIn("sync --dry-run --format json", docs.getvalue())

        triage = io.StringIO()
        with redirect_stdout(triage):
            self.assertEqual(RECONCILE.main(["--robot-triage"]), 0)
        self.assertEqual(json.loads(triage.getvalue())["tool"], "skillbox-reconcile")

    def test_json_alias_and_error_paths_keep_stdout_stderr_split(self) -> None:
        doctor_results = [RECONCILE.CheckResult(status="pass", code="ok", message="ok")]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(RECONCILE, "doctor_results", return_value=doctor_results):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(RECONCILE.main(["doctor", "--jsno"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())[0]["code"], "ok")
        self.assertIsNone(json.loads(stdout.getvalue())[0]["fix_command"])
        self.assertIn("Interpreting --jsno as --format json", stderr.getvalue())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                RECONCILE.main(["doctro", "--json"])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Did you mean: `04-reconcile.py doctor`?", stderr.getvalue())
        self.assertIn("04-reconcile.py capabilities --json", stderr.getvalue())

    def test_runtime_error_is_concise_stderr_only(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(RECONCILE, "build_render_payload", side_effect=RuntimeError("boom")):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(RECONCILE.main(["render", "--format", "json"]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("04-reconcile.py: boom", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

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
                        "relative_path": "clients",
                        "resolved_host_path": "/state-root/clients",
                    },
                    {
                        "id": "claude-home",
                        "runtime_path": "/workspace/home/.claude",
                        "storage_class": "persistent",
                        "relative_path": "home/.claude",
                        "resolved_host_path": "/state-root/home/.claude",
                    },
                    {
                        "id": "codex-home",
                        "runtime_path": "/workspace/home/.codex",
                        "storage_class": "persistent",
                        "relative_path": "home/.codex",
                        "resolved_host_path": "/state-root/home/.codex",
                    },
                    {
                        "id": "logs-root",
                        "runtime_path": "/workspace/logs",
                        "storage_class": "persistent",
                        "relative_path": "logs",
                        "resolved_host_path": "/state-root/logs",
                    },
                    {
                        "id": "monoserver-root",
                        "runtime_path": "/monoserver",
                        "storage_class": "persistent",
                        "relative_path": "monoserver",
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


import subprocess  # noqa: E402

MANAGER = ROOT_DIR / ".env-manager" / "manage.py"

_PERSISTENCE_YAML = (
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
    "  - id: clients-root\n"
    "    runtime_path: /workspace/workspace/clients\n"
    "    storage_class: persistent\n"
    "    relative_path: clients\n"
)

_ENV_EXAMPLE = (
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
)


class RuntimeIdRejectionCliTests(unittest.TestCase):
    """skillbox-typed-contracts-epic-ugcx.4: render/doctor must reject an id
    that violates the canonical slug grammar with code RUNTIME_ID_INVALID and
    provenance (kind + source file), and client-init must refuse a bad slug.
    """

    def _run(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(MANAGER), "--root-dir", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_minimal_repo(self, repo: Path, *, service_id: str) -> None:
        (repo / "workspace").mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "persistence.yaml").write_text(
            _PERSISTENCE_YAML, encoding="utf-8"
        )
        (repo / ".env.example").write_text(_ENV_EXAMPLE, encoding="utf-8")
        (repo / "workspace" / "runtime.yaml").write_text(
            "version: 2\n"
            "selection:\n"
            "  profiles:\n"
            "    - core\n"
            "core:\n"
            "  services:\n"
            f"    - id: {service_id}\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}/.env-manager/manage.py\n",
            encoding="utf-8",
        )

    def test_render_rejects_bad_service_id_with_code_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_minimal_repo(repo, service_id="env/manager")

            result = self._run(repo, "render", "--format", "json")

            self.assertEqual(result.returncode, 1, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload.get("ok", True))
            error = payload["error"]
            self.assertEqual(error["code"], "RUNTIME_ID_INVALID")
            self.assertEqual(error["type"], "RUNTIME_ID_INVALID")
            self.assertEqual(payload["error_code"], "RUNTIME_ID_INVALID")
            context = error["context"]
            self.assertEqual(context["id"], "env/manager")
            self.assertEqual(context["kind"], "service")
            self.assertEqual(Path(context["source_file"]).name, "runtime.yaml")
            self.assertTrue(error["next_actions"])

    def test_doctor_rejects_bad_service_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_minimal_repo(repo, service_id="../escape")

            result = self._run(repo, "doctor", "--format", "json")

            self.assertEqual(result.returncode, 1, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["code"], "RUNTIME_ID_INVALID")
            self.assertEqual(payload["error"]["context"]["id"], "../escape")
            self.assertEqual(payload["error"]["context"]["kind"], "service")

    def test_clean_minimal_repo_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_minimal_repo(repo, service_id="env-manager")

            result = self._run(repo, "render", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertNotIn("RUNTIME_ID_INVALID", result.stdout)
            self.assertTrue(
                any(s["id"] == "env-manager" for s in payload.get("services", []))
            )

    def test_client_init_refuses_bad_slug(self) -> None:
        # client-init must reject a bad slug at creation time with the same
        # grammar (here: a path-separator slug), before any directory is made.
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_minimal_repo(repo, service_id="env-manager")

            result = self._run(repo, "client-init", "bad/slug", "--format", "json")

            self.assertEqual(result.returncode, 1, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertIn("Invalid client id", payload["error"]["message"])

    def test_client_init_refuses_uppercase_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_minimal_repo(repo, service_id="env-manager")

            result = self._run(repo, "client-init", "Acme", "--format", "json")

            self.assertEqual(result.returncode, 1, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertIn("Invalid client id", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
