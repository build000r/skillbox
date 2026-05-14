from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.runtime_ops import (  # noqa: E402
    collect_live_state,
    compact_runtime_status,
    repo_regenerable_file_rel_paths,
    select_services,
    select_tasks,
    sync_runtime,
    validate_task_state,
    validate_storage_posture,
)
from runtime_manager import runtime_ops as runtime_ops_module  # noqa: E402
from runtime_manager import validation as validation_module  # noqa: E402
from runtime_manager import context_rendering as context_rendering_module  # noqa: E402
from runtime_manager import mmdx_open as mmdx_open_module  # noqa: E402
from runtime_manager import publish as publish_module  # noqa: E402
from runtime_manager import text_renderers as text_renderers_module  # noqa: E402
from runtime_manager import workflows as workflows_module  # noqa: E402
from runtime_manager.text_renderers import print_client_diff_text, print_render_text  # noqa: E402
from runtime_manager.validation import collect_skill_inventory  # noqa: E402
from runtime_manager.workflows import run_onboard  # noqa: E402


class _FakeManageProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeout_exc: BaseException | None = None,
        cleanup_timeout: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout_exc = timeout_exc
        self.cleanup_timeout = cleanup_timeout
        self.pid = 12345
        self.communicate_calls = 0

    def __enter__(self) -> "_FakeManageProcess":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def communicate(self, *, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_calls += 1
        if self.communicate_calls == 1 and self.timeout_exc is not None:
            raise self.timeout_exc
        if self.cleanup_timeout:
            raise workflows_module.subprocess.TimeoutExpired("manage.py", timeout or 0)
        return self.stdout, self.stderr


class RuntimeStatusHotspotTests(unittest.TestCase):
    def test_compact_runtime_status_preserves_agent_facing_summary(self) -> None:
        payload = {
            "clients": [{"id": "personal"}, {"id": ""}],
            "active_clients": ["personal"],
            "default_client": "personal",
            "active_profiles": ["core"],
            "distributors": [{"id": "local"}],
            "storage": {
                "provider": "local",
                "state_root": "/tmp/skillbox",
                "required": True,
                "secret": "discard",
            },
            "repos": [
                {
                    "id": "app",
                    "present": True,
                    "branch": "main",
                    "dirty": 1,
                    "untracked": 2,
                    "remote": "discard",
                }
            ],
            "artifacts": [
                {
                    "id": "archive",
                    "state": "ok",
                    "source_kind": "file",
                    "required": True,
                    "host_path": "/hidden",
                }
            ],
            "env_files": [
                {
                    "id": "env",
                    "state": "missing",
                    "source_kind": "manual",
                    "required": False,
                }
            ],
            "skills": [
                {
                    "id": "bundled",
                    "kind": "packaged-skill-set",
                    "lock_present": True,
                    "lock_error": None,
                    "skills": [
                        {
                            "name": "alpha",
                            "targets": [{"state": "ok"}, {"state": "drift"}],
                        }
                    ],
                }
            ],
            "tasks": [{"id": "sync", "state": "done", "depends_on": ["prep"]}],
            "services": [
                {
                    "id": "api",
                    "state": "running",
                    "pid": 123,
                    "managed": True,
                    "manager_reason": "",
                    "ownership_state": "covered",
                    "depends_on": ["db"],
                    "bootstrap_tasks": ["sync"],
                }
            ],
            "blocked_services": ["worker"],
            "logs": [{"id": "api-log", "present": True, "files": 3, "bytes": 42}],
            "checks": [{"id": "health", "type": "path_exists", "ok": True}],
            "ingress": {
                "route_file_present": True,
                "nginx_config_present": False,
                "routes": [{"id": "api"}],
            },
            "parity_ledger": {
                "covered_surfaces": ["api"],
                "deferred_surfaces": ["worker"],
            },
            "next_actions": ["inspect worker"],
        }

        compact = compact_runtime_status(payload)

        self.assertEqual(compact["client_ids"], ["personal"])
        self.assertEqual(
            compact["storage"],
            {"provider": "local", "state_root": "/tmp/skillbox", "required": True},
        )
        self.assertEqual(compact["skills"][0]["healthy_targets"], 1)
        self.assertEqual(compact["skills"][0]["total_targets"], 2)
        self.assertEqual(compact["ingress"]["route_count"], 1)
        self.assertEqual(compact["parity_ledger"]["covered_count"], 1)
        self.assertEqual(compact["next_actions"], ["inspect worker"])
        self.assertNotIn("secret", compact["storage"])

    def test_validate_storage_posture_reports_off_root_and_pass_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            inside = root / "inside"
            outside = root.parent / f"{root.name}-outside"
            inside.mkdir()
            outside.mkdir()
            base_storage = {
                "provider": "local",
                "required": True,
                "state_root": str(root),
                "min_free_gb": 0,
            }

            pass_model = {
                "storage": base_storage
                | {
                    "bindings": [
                        {
                            "id": "inside",
                            "storage_class": "persistent",
                            "resolved_host_path": str(inside),
                        }
                    ]
                }
            }
            fail_model = {
                "storage": base_storage
                | {
                    "bindings": [
                        {
                            "id": "outside",
                            "storage_class": "persistent",
                            "resolved_host_path": str(outside),
                        }
                    ]
                }
            }

            with mock.patch("runtime_manager.runtime_ops.os.access", return_value=True):
                self.assertEqual(validate_storage_posture(pass_model)[0].status, "pass")
                failures = validate_storage_posture(fail_model)

        self.assertEqual(failures[0].code, "PERSISTENT_PATH_OFF_STATE_ROOT")
        self.assertEqual(failures[0].details["binding_id"], "outside")

    def test_collect_live_state_summarizes_runtime_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "repo"
            log_path = root / "logs"
            check_path = root / "ready"
            repo_path.mkdir()
            log_path.mkdir()
            check_path.write_text("ok\n", encoding="utf-8")
            (log_path / "service.log").write_text("ok\nERROR failed\n", encoding="utf-8")
            model = {
                "repos": [{"id": "repo", "path": "/repo", "host_path": str(repo_path)}],
                "services": [{"id": "api", "kind": "service"}],
                "checks": [
                    {
                        "id": "ready",
                        "type": "path_exists",
                        "host_path": str(check_path),
                    }
                ],
                "logs": [{"id": "api-log", "path": "/logs", "host_path": str(log_path)}],
                "active_clients": ["personal"],
                "bridges": [{"id": "legacy", "env_tier": "local"}],
            }

            with (
                mock.patch(
                    "runtime_manager.runtime_ops.git_repo_state",
                    return_value={"git": False},
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.probe_service",
                    return_value={"state": "running", "pid": 123},
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.list_client_sessions",
                    return_value=[
                        {
                            "session_id": "s1",
                            "status": "active",
                            "label": "work",
                            "goal": "ship",
                            "updated_at": 2.0,
                            "last_event_type": "note",
                            "last_message": "hello",
                        }
                    ],
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.bridge_outputs_state",
                    return_value={"state": "missing", "missing": ["legacy.env"]},
                ),
            ):
                live_state = collect_live_state(model, root_dir=root)

        self.assertTrue(live_state["repos"][0]["present"])
        self.assertEqual(live_state["services"][0]["state"], "running")
        self.assertTrue(live_state["checks"][0]["ok"])
        self.assertEqual(live_state["logs"][0]["recent_errors"], ["ERROR failed"])
        self.assertEqual(live_state["sessions"][0]["session_id"], "s1")
        self.assertEqual(live_state["bridges"][0]["state"], "missing")

    def test_sync_runtime_dry_run_orders_repo_and_tail_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            existing_repo = root / "existing"
            existing_repo.mkdir()
            log_dir = root / "logs"
            model = {
                "root_dir": str(root),
                "repos": [
                    {
                        "id": "git",
                        "host_path": str(root / "git-repo"),
                        "source": {
                            "kind": "git",
                            "url": "https://example.test/repo.git",
                            "branch": "main",
                        },
                        "sync": {"mode": "clone-if-missing"},
                    },
                    {
                        "id": "existing",
                        "host_path": str(existing_repo),
                        "source": {"kind": "directory"},
                    },
                ],
                "artifacts": [{"id": "artifact"}],
                "env_files": [{"id": "env"}],
                "logs": [{"id": "log", "host_path": str(log_dir)}],
                "skills": [],
            }

            with (
                mock.patch(
                    "runtime_manager.runtime_ops.sync_artifact",
                    return_value=["artifact: ok"],
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.sync_env_file",
                    return_value=["env: ok"],
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.sync_skill_repo_sets",
                    return_value=["skill-repos: ok"],
                ),
                mock.patch(
                    "runtime_manager.runtime_ops._sync_distributor_sources",
                    return_value=["distributors: ok"],
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.sync_skill_sets",
                    return_value=["skills: ok"],
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.sync_dcg_config",
                    return_value=["dcg: ok"],
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.sync_ingress_artifacts",
                    return_value=["ingress: ok"],
                ),
            ):
                actions = sync_runtime(model, dry_run=True)

        self.assertEqual(
            actions,
            [
                f"clone-if-missing: https://example.test/repo.git -> {root / 'git-repo'}",
                f"exists: {existing_repo}",
                "artifact: ok",
                "env: ok",
                f"ensure-directory: {log_dir}",
                "skill-repos: ok",
                "distributors: ok",
                "skills: ok",
                "dcg: ok",
                "ingress: ok",
            ],
        )

    def test_validate_task_state_reports_pending_blocked_and_pass_states(self) -> None:
        model = {
            "tasks": [
                {"id": "ready"},
                {"id": "pending"},
                {"id": "blocked"},
            ]
        }
        states = {
            "ready": {"state": "ready"},
            "pending": {"state": "pending", "target": "/tmp/out"},
            "blocked": {
                "state": "blocked",
                "dependency_states": {"setup": "missing", "cache": "ok"},
            },
        }

        with mock.patch(
            "runtime_manager.runtime_ops.probe_task",
            side_effect=lambda _model, task: states[task["id"]],
        ):
            warning = validate_task_state(model)[0]

        self.assertEqual(validate_task_state({"tasks": []}), [])
        self.assertEqual(warning.status, "warn")
        self.assertEqual(warning.code, "bootstrap-task-state")
        self.assertEqual(warning.details["pending"], ["pending -> /tmp/out"])
        self.assertEqual(warning.details["blocked"], ["blocked (blocked by setup)"])

        with mock.patch("runtime_manager.runtime_ops.probe_task", return_value={"state": "ready"}):
            passed = validate_task_state(model)[0]
        self.assertEqual(passed.status, "pass")

    def test_repo_regenerable_file_rel_paths_scopes_env_and_task_outputs_to_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            other_root = root / "other"
            model = {
                "env_files": [
                    {
                        "id": "inside-env",
                        "repo": "app",
                        "host_path": str(repo_root / ".env.local"),
                    },
                    {
                        "id": "outside-env",
                        "repo": "app",
                        "host_path": str(other_root / ".env"),
                    },
                    {
                        "id": "other-repo-env",
                        "repo": "other",
                        "host_path": str(repo_root / "other.env"),
                    },
                ],
                "tasks": [
                    {
                        "id": "inside-task",
                        "repo": "app",
                        "success": {
                            "type": "path_exists",
                            "host_path": str(repo_root / ".skillbox" / "bootstrap.ok"),
                        },
                    },
                    {
                        "id": "port-task",
                        "repo": "app",
                        "success": {"type": "port_listening", "host_path": str(repo_root / "ignored")},
                    },
                    {
                        "id": "outside-task",
                        "repo": "app",
                        "success": {"type": "path_exists", "host_path": str(other_root / "done")},
                    },
                ],
            }

            allowed = repo_regenerable_file_rel_paths(
                model,
                {"id": "app", "host_path": str(repo_root)},
            )

        self.assertEqual(allowed, {Path(".env.local"), Path(".skillbox/bootstrap.ok")})

    def test_select_tasks_and_services_filter_order_and_report_unknown_ids(self) -> None:
        model = {
            "tasks": [{"id": "prepare"}, {"id": "sync"}, {"id": ""}],
            "services": [{"id": "api"}, {"id": "worker"}, {"name": "nameless"}],
        }

        self.assertEqual(select_tasks(model, None), model["tasks"])
        self.assertEqual(select_services(model, []), model["services"])
        self.assertEqual([task["id"] for task in select_tasks(model, [" sync ", "prepare"])], ["prepare", "sync"])
        self.assertEqual([svc["id"] for svc in select_services(model, ["worker"])], ["worker"])

        with self.assertRaisesRegex(RuntimeError, "Unknown task id\\(s\\): missing.*prepare, sync"):
            select_tasks(model, ["missing"])
        with self.assertRaisesRegex(RuntimeError, "Unknown service id\\(s\\): missing.*api, worker"):
            select_services(model, ["missing"])

    def test_repo_residue_and_git_repo_state_cover_presence_and_status_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            repo = {"id": "app", "host_path": str(repo_root)}

            self.assertFalse(runtime_ops_module.repo_has_only_regenerable_git_residue({"env_files": [], "tasks": []}, repo))

            repo_root.mkdir()
            (repo_root / ".skillbox").mkdir()
            (repo_root / ".skillbox" / "state.json").write_text("{}", encoding="utf-8")
            model = {
                "env_files": [{"id": "env", "repo": "app", "host_path": str(repo_root / ".env")}],
                "tasks": [
                    {
                        "id": "bootstrap",
                        "repo": "app",
                        "success": {"type": "path_exists", "host_path": str(repo_root / "ready.flag")},
                    }
                ],
            }
            (repo_root / ".env").write_text("A=1\n", encoding="utf-8")
            (repo_root / "ready.flag").write_text("ok\n", encoding="utf-8")
            self.assertTrue(runtime_ops_module.repo_has_only_regenerable_git_residue(model, repo))

            (repo_root / "manual.txt").write_text("keep\n", encoding="utf-8")
            self.assertFalse(runtime_ops_module.repo_has_only_regenerable_git_residue(model, repo))

            with mock.patch("runtime_manager.runtime_ops.run_command", return_value=mock.Mock(returncode=1, stdout="", stderr="")):
                self.assertEqual(runtime_ops_module.git_repo_state(repo_root), {"git": False})

            wrong_top = mock.Mock(returncode=0, stdout=str(root) + "\n", stderr="")
            with mock.patch("runtime_manager.runtime_ops.run_command", return_value=wrong_top):
                self.assertEqual(runtime_ops_module.git_repo_state(repo_root), {"git": False})

            status_fail = [
                mock.Mock(returncode=0, stdout=str(repo_root) + "\n", stderr=""),
                mock.Mock(returncode=1, stdout="", stderr="bad status"),
            ]
            with mock.patch("runtime_manager.runtime_ops.run_command", side_effect=status_fail):
                self.assertEqual(runtime_ops_module.git_repo_state(repo_root), {"git": False})

            status_ok = [
                mock.Mock(returncode=0, stdout=str(repo_root) + "\n", stderr=""),
                mock.Mock(
                    returncode=0,
                    stdout="## main...origin/main\n M changed.py\n?? new.py\n\nA  added.py\n",
                    stderr="",
                ),
            ]
            with mock.patch("runtime_manager.runtime_ops.run_command", side_effect=status_ok):
                self.assertEqual(
                    runtime_ops_module.git_repo_state(repo_root),
                    {"git": True, "branch": "main...origin/main", "dirty": 2, "untracked": 1},
                )

    def test_runtime_status_assembles_all_sections_and_blocks_down_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            log_root = root / "logs"
            check_path = root / "ready"
            repo_root.mkdir()
            log_root.mkdir()
            check_path.write_text("ok\n", encoding="utf-8")
            model = {
                "clients": [{"id": "acme"}],
                "active_clients": ["acme"],
                "selection": {"default_client": "acme"},
                "active_profiles": ["core"],
                "storage": {"provider": "local"},
                "repos": [
                    {
                        "id": "repo",
                        "kind": "git",
                        "path": "/repo",
                        "host_path": str(repo_root),
                        "profiles": ["core"],
                    }
                ],
                "artifacts": [{"id": "artifact"}],
                "env_files": [{"id": "env"}],
                "skills": [
                    {"id": "skill-repos", "kind": "skill-repo-set"},
                    {"id": "packaged", "kind": "packaged-skill-set"},
                ],
                "tasks": [{"id": "task", "inputs": ["in"], "outputs": ["out"]}],
                "services": [
                    {"id": "api", "kind": "service"},
                    {"id": "idle", "kind": "service"},
                ],
                "logs": [{"id": "runtime", "path": "/logs", "host_path": str(log_root)}],
                "checks": [
                    {"id": "ready", "type": "path_exists", "path": "/ready", "host_path": str(check_path)},
                    {"id": "custom", "type": "external"},
                ],
                "parity_ledger": [],
            }

            inventory = {
                "id": "packaged",
                "kind": "packaged-skill-set",
                "bundle_dir": "/bundles",
                "bundle_dir_host_path": str(root / "bundles"),
                "manifest": {},
                "lock_path": "/lock.json",
                "lock_present": True,
                "lock_error": None,
                "missing_bundles": [],
                "extra_bundles": [],
                "skills": [{"name": "one", "targets": []}],
            }

            with (
                mock.patch("runtime_manager.runtime_ops.git_repo_state", return_value={"git": True, "branch": "main"}),
                mock.patch("runtime_manager.runtime_ops.artifact_state", return_value={"id": "artifact", "state": "ok"}),
                mock.patch("runtime_manager.runtime_ops.env_file_state", return_value={"id": "env", "state": "ok"}),
                mock.patch(
                    "runtime_manager.runtime_ops._collect_skill_repo_status",
                    return_value={"id": "skill-repos", "kind": "skill-repo-set", "state": "ok"},
                ),
                mock.patch("runtime_manager.runtime_ops.collect_skill_inventory", return_value=inventory),
                mock.patch("runtime_manager.runtime_ops.probe_task", return_value={"state": "ready"}),
                mock.patch(
                    "runtime_manager.runtime_ops.probe_service",
                    side_effect=[{"state": "down"}, {"state": "idle"}],
                ),
                mock.patch(
                    "runtime_manager.runtime_ops.ownership_state_for_service",
                    side_effect=["covered", "external"],
                ),
                mock.patch("runtime_manager.runtime_ops.log_directory_state", return_value={"present": True, "files": 1}),
                mock.patch("runtime_manager.runtime_ops.has_ingress_runtime", return_value=True),
                mock.patch(
                    "runtime_manager.runtime_ops.ingress_config_paths",
                    return_value={"route_file": root / "routes.map", "nginx_config": root / "nginx.conf"},
                ),
                mock.patch("runtime_manager.runtime_ops.ingress_listener_settings", side_effect=lambda _m, name: {"name": name}),
                mock.patch("runtime_manager.runtime_ops.resolved_ingress_routes", return_value=[{"id": "route"}]),
                mock.patch("runtime_manager.runtime_ops._runtime_status_distributors", return_value=[{"id": "dist"}]),
                mock.patch("runtime_manager.runtime_ops.parity_ledger_covered_surfaces", return_value=["api"]),
                mock.patch("runtime_manager.runtime_ops.parity_ledger_deferred_surfaces", return_value=["worker"]),
            ):
                status = runtime_ops_module.runtime_status(model)

        self.assertEqual(status["default_client"], "acme")
        self.assertEqual(status["repos"][0]["branch"], "main")
        self.assertEqual(status["skills"][0]["kind"], "skill-repo-set")
        self.assertEqual(status["skills"][1]["skills"][0]["name"], "one")
        self.assertEqual(status["tasks"][0]["inputs"], ["in"])
        self.assertEqual(status["blocked_services"], ["api"])
        self.assertTrue(status["checks"][0]["ok"])
        self.assertEqual(status["ingress"]["routes"], [{"id": "route"}])
        self.assertEqual(status["parity_ledger"]["deferred_surfaces"], ["worker"])


class RuntimeScanHotspotTests(unittest.TestCase):
    def test_validate_ingress_covers_empty_invalid_and_artifact_states(self) -> None:
        self.assertEqual(runtime_ops_module.validate_ingress({}), [])
        self.assertEqual(
            runtime_ops_module.validate_ingress({"services": [{"id": "ingress", "kind": "ingress"}]}),
            [],
        )

        invalid_model = {
            "services": [{"id": "api", "origin_url": ""}],
            "ingress_routes": [{"id": "api-route", "service_id": "api", "path": "/api"}],
        }
        invalid_results = runtime_ops_module.validate_ingress(invalid_model)
        self.assertEqual(invalid_results[0].status, "fail")
        self.assertEqual(invalid_results[0].details["routes"], ["api-route"])

        route = {
            "id": "api-route",
            "service_id": "api",
            "origin_url": "http://127.0.0.1:9000",
        }
        paths = {"route_file": Path("/routes.json"), "nginx_config": Path("/nginx.conf")}
        with (
            mock.patch("runtime_manager.runtime_ops.resolved_ingress_routes", return_value=[route]),
            mock.patch("runtime_manager.runtime_ops.render_ingress_routes_document", return_value="routes"),
            mock.patch("runtime_manager.runtime_ops.render_ingress_nginx_config", return_value="nginx"),
            mock.patch("runtime_manager.runtime_ops.ingress_config_paths", return_value=paths),
            mock.patch("runtime_manager.runtime_ops.managed_text_artifact_state", side_effect=["ok", "stale"]),
        ):
            results = runtime_ops_module.validate_ingress({"ingress_routes": [{"id": "api-route"}]})

        self.assertEqual([result.status for result in results], ["pass", "warn"])
        self.assertEqual(results[0].code, "ingress-route-manifest")
        self.assertEqual(results[1].details["state"], "stale")

    def test_doctor_results_covers_manifest_connector_and_aggregate_success_paths(self) -> None:
        manifest_fail = text_renderers_module.CheckResult("fail", "manifest", "bad", {})
        connector_fail = text_renderers_module.CheckResult("fail", "connector", "bad", {})
        manifest_pass = text_renderers_module.CheckResult("pass", "manifest", "ok", {})
        connector_pass = text_renderers_module.CheckResult("pass", "connector", "ok", {})
        parity_pass = text_renderers_module.CheckResult("pass", "parity-ledger", "ok", {})

        with mock.patch("runtime_manager.runtime_ops.check_manifest", return_value=[manifest_fail]):
            self.assertEqual(runtime_ops_module.doctor_results({}, Path("/repo")), [manifest_fail])

        with (
            mock.patch("runtime_manager.runtime_ops.check_manifest", return_value=[manifest_pass]),
            mock.patch("runtime_manager.runtime_ops.validate_connector_contract", return_value=[connector_fail]),
        ):
            self.assertEqual(
                runtime_ops_module.doctor_results({}, Path("/repo")),
                [manifest_pass, connector_fail],
            )

        with (
            mock.patch("runtime_manager.runtime_ops.check_manifest", return_value=[manifest_pass]),
            mock.patch("runtime_manager.runtime_ops.validate_connector_contract", return_value=[connector_pass]),
            mock.patch("runtime_manager.runtime_ops.validate_parity_ledger", return_value=[parity_pass]),
            mock.patch("runtime_manager.runtime_ops.parity_ledger_deferred_surfaces", return_value=["legacy"]),
            mock.patch("runtime_manager.runtime_ops.parity_ledger_covered_surfaces", return_value=["api"]),
            mock.patch("runtime_manager.runtime_ops.check_filesystem", return_value=[]),
            mock.patch("runtime_manager.runtime_ops.validate_skill_repo_sets", return_value=[]),
            mock.patch("runtime_manager.runtime_ops.validate_skill_locks_and_state", return_value=[]),
            mock.patch("runtime_manager.runtime_ops.validate_task_state", return_value=[]),
            mock.patch("runtime_manager.runtime_ops.validate_storage_posture", return_value=[]),
            mock.patch("runtime_manager.runtime_ops.validate_bridges", return_value=[]),
            mock.patch("runtime_manager.runtime_ops.validate_ingress", return_value=[]),
        ):
            results = runtime_ops_module.doctor_results({}, Path("/repo"))

        self.assertEqual([result.code for result in results], ["manifest", "connector", "parity-ledger"])
        self.assertEqual(results[-1].details["subject"], "parity_ledger")
        self.assertEqual(results[-1].details["deferred_surfaces"], ["legacy"])

    def test_sync_dcg_config_and_scan_check_paths_cover_skip_exists_dry_run_and_write(self) -> None:
        self.assertEqual(
            runtime_ops_module.sync_dcg_config({"env": {}}, Path("/repo"), dry_run=False),
            ["skip: .dcg.toml (dcg not configured)"],
        )

        model = {"env": {"SKILLBOX_DCG_BIN": "dcg"}, "clients": []}
        with mock.patch("runtime_manager.runtime_ops._dcg_config_current", return_value=True):
            self.assertEqual(
                runtime_ops_module.sync_dcg_config(model, Path("/repo"), dry_run=False),
                ["exists: /repo/.dcg.toml"],
            )

        with mock.patch("runtime_manager.runtime_ops._dcg_config_current", return_value=False):
            self.assertEqual(
                runtime_ops_module.sync_dcg_config(model, Path("/repo"), dry_run=True),
                ["render-dcg-config: /repo/.dcg.toml (packs: core.git, core.filesystem)"],
            )

        with (
            mock.patch("runtime_manager.runtime_ops._dcg_config_current", return_value=False),
            mock.patch("runtime_manager.runtime_ops.atomic_write_text") as write_text,
        ):
            self.assertEqual(
                runtime_ops_module.sync_dcg_config(model, Path("/repo"), dry_run=False),
                ["render-dcg-config: /repo/.dcg.toml (packs: core.git, core.filesystem)"],
            )
        write_text.assert_called_once()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            existing = root / "exists.txt"
            existing.write_text("ok", encoding="utf-8")
            missing = root / "missing.txt"
            self.assertEqual(
                runtime_ops_module._scan_check_paths(  # noqa: SLF001
                    {
                        "checks": [
                            {"type": "path_exists", "host_path": str(existing), "required": True},
                            {"type": "path_exists", "host_path": str(missing), "required": True},
                            {"type": "http", "host_path": str(missing), "required": True},
                            {"type": "path_exists", "host_path": str(root / "optional"), "required": False},
                        ]
                    },
                    root,
                ),
                ["missing.txt"],
            )

    def test_dcg_packs_and_allowlist_merges_env_and_client_config(self) -> None:
        self.assertEqual(
            runtime_ops_module._dcg_packs_and_allowlist({"clients": []}, {}),  # noqa: SLF001
            (["core.git", "core.filesystem"], []),
        )

        packs, allowlist = runtime_ops_module._dcg_packs_and_allowlist(  # noqa: SLF001
            {
                "clients": [
                    {
                        "id": "personal",
                        "dcg": {
                            "packs": ["core.git", "custom.net"],
                            "allowlist": [{"cmd": "git status"}],
                        },
                    },
                    {"id": "other", "dcg": {"packs": ["ignored"]}},
                ]
            },
            {"SKILLBOX_DCG_PACKS": " core.filesystem, local.shell ,, "},
        )

        self.assertEqual(packs, ["core.filesystem", "local.shell", "core.git", "custom.net", "ignored"])
        self.assertEqual(allowlist, [])

    def test_scan_repo_paths_classifies_syncable_and_required_missing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            existing = root / "existing"
            existing.mkdir()
            model = {
                "repos": [
                    {"id": "existing", "host_path": str(existing)},
                    {
                        "id": "directory",
                        "host_path": str(root / "directory"),
                        "source": {"kind": "directory"},
                    },
                    {
                        "id": "git",
                        "host_path": str(root / "git"),
                        "source": {"kind": "git"},
                    },
                    {
                        "id": "manual-required",
                        "host_path": str(root / "manual"),
                        "source": {"kind": "manual"},
                        "required": True,
                    },
                    {
                        "id": "manual-optional",
                        "host_path": str(root / "optional"),
                        "source": {"kind": "manual"},
                    },
                ]
            }

            syncable, required = runtime_ops_module._scan_repo_paths(model, root)  # noqa: SLF001

        self.assertEqual(syncable, ["directory", "git"])
        self.assertEqual(required, ["manual"])

    def test_scan_artifact_paths_classifies_missing_stale_and_required_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.bin"
            stale_source = root / "stale-source.bin"
            stale_target = root / "stale-target.bin"
            present = root / "present.bin"
            source.write_text("source\n", encoding="utf-8")
            stale_source.write_text("desired\n", encoding="utf-8")
            stale_target.write_text("old\n", encoding="utf-8")
            present.write_text("ok\n", encoding="utf-8")
            model = {
                "artifacts": [
                    {
                        "id": "missing-copy",
                        "host_path": str(root / "missing.bin"),
                        "source": {"kind": "file", "host_path": str(source)},
                    },
                    {
                        "id": "stale-copy",
                        "host_path": str(stale_target),
                        "source": {"kind": "file", "host_path": str(stale_source)},
                    },
                    {
                        "id": "manual-required",
                        "host_path": str(root / "manual.bin"),
                        "source": {"kind": "manual"},
                        "required": True,
                    },
                    {
                        "id": "present-manual",
                        "host_path": str(present),
                        "source": {"kind": "manual"},
                        "required": True,
                    },
                ]
            }

            missing_syncable, stale_syncable, missing_required = (
                runtime_ops_module._scan_artifact_paths(model, root)  # noqa: SLF001
            )

        self.assertEqual(missing_syncable, ["missing.bin"])
        self.assertEqual(stale_syncable, ["stale-target.bin"])
        self.assertEqual(missing_required, ["manual.bin"])

    def test_scan_env_file_paths_handles_sources_bridges_syncable_and_required_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.env"
            stale_source = root / "stale-source.env"
            stale_target = root / "stale.env"
            bridge_source = root / "bridge-output.env"
            source.write_text("A=1\n", encoding="utf-8")
            stale_source.write_text("A=desired\n", encoding="utf-8")
            stale_target.write_text("A=old\n", encoding="utf-8")
            model = {
                "env_files": [
                    {
                        "id": "missing-source-required",
                        "path": "/env/missing.env",
                        "host_path": str(root / "missing-source-target.env"),
                        "required": True,
                        "source": {"kind": "file", "host_path": str(root / "missing-source.env")},
                    },
                    {
                        "id": "missing-source-optional",
                        "path": "/env/optional.env",
                        "host_path": str(root / "optional-target.env"),
                        "source": {"kind": "file", "host_path": str(root / "optional-source.env")},
                    },
                    {
                        "id": "bridge-source",
                        "path": "/env/bridge.env",
                        "host_path": str(root / "bridge-target.env"),
                        "required": True,
                        "source": {"kind": "file", "host_path": str(bridge_source)},
                    },
                    {
                        "id": "missing-syncable-target",
                        "path": "/env/syncable.env",
                        "host_path": str(root / "syncable.env"),
                        "source": {"kind": "file", "host_path": str(source)},
                    },
                    {
                        "id": "stale-syncable-target",
                        "path": "/env/stale.env",
                        "host_path": str(stale_target),
                        "source": {"kind": "file", "host_path": str(stale_source)},
                    },
                    {
                        "id": "manual-required-target",
                        "path": "/env/manual.env",
                        "host_path": str(root / "manual.env"),
                        "required": True,
                        "source": {"kind": "manual"},
                    },
                    {
                        "id": "missing-source-without-host",
                        "path": "/env/no-host.env",
                        "host_path": str(root / "no-host.env"),
                        "required": True,
                        "source": {"kind": "file", "path": "/remote/no-host.env"},
                    },
                ]
            }

            syncable, missing_sources, missing_targets = runtime_ops_module._scan_env_file_paths(  # noqa: SLF001
                model,
                root,
                {str(bridge_source.resolve())},
            )

        self.assertEqual(syncable, ["syncable.env", "stale.env"])
        self.assertEqual(missing_sources, ["missing-source.env", "/remote/no-host.env"])
        self.assertEqual(missing_targets, ["manual.env"])

    def test_sync_artifact_env_and_git_helpers_cover_reconcile_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifact_target = root / "out" / "artifact.sh"
            artifact_source = root / "artifact-source.sh"
            artifact_source.write_text("#!/bin/sh\n", encoding="utf-8")

            self.assertEqual(
                runtime_ops_module._sync_file_artifact(  # noqa: SLF001
                    {},
                    {"state": "missing"},
                    artifact_target,
                    dry_run=False,
                ),
                [f"skip: {artifact_target} (artifact source path missing)"],
            )
            self.assertEqual(
                runtime_ops_module._sync_file_artifact(  # noqa: SLF001
                    {"host_path": str(artifact_source)},
                    {"state": "missing"},
                    artifact_target,
                    dry_run=True,
                ),
                [f"copy-if-missing: {artifact_source} -> {artifact_target}"],
            )
            self.assertFalse(artifact_target.exists())
            self.assertEqual(
                runtime_ops_module._sync_file_artifact(  # noqa: SLF001
                    {"path": str(artifact_source), "executable": True},
                    {"state": "stale"},
                    artifact_target,
                    dry_run=False,
                ),
                [f"copy-reconcile: {artifact_source} -> {artifact_target}"],
            )
            self.assertEqual(artifact_target.read_text(encoding="utf-8"), "#!/bin/sh\n")
            self.assertEqual(artifact_target.stat().st_mode & 0o777, 0o755)

            env_source = root / "source.env"
            env_target = root / ".env"
            env_source.write_text("A=1\n", encoding="utf-8")
            self.assertEqual(
                runtime_ops_module._write_env_payload_if_changed(  # noqa: SLF001
                    {"mode": "0640"},
                    env_target,
                    env_source,
                ),
                [f"hydrate-env: {env_source} -> {env_target}"],
            )
            self.assertEqual(env_target.stat().st_mode & 0o777, 0o640)
            self.assertEqual(
                runtime_ops_module._write_env_payload_if_changed(  # noqa: SLF001
                    {"mode": "0640"},
                    env_target,
                    env_source,
                ),
                [f"env-unchanged: {env_target}"],
            )
            env_source.write_text("A=2\n", encoding="utf-8")
            self.assertEqual(
                runtime_ops_module._write_env_payload_if_changed(  # noqa: SLF001
                    {"mode": "0640"},
                    env_target,
                    env_source,
                ),
                [f"hydrate-env: {env_source} -> {env_target}"],
            )

            repo_path = root / "repo"
            repo_path.mkdir()
            model = {"env_files": [], "tasks": []}
            repo = {"id": "app", "host_path": str(repo_path)}
            with mock.patch("runtime_manager.runtime_ops.git_repo_state", return_value={"git": True}):
                self.assertEqual(
                    runtime_ops_module._sync_existing_git_repo(  # noqa: SLF001
                        model,
                        repo,
                        repo_path,
                        "https://example.test/app.git",
                        "main",
                        dry_run=False,
                    ),
                    f"exists: {repo_path}",
                )

            with (
                mock.patch("runtime_manager.runtime_ops.git_repo_state", return_value={"git": False}),
                mock.patch("runtime_manager.runtime_ops.repo_has_only_regenerable_git_residue", return_value=False),
            ):
                self.assertEqual(
                    runtime_ops_module._sync_existing_git_repo(  # noqa: SLF001
                        model,
                        repo,
                        repo_path,
                        "https://example.test/app.git",
                        "main",
                        dry_run=False,
                    ),
                    f"exists: {repo_path}",
                )

            with (
                mock.patch("runtime_manager.runtime_ops.git_repo_state", return_value={"git": False}),
                mock.patch("runtime_manager.runtime_ops.repo_has_only_regenerable_git_residue", return_value=True),
            ):
                self.assertEqual(
                    runtime_ops_module._sync_existing_git_repo(  # noqa: SLF001
                        model,
                        repo,
                        repo_path,
                        "https://example.test/app.git",
                        "main",
                        dry_run=True,
                    ),
                    f"clone-reconcile: https://example.test/app.git -> {repo_path}",
                )

            with (
                mock.patch("runtime_manager.runtime_ops.git_repo_state", return_value={"git": False}),
                mock.patch("runtime_manager.runtime_ops.repo_has_only_regenerable_git_residue", return_value=True),
                mock.patch("runtime_manager.runtime_ops.clear_repo_git_residue") as clear_residue,
                mock.patch("runtime_manager.runtime_ops._run_git_clone") as run_clone,
            ):
                self.assertEqual(
                    runtime_ops_module._sync_existing_git_repo(  # noqa: SLF001
                        model,
                        repo,
                        repo_path,
                        "https://example.test/app.git",
                        "main",
                        dry_run=False,
                    ),
                    f"clone-reconcile: https://example.test/app.git -> {repo_path}",
                )
            clear_residue.assert_called_once_with(repo_path)
            run_clone.assert_called_once_with("https://example.test/app.git", "main", repo_path)


class RuntimeTaskAndHealthHotspotTests(unittest.TestCase):
    def test_service_healthcheck_state_covers_declared_path_http_port_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ready = root / "ready"
            ready.write_text("ok\n", encoding="utf-8")

            self.assertEqual(runtime_ops_module.service_healthcheck_state({}), {"state": "declared"})
            self.assertEqual(
                runtime_ops_module.service_healthcheck_state(
                    {"healthcheck": {"type": "path_exists", "host_path": str(ready)}}
                ),
                {"state": "ok", "target": str(ready)},
            )
            self.assertEqual(
                runtime_ops_module.service_healthcheck_state(
                    {"healthcheck": {"type": "path_exists", "host_path": str(root / "missing")}}
                ),
                {"state": "down", "target": str(root / "missing")},
            )

        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        response.getcode.return_value = 204
        with mock.patch("runtime_manager.runtime_ops.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(
                runtime_ops_module.service_healthcheck_state(
                    {"healthcheck": {"type": "http", "url": "http://localhost/ok", "timeout_seconds": 1}}
                ),
                {"state": "ok", "status_code": 204, "url": "http://localhost/ok"},
            )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 1.0)

        with mock.patch(
            "runtime_manager.runtime_ops.urllib.request.urlopen",
            side_effect=runtime_ops_module.urllib.error.URLError("down"),
        ):
            self.assertEqual(
                runtime_ops_module.service_healthcheck_state(
                    {"healthcheck": {"type": "http", "url": "http://localhost/down"}}
                ),
                {"state": "down", "url": "http://localhost/down"},
            )

        self.assertEqual(
            runtime_ops_module.service_healthcheck_state({"healthcheck": {"type": "port", "port": "bad"}}),
            {"state": "unknown"},
        )
        with mock.patch("runtime_manager.runtime_ops._port_listening_state", return_value={"state": "ok", "port": 5432}):
            self.assertEqual(
                runtime_ops_module.service_healthcheck_state(
                    {"healthcheck": {"type": "port", "port": "5432", "host": "0.0.0.0"}}
                ),
                {"state": "ok", "port": 5432},
            )
        self.assertEqual(
            runtime_ops_module.service_healthcheck_state({"healthcheck": {"type": "other"}}),
            {"state": "unknown"},
        )

    def test_service_healthcheck_state_covers_process_running_variants(self) -> None:
        self.assertEqual(
            runtime_ops_module.service_healthcheck_state(
                {"healthcheck": {"type": "process_running", "pattern": "  "}}
            ),
            {"state": "down", "pattern": ""},
        )
        with mock.patch("runtime_manager.runtime_ops.run_command", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(
                runtime_ops_module.service_healthcheck_state(
                    {"healthcheck": {"type": "process_running", "pattern": "worker"}}
                ),
                {"state": "unknown", "pattern": "worker"},
            )

        ps_output = "badline\nabc not-a-pid\n123 python worker.py\n456 python worker.py --queue\n789 other\n"
        with mock.patch(
            "runtime_manager.runtime_ops.run_command",
            return_value=mock.Mock(returncode=0, stdout=ps_output),
        ):
            state = runtime_ops_module.service_healthcheck_state(
                {"healthcheck": {"type": "process_running", "pattern": "worker.py"}}
            )
        self.assertEqual(state["state"], "ok")
        self.assertEqual(state["matched_pid"], 123)
        self.assertEqual(state["match_count"], 2)

        with mock.patch(
            "runtime_manager.runtime_ops.run_command",
            return_value=mock.Mock(returncode=0, stdout="123 python other.py\n"),
        ):
            self.assertEqual(
                runtime_ops_module.service_healthcheck_state(
                    {"healthcheck": {"type": "process_running", "pattern": "worker.py"}}
                ),
                {"state": "down", "pattern": "worker.py"},
            )

    def test_run_tasks_reports_ready_blocked_dry_run_success_and_failure_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "logs"
            log_file = log_dir / "task.log"
            paths = {"log_dir": log_dir, "log_file": log_file}
            task = {"id": "build", "timeout_seconds": 2, "success": {"type": "path_exists"}}
            model = {"root_dir": str(root), "tasks": [task]}

            with (
                mock.patch("runtime_manager.runtime_ops.task_paths", return_value=paths),
                mock.patch("runtime_manager.runtime_ops.probe_task", return_value={"state": "ready", "target": "/done"}),
            ):
                self.assertEqual(
                    runtime_ops_module.run_tasks(model, [task], dry_run=False),
                    [
                        {
                            "id": "build",
                            "kind": "task",
                            "log_file": str(log_file),
                            "depends_on": [],
                            "result": "ready",
                            "target": "/done",
                        }
                    ],
                )

            with (
                mock.patch("runtime_manager.runtime_ops.task_paths", return_value=paths),
                mock.patch(
                    "runtime_manager.runtime_ops.probe_task",
                    return_value={"state": "blocked", "dependency_states": {"prep": "missing", "cache": "ok"}},
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "blocked by incomplete dependencies: prep"):
                    runtime_ops_module.run_tasks(model, [task], dry_run=False)

            with (
                mock.patch("runtime_manager.runtime_ops.task_paths", return_value=paths),
                mock.patch("runtime_manager.runtime_ops.probe_task", return_value={"state": "pending"}),
                mock.patch("runtime_manager.runtime_ops.translated_runtime_command", return_value=("echo ok", {})),
                mock.patch("runtime_manager.runtime_ops.resolve_runtime_command_cwd", return_value=root),
                mock.patch("runtime_manager.runtime_ops.ensure_directory") as ensure_directory,
            ):
                dry = runtime_ops_module.run_tasks(model, [task], dry_run=True)
            ensure_directory.assert_called_once_with(log_dir, True)
            self.assertEqual(dry[0]["result"], "dry-run")
            self.assertEqual(dry[0]["command"], "echo ok")

            class FakeProcess:
                pid = 321

                def __init__(self, *, returncode: int = 0, timeout: bool = False) -> None:
                    self.returncode = returncode
                    self.timeout = timeout
                    self.wait_calls = 0

                def wait(self, timeout: float | None = None) -> int:
                    self.wait_calls += 1
                    if self.timeout:
                        raise runtime_ops_module.subprocess.TimeoutExpired("build", timeout or 0)
                    return self.returncode

            log_dir.mkdir(exist_ok=True)
            log_file.write_text("last line\n", encoding="utf-8")
            success_process = FakeProcess()
            with (
                mock.patch("runtime_manager.runtime_ops.task_paths", return_value=paths),
                mock.patch(
                    "runtime_manager.runtime_ops.probe_task",
                    side_effect=[{"state": "pending"}, {"state": "ready", "target": "/done"}],
                ),
                mock.patch("runtime_manager.runtime_ops.translated_runtime_command", return_value=("echo ok", {})),
                mock.patch("runtime_manager.runtime_ops.resolve_runtime_command_cwd", return_value=root),
                mock.patch("runtime_manager.runtime_ops.ensure_directory"),
                mock.patch("runtime_manager.runtime_ops.subprocess.Popen", return_value=success_process),
                mock.patch("runtime_manager.runtime_ops.log_runtime_event") as event,
            ):
                done = runtime_ops_module.run_tasks(model, [task], dry_run=False)
            self.assertEqual(done[0]["result"], "completed")
            self.assertEqual(done[0]["target"], "/done")
            self.assertEqual(event.call_args_list[-1].args[:2], ("task.completed", "build"))

            failing_process = FakeProcess(returncode=7)
            with (
                mock.patch("runtime_manager.runtime_ops.task_paths", return_value=paths),
                mock.patch("runtime_manager.runtime_ops.probe_task", return_value={"state": "pending"}),
                mock.patch("runtime_manager.runtime_ops.translated_runtime_command", return_value=("exit 7", {})),
                mock.patch("runtime_manager.runtime_ops.resolve_runtime_command_cwd", return_value=root),
                mock.patch("runtime_manager.runtime_ops.ensure_directory"),
                mock.patch("runtime_manager.runtime_ops.subprocess.Popen", return_value=failing_process),
                mock.patch("runtime_manager.runtime_ops.log_runtime_event"),
                mock.patch("runtime_manager.runtime_ops.tail_lines", return_value=["boom"]),
            ):
                with self.assertRaisesRegex(RuntimeError, "failed with exit code 7.*boom"):
                    runtime_ops_module.run_tasks(model, [task], dry_run=False)

            timeout_process = FakeProcess(timeout=True)
            with (
                mock.patch("runtime_manager.runtime_ops.task_paths", return_value=paths),
                mock.patch("runtime_manager.runtime_ops.probe_task", return_value={"state": "pending"}),
                mock.patch("runtime_manager.runtime_ops.translated_runtime_command", return_value=("sleep", {})),
                mock.patch("runtime_manager.runtime_ops.resolve_runtime_command_cwd", return_value=root),
                mock.patch("runtime_manager.runtime_ops.ensure_directory"),
                mock.patch("runtime_manager.runtime_ops.subprocess.Popen", return_value=timeout_process),
                mock.patch("runtime_manager.runtime_ops.os.getpgid", return_value=987),
                mock.patch("runtime_manager.runtime_ops.os.killpg") as killpg,
                mock.patch("runtime_manager.runtime_ops.log_runtime_event"),
            ):
                with self.assertRaisesRegex(RuntimeError, "timed out after 2s"):
                    runtime_ops_module.run_tasks(model, [task], dry_run=False)
            killpg.assert_called_once_with(987, runtime_ops_module.signal.SIGKILL)

            unsatisfied_process = FakeProcess()
            with (
                mock.patch("runtime_manager.runtime_ops.task_paths", return_value=paths),
                mock.patch(
                    "runtime_manager.runtime_ops.probe_task",
                    side_effect=[{"state": "pending"}, {"state": "pending", "target": "/missing"}],
                ),
                mock.patch("runtime_manager.runtime_ops.translated_runtime_command", return_value=("echo ok", {})),
                mock.patch("runtime_manager.runtime_ops.resolve_runtime_command_cwd", return_value=root),
                mock.patch("runtime_manager.runtime_ops.ensure_directory"),
                mock.patch("runtime_manager.runtime_ops.subprocess.Popen", return_value=unsatisfied_process),
                mock.patch("runtime_manager.runtime_ops.log_runtime_event"),
                mock.patch("runtime_manager.runtime_ops.tail_lines", return_value=["not ready"]),
            ):
                with self.assertRaisesRegex(RuntimeError, "did not satisfy its success check.*not ready"):
                    runtime_ops_module.run_tasks(model, [task], dry_run=False)


class WorkflowManageJsonHotspotTests(unittest.TestCase):
    def test_run_manage_json_command_parses_json_stdout_and_preserves_stderr(self) -> None:
        fake_proc = _FakeManageProcess(stdout='{"ok": true}', stderr="warning\n", returncode=7)
        with mock.patch("runtime_manager.workflows.subprocess.Popen", return_value=fake_proc) as popen:
            code, payload = workflows_module.run_manage_json_command(
                Path("/repo"),
                ["doctor", "--format", "json"],
            )

        cmd = popen.call_args.args[0]
        self.assertEqual(code, 7)
        self.assertEqual(payload, {"ok": True, "_stderr": "warning"})
        self.assertIn("--root-dir", cmd)
        self.assertIn("/repo", cmd)
        self.assertEqual(fake_proc.communicate_calls, 1)

    def test_run_manage_json_command_wraps_non_dict_empty_and_plain_stdout(self) -> None:
        cases = [
            ("", {}),
            ("not json", {"stdout": "not json"}),
            ('["value"]', {"payload": ["value"]}),
        ]
        for stdout, expected in cases:
            with self.subTest(stdout=stdout):
                fake_proc = _FakeManageProcess(stdout=stdout)
                with mock.patch("runtime_manager.workflows.subprocess.Popen", return_value=fake_proc):
                    code, payload = workflows_module.run_manage_json_command(Path("/repo"), ["status"])
                self.assertEqual(code, 0)
                self.assertEqual(payload, expected)

    def test_run_manage_json_command_timeout_kills_process_group(self) -> None:
        timeout = workflows_module.subprocess.TimeoutExpired("manage.py", 300)
        fake_proc = _FakeManageProcess(
            stdout='{"late": true}',
            stderr="cleanup stderr",
            timeout_exc=timeout,
        )
        with (
            mock.patch("runtime_manager.workflows.subprocess.Popen", return_value=fake_proc),
            mock.patch("runtime_manager.workflows.os.getpgid", return_value=456) as getpgid,
            mock.patch("runtime_manager.workflows.os.killpg") as killpg,
        ):
            code, payload = workflows_module.run_manage_json_command(Path("/repo"), ["sync"])

        self.assertEqual(code, 124)
        self.assertIn("timed out after 300s", payload["error"])
        self.assertEqual(payload["_stderr"], "cleanup stderr")
        getpgid.assert_called_once_with(fake_proc.pid)
        killpg.assert_called_once_with(456, workflows_module.signal.SIGKILL)
        self.assertEqual(fake_proc.communicate_calls, 2)

    def test_run_manage_json_command_timeout_falls_back_to_exception_streams(self) -> None:
        timeout = workflows_module.subprocess.TimeoutExpired(
            "manage.py",
            300,
            output="partial stdout",
            stderr="partial stderr",
        )
        fake_proc = _FakeManageProcess(timeout_exc=timeout, cleanup_timeout=True)
        with (
            mock.patch("runtime_manager.workflows.subprocess.Popen", return_value=fake_proc),
            mock.patch("runtime_manager.workflows.os.getpgid", return_value=456),
            mock.patch("runtime_manager.workflows.os.killpg", side_effect=OSError),
        ):
            code, payload = workflows_module.run_manage_json_command(Path("/repo"), ["sync"])

        self.assertEqual(code, 124)
        self.assertIn("manage.py sync timed out", payload["error"])
        self.assertEqual(payload["_stderr"], "partial stderr")
        self.assertEqual(fake_proc.communicate_calls, 2)


class WorkflowOnboardAndFocusStateHotspotTests(unittest.TestCase):
    def test_run_stewardship_report_covers_json_text_write_and_error_paths(self) -> None:
        root = Path("/repo")
        with (
            mock.patch("runtime_manager.workflows.validate_client_id", side_effect=RuntimeError("bad client")),
            mock.patch("runtime_manager.workflows.emit_json") as emit_json,
        ):
            self.assertEqual(
                workflows_module.run_stewardship_report(
                    root_dir=root,
                    client_id="bad",
                    profiles=[],
                    fmt="json",
                    write=False,
                    output_dir_arg=None,
                ),
                workflows_module.EXIT_ERROR,
            )
        self.assertEqual(emit_json.call_args.args[0]["error"]["type"], "runtime_error")
        self.assertEqual(emit_json.call_args.args[0]["error"]["message"], "bad client")

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            overlay = repo / "clients" / "acme" / "overlay.yaml"
            overlay.parent.mkdir(parents=True)
            overlay.write_text("client:\n  id: acme\n", encoding="utf-8")
            payload = {"client_id": "acme", "artifact": {"path": "/report.md"}, "risks": []}

            with (
                mock.patch("runtime_manager.workflows.validate_client_id", return_value="acme"),
                mock.patch(
                    "runtime_manager.workflows.client_overlay_location",
                    return_value=({}, overlay, Path("/runtime/overlay.yaml")),
                ),
                mock.patch("runtime_manager.workflows._build_stewardship_report", return_value=payload) as build,
                mock.patch("runtime_manager.workflows._write_stewardship_artifact") as write_artifact,
                mock.patch("runtime_manager.workflows.emit_json") as emit_json,
            ):
                self.assertEqual(
                    workflows_module.run_stewardship_report(
                        root_dir=repo,
                        client_id="acme",
                        profiles=["local"],
                        fmt="json",
                        write=True,
                        output_dir_arg="reports",
                    ),
                    workflows_module.EXIT_OK,
                )
            build.assert_called_once_with(repo, "acme", ["local"])
            write_artifact.assert_called_once()
            emit_json.assert_called_once_with(payload)

            with (
                mock.patch("runtime_manager.workflows.validate_client_id", return_value="acme"),
                mock.patch(
                    "runtime_manager.workflows.client_overlay_location",
                    return_value=({}, overlay, Path("/runtime/overlay.yaml")),
                ),
                mock.patch("runtime_manager.workflows._build_stewardship_report", return_value=payload),
                mock.patch("runtime_manager.workflows.render_stewardship_report_markdown", return_value="report\n"),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(
                    workflows_module.run_stewardship_report(
                        root_dir=repo,
                        client_id="acme",
                        profiles=[],
                        fmt="markdown",
                        write=False,
                        output_dir_arg=None,
                    ),
                    workflows_module.EXIT_OK,
                )
            self.assertEqual(stdout.getvalue(), "report\n")

        with (
            mock.patch("runtime_manager.workflows.validate_client_id", return_value="acme"),
            mock.patch(
                "runtime_manager.workflows.client_overlay_location",
                return_value=({}, Path("/missing/overlay.yaml"), Path("/runtime/overlay.yaml")),
            ),
            mock.patch("runtime_manager.workflows.emit_json") as emit_json,
        ):
            self.assertEqual(
                workflows_module.run_stewardship_report(
                    root_dir=root,
                    client_id="acme",
                    profiles=[],
                    fmt="json",
                    write=False,
                    output_dir_arg=None,
                ),
                workflows_module.EXIT_ERROR,
            )
        self.assertIn("no overlay", emit_json.call_args.args[0]["error"]["message"])

    def test_run_focus_covers_initial_prepare_bootstrap_and_success_paths(self) -> None:
        root = Path("/repo")
        with mock.patch(
            "runtime_manager.workflows._focus_initial_state",
            return_value=(None, [], None, 9),
        ):
            self.assertEqual(
                workflows_module.run_focus(
                    root_dir=root,
                    client_id="acme",
                    profiles=[],
                    service_filter=[],
                    resume=False,
                    wait_seconds=1.0,
                    fmt="json",
                ),
                9,
            )

        model = {"active_profiles": ["local-core"]}
        with (
            mock.patch(
                "runtime_manager.workflows._focus_initial_state",
                return_value=("acme", ["local-core"], model, None),
            ),
            mock.patch("runtime_manager.workflows.local_runtime_active_profile", return_value="local-core"),
            mock.patch("runtime_manager.workflows._focus_prepare_runtime", return_value=8) as prepare,
        ):
            self.assertEqual(
                workflows_module.run_focus(
                    root_dir=root,
                    client_id="acme",
                    profiles=["local-core"],
                    service_filter=[],
                    resume=False,
                    wait_seconds=1.0,
                    fmt="json",
                ),
                8,
            )
        prepare.assert_called_once()

        with (
            mock.patch(
                "runtime_manager.workflows._focus_initial_state",
                return_value=("acme", [], model, None),
            ),
            mock.patch("runtime_manager.workflows.local_runtime_active_profile", return_value=None),
            mock.patch("runtime_manager.workflows._focus_prepare_runtime", return_value=None),
            mock.patch("runtime_manager.workflows._focus_bootstrap_and_start", return_value=([], {}, 7)),
        ):
            self.assertEqual(
                workflows_module.run_focus(
                    root_dir=root,
                    client_id="acme",
                    profiles=[],
                    service_filter=["api"],
                    resume=False,
                    wait_seconds=1.0,
                    fmt="json",
                ),
                7,
            )

        live = {"services": [{"id": "api", "healthy": True}], "repos": [], "checks": [], "logs": []}
        with (
            mock.patch(
                "runtime_manager.workflows._focus_initial_state",
                return_value=("acme", ["local-core"], model, None),
            ),
            mock.patch("runtime_manager.workflows.local_runtime_active_profile", return_value="local-core"),
            mock.patch("runtime_manager.workflows._focus_prepare_runtime", return_value=None),
            mock.patch(
                "runtime_manager.workflows._focus_bootstrap_and_start",
                return_value=([{"id": "bridge"}], {"bridge": {"fresh": True}}, None),
            ) as bootstrap,
            mock.patch("runtime_manager.workflows._focus_collect_and_persist", return_value=live) as collect,
            mock.patch("runtime_manager.workflows._focus_finish", return_value=workflows_module.EXIT_OK) as finish,
        ):
            self.assertEqual(
                workflows_module.run_focus(
                    root_dir=root,
                    client_id="acme",
                    profiles=["local-core"],
                    service_filter=["api"],
                    resume=True,
                    wait_seconds=2.5,
                    fmt="json",
                    context_dir=Path("/context"),
                ),
                workflows_module.EXIT_OK,
            )
        bootstrap.assert_called_once()
        collect.assert_called_once()
        finish.assert_called_once()
        self.assertEqual(finish.call_args.kwargs["bridges"], [{"id": "bridge"}])
        self.assertEqual(finish.call_args.kwargs["live"], live)

    def test_focus_step_detail_prefers_live_services_then_up_step_fallback(self) -> None:
        self.assertEqual(
            workflows_module.focus_step_detail(
                {
                    "live_state": {"services": [{"id": "api"}, {"id": ""}]},
                    "steps": [{"step": "up", "detail": {"services": [{"id": "worker"}]}}],
                },
                ["local-core"],
            ),
            {
                "active_profiles": ["local-core"],
                "services": ["api"],
                "step_names": ["up"],
            },
        )
        self.assertEqual(
            workflows_module.focus_step_detail(
                {
                    "live_state": {"services": []},
                    "steps": [
                        {"step": "sync"},
                        {"step": "up", "detail": {"services": [{"id": "worker"}, {"missing": True}]}},
                    ],
                },
                [],
            ),
            {
                "active_profiles": [],
                "services": ["worker"],
                "step_names": ["sync", "up"],
            },
        )

    def test_client_compose_repo_mounts_skip_internal_and_incomplete_repos(self) -> None:
        model = {
            "env": {"SKILLBOX_WORKSPACE_ROOT": "/workspace"},
            "repos": [
                {"host_path": "/host/project", "path": "/app/project"},
                {"host_path": "/host/internal", "path": "/workspace/internal"},
                {"host_path": "", "path": "/app/missing-host"},
                {"host_path": "/host/missing-runtime"},
            ],
        }

        self.assertEqual(
            workflows_module._client_compose_repo_mounts(model),  # noqa: SLF001
            {"/app/project": "/host/project"},
        )

    def test_validate_focus_client_reports_invalid_missing_and_success(self) -> None:
        root = Path("/repo")
        with (
            mock.patch("runtime_manager.workflows.validate_client_id", side_effect=RuntimeError("bad client")),
            mock.patch("runtime_manager.workflows.emit_json") as emit_json,
        ):
            self.assertEqual(
                workflows_module._validate_focus_client(root, "bad", True),  # noqa: SLF001
                ("", workflows_module.EXIT_ERROR),
            )
        self.assertEqual(emit_json.call_args.args[0]["error"]["message"], "bad client")

        with (
            mock.patch("runtime_manager.workflows.validate_client_id", return_value="acme"),
            mock.patch(
                "runtime_manager.workflows.client_overlay_location",
                return_value=({}, Path("/missing/overlay.yaml"), Path("/runtime/overlay.yaml")),
            ),
            mock.patch("runtime_manager.workflows.emit_json") as emit_json,
        ):
            self.assertEqual(
                workflows_module._validate_focus_client(root, "acme", True),  # noqa: SLF001
                ("acme", workflows_module.EXIT_ERROR),
            )
        self.assertIn("Use 'onboard acme'", emit_json.call_args.args[0]["error"]["message"])

        with tempfile.TemporaryDirectory() as tmpdir:
            overlay = Path(tmpdir) / "overlay.yaml"
            overlay.write_text("client:\n  id: acme\n", encoding="utf-8")
            with (
                mock.patch("runtime_manager.workflows.validate_client_id", return_value="acme"),
                mock.patch(
                    "runtime_manager.workflows.client_overlay_location",
                    return_value=({}, overlay, Path("/runtime/overlay.yaml")),
                ),
            ):
                self.assertEqual(
                    workflows_module._validate_focus_client(root, "acme", False),  # noqa: SLF001
                    ("acme", None),
                )

    def test_focus_local_runtime_preflight_covers_skip_blocked_ok_and_runtime_error(self) -> None:
        model = {"id": "model"}
        self.assertIsNone(
            workflows_module._focus_local_runtime_preflight(  # noqa: SLF001
                model, None, "acme", [], True,
            )
        )

        blocked_steps: list[dict[str, object]] = []
        with (
            mock.patch("runtime_manager.workflows.local_runtime_overlay_path", return_value=Path("/overlay.yaml")),
            mock.patch(
                "runtime_manager.workflows.reconcile_local_runtime_env",
                return_value={"status": "blocked", "error": {"type": "LOCAL_RUNTIME_START_BLOCKED"}},
            ),
            mock.patch("runtime_manager.workflows._focus_emit_local_runtime_payload", return_value=7) as emit,
        ):
            self.assertEqual(
                workflows_module._focus_local_runtime_preflight(  # noqa: SLF001
                    model, "local-core", "acme", blocked_steps, True,
                ),
                7,
            )
        self.assertEqual(blocked_steps[0]["status"], "fail")
        self.assertEqual(emit.call_args.args[1]["error"]["type"], "LOCAL_RUNTIME_START_BLOCKED")

        ok_steps: list[dict[str, object]] = []
        with (
            mock.patch("runtime_manager.workflows.local_runtime_overlay_path", return_value=Path("/overlay.yaml")),
            mock.patch(
                "runtime_manager.workflows.reconcile_local_runtime_env",
                return_value={"status": "ok", "actions": ["write env"]},
            ),
        ):
            self.assertIsNone(
                workflows_module._focus_local_runtime_preflight(  # noqa: SLF001
                    model, "local-core", "acme", ok_steps, False,
                )
            )
        self.assertEqual(ok_steps[0]["detail"]["actions"], ["write env"])

        error_steps: list[dict[str, object]] = []
        with (
            mock.patch("runtime_manager.workflows.local_runtime_overlay_path", return_value=Path("/overlay.yaml")),
            mock.patch("runtime_manager.workflows.reconcile_local_runtime_env", side_effect=RuntimeError("bad env")),
            mock.patch("runtime_manager.workflows._focus_emit_classify_error", return_value=6) as emit,
        ):
            self.assertEqual(
                workflows_module._focus_local_runtime_preflight(  # noqa: SLF001
                    model, "local-core", "acme", error_steps, True,
                ),
                6,
            )
        self.assertEqual(error_steps[0]["status"], "fail")
        self.assertEqual(str(emit.call_args.args[0]), "bad env")

    def test_focus_bootstrap_error_emitters_cover_doctor_and_runtime_paths(self) -> None:
        pass_check = text_renderers_module.CheckResult("pass", "doctor", "ok", {})
        fail_check = text_renderers_module.CheckResult("fail", "doctor", "bad", {"issue": "missing"})

        with mock.patch("runtime_manager.workflows.doctor_results", return_value=[pass_check]):
            self.assertIsNone(
                workflows_module._focus_emit_bootstrap_doctor_failure(  # noqa: SLF001
                    {}, Path("/repo"), "acme", [], True,
                )
            )

        steps: list[dict[str, object]] = []
        with (
            mock.patch("runtime_manager.workflows.doctor_results", return_value=[pass_check, fail_check]),
            mock.patch("runtime_manager.workflows.emit_json") as emit_json,
        ):
            self.assertEqual(
                workflows_module._focus_emit_bootstrap_doctor_failure(  # noqa: SLF001
                    {}, Path("/repo"), "acme", steps, True,
                ),
                workflows_module.EXIT_ERROR,
            )
        payload = emit_json.call_args.args[0]
        self.assertEqual(steps[0]["step"], "bootstrap")
        self.assertEqual(payload["error"]["type"], "pre_bootstrap_doctor_failed")
        self.assertEqual(payload["next_actions"][0], "doctor --client acme --format json")

        bridge_steps: list[dict[str, object]] = []
        with mock.patch("runtime_manager.workflows.emit_json") as emit_json:
            self.assertEqual(
                workflows_module._focus_emit_bootstrap_runtime_error(  # noqa: SLF001
                    RuntimeError("bridge-a output missing"),
                    "acme",
                    {"bridge-a": {"fresh": False}},
                    bridge_steps,
                    True,
                ),
                workflows_module.EXIT_ERROR,
            )
        self.assertEqual(
            emit_json.call_args.args[0]["error"]["type"],
            "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
        )

        with (
            mock.patch("runtime_manager.workflows.classify_error", return_value={"error": {"type": "runtime_error"}}),
            mock.patch("runtime_manager.workflows.emit_json") as emit_json,
        ):
            self.assertEqual(
                workflows_module._focus_emit_bootstrap_runtime_error(  # noqa: SLF001
                    RuntimeError("generic failure"),
                    "acme",
                    {"bridge-a": {}},
                    [],
                    True,
                ),
                workflows_module.EXIT_ERROR,
            )
        self.assertEqual(emit_json.call_args.args[0]["error"]["type"], "runtime_error")

    def test_focus_finish_summarizes_live_state_and_drift_exit(self) -> None:
        live = {
            "repos": [{"present": True, "dirty": 2}, {"present": False, "dirty": 0}],
            "services": [{"healthy": True}, {"state": "declared"}, {"state": "running"}],
            "checks": [{"ok": True}, {"ok": False}],
            "logs": [{"recent_errors": ["one", "two"]}, {}],
        }
        steps = [{"step": "sync", "status": "ok"}, {"step": "doctor", "status": "fail"}]
        with (
            mock.patch("runtime_manager.workflows._focus_local_runtime_section", return_value={"ready": False}),
            mock.patch("runtime_manager.workflows.next_actions_for_focus", return_value=["doctor"]),
            mock.patch("runtime_manager.workflows.log_runtime_event") as log_event,
            mock.patch("runtime_manager.workflows._focus_emit_summary") as emit_summary,
        ):
            self.assertEqual(
                workflows_module._focus_finish(  # noqa: SLF001
                    model={"active_profiles": ["core", "local-core"]},
                    active_local_profile="local-core",
                    bridges=[{"id": "bridge-a"}],
                    cid="acme",
                    live=live,
                    steps=steps,
                    is_json=True,
                    root_dir=Path("/repo"),
                ),
                workflows_module.EXIT_DRIFT,
            )

        payload = emit_summary.call_args.args[0]
        summary = emit_summary.call_args.args[1]
        self.assertEqual(summary["repos_present"], 1)
        self.assertEqual(summary["repos_dirty"], 1)
        self.assertEqual(summary["services_running"], 1)
        self.assertEqual(summary["services_down"], 1)
        self.assertEqual(summary["checks_passing"], 1)
        self.assertEqual(summary["recent_errors"], 2)
        self.assertEqual(payload["next_actions"], ["doctor"])
        self.assertEqual(payload["local_runtime"], {"ready": False})
        log_event.assert_called_once()

    def test_resolve_resume_focus_state_handles_missing_invalid_and_saved_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            focus_path = Path(tmpdir) / ".focus.json"
            with mock.patch("runtime_manager.workflows._focus_emit_simple_error", return_value=9) as emit:
                self.assertEqual(
                    workflows_module._resolve_resume_focus_state(focus_path, "acme", ["cli"], True),  # noqa: SLF001
                    ("acme", ["cli"], 9),
                )
            self.assertIn("No .focus.json", emit.call_args.args[0])

            focus_path.write_text("{not-json", encoding="utf-8")
            with mock.patch("runtime_manager.workflows._focus_emit_simple_error", return_value=8) as emit:
                self.assertEqual(
                    workflows_module._resolve_resume_focus_state(focus_path, "acme", [], False),  # noqa: SLF001
                    ("acme", [], 8),
                )
            self.assertIn("Failed to read .focus.json", emit.call_args.args[0])

            focus_path.write_text(
                json.dumps(
                    {
                        "client_id": "saved",
                        "active_profiles": ["core", "local-core", "", "prod"],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                workflows_module._resolve_resume_focus_state(focus_path, "acme", [], True),  # noqa: SLF001
                ("saved", ["local-core", "prod"], None),
            )
            self.assertEqual(
                workflows_module._resolve_resume_focus_state(focus_path, "acme", ["explicit"], True),  # noqa: SLF001
                ("saved", ["explicit"], None),
            )

    def test_focus_initial_state_returns_each_exit_path_and_success(self) -> None:
        root = Path("/repo")
        steps: list[dict[str, object]] = []
        with mock.patch(
            "runtime_manager.workflows._resolve_resume_focus_state",
            return_value=("saved", ["local"], 7),
        ):
            self.assertEqual(
                workflows_module._focus_initial_state(  # noqa: SLF001
                    root_dir=root,
                    client_id="acme",
                    profiles=[],
                    resume=True,
                    focus_path=Path("/focus.json"),
                    steps=steps,
                    is_json=True,
                ),
                (None, ["local"], None, 7),
            )

        with (
            mock.patch("runtime_manager.workflows._validate_focus_client", return_value=("", 6)),
        ):
            self.assertEqual(
                workflows_module._focus_initial_state(  # noqa: SLF001
                    root_dir=root,
                    client_id="bad",
                    profiles=["local"],
                    resume=False,
                    focus_path=Path("/focus.json"),
                    steps=[],
                    is_json=False,
                ),
                (None, ["local"], None, 6),
            )

        with (
            mock.patch("runtime_manager.workflows._validate_focus_client", return_value=("acme", None)),
            mock.patch("runtime_manager.workflows._build_focus_model", return_value=(None, None)),
        ):
            self.assertEqual(
                workflows_module._focus_initial_state(  # noqa: SLF001
                    root_dir=root,
                    client_id="acme",
                    profiles=[],
                    resume=False,
                    focus_path=Path("/focus.json"),
                    steps=[],
                    is_json=True,
                ),
                ("acme", [], None, workflows_module.EXIT_ERROR),
            )

        model = {"active_profiles": ["local-core"]}
        profile_error = {"error": {"type": "LOCAL_RUNTIME_BAD"}}
        with (
            mock.patch("runtime_manager.workflows._validate_focus_client", return_value=("acme", None)),
            mock.patch("runtime_manager.workflows._build_focus_model", return_value=(model, None)),
            mock.patch("runtime_manager.workflows.validate_local_runtime_profiles", return_value=[profile_error]),
            mock.patch("runtime_manager.workflows._focus_emit_local_runtime_payload", return_value=5) as emit,
        ):
            self.assertEqual(
                workflows_module._focus_initial_state(  # noqa: SLF001
                    root_dir=root,
                    client_id="acme",
                    profiles=[],
                    resume=False,
                    focus_path=Path("/focus.json"),
                    steps=[],
                    is_json=True,
                ),
                ("acme", [], None, 5),
            )
        self.assertEqual(emit.call_args.args[1], profile_error)

        with (
            mock.patch("runtime_manager.workflows._validate_focus_client", return_value=("acme", None)),
            mock.patch("runtime_manager.workflows._build_focus_model", return_value=(model, None)),
            mock.patch("runtime_manager.workflows.validate_local_runtime_profiles", return_value=[]),
        ):
            self.assertEqual(
                workflows_module._focus_initial_state(  # noqa: SLF001
                    root_dir=root,
                    client_id="acme",
                    profiles=["local-core"],
                    resume=False,
                    focus_path=Path("/focus.json"),
                    steps=[],
                    is_json=False,
                ),
                ("acme", ["local-core"], model, None),
            )

    def test_run_onboard_covers_scaffold_dry_run_sync_and_success_with_context_failure(self) -> None:
        root = Path("/repo")
        with (
            mock.patch("runtime_manager.workflows._onboard_scaffold_detail", side_effect=RuntimeError("bad input")),
            mock.patch("runtime_manager.workflows._emit_onboard_error", return_value=7) as emit_error,
        ):
            self.assertEqual(
                run_onboard(
                    root_dir=root,
                    client_id="bad",
                    label=None,
                    default_cwd=None,
                    root_path=None,
                    blueprint_name=None,
                    set_args=[],
                    dry_run=False,
                    force=False,
                    wait_seconds=1.0,
                    fmt="json",
                ),
                7,
            )
        self.assertEqual(emit_error.call_args.kwargs["client_id"], "bad")

        with (
            mock.patch(
                "runtime_manager.workflows._onboard_scaffold_detail",
                return_value=("acme", {"actions": ["create overlay"]}),
            ),
            mock.patch("runtime_manager.workflows._emit_onboard_dry_run", return_value=3) as dry_emit,
        ):
            self.assertEqual(
                run_onboard(
                    root_dir=root,
                    client_id="acme",
                    label="Acme",
                    default_cwd=None,
                    root_path=None,
                    blueprint_name=None,
                    set_args=[],
                    dry_run=True,
                    force=False,
                    wait_seconds=1.0,
                    fmt="text",
                ),
                3,
            )
        self.assertEqual(dry_emit.call_args.kwargs["cid"], "acme")

        with (
            mock.patch(
                "runtime_manager.workflows._onboard_scaffold_detail",
                return_value=("acme", {"actions": []}),
            ),
            mock.patch("runtime_manager.workflows._onboard_filtered_model", side_effect=RuntimeError("sync bad")),
            mock.patch("runtime_manager.workflows._emit_onboard_error", return_value=4) as emit_error,
        ):
            self.assertEqual(
                run_onboard(
                    root_dir=root,
                    client_id="acme",
                    label=None,
                    default_cwd=None,
                    root_path=None,
                    blueprint_name=None,
                    set_args=[],
                    dry_run=False,
                    force=False,
                    wait_seconds=1.0,
                    fmt="json",
                ),
                4,
            )
        self.assertEqual(emit_error.call_args.kwargs["client_id"], "acme")

        model = {"id": "model"}
        with (
            mock.patch(
                "runtime_manager.workflows._onboard_scaffold_detail",
                return_value=("acme", {"actions": []}),
            ),
            mock.patch("runtime_manager.workflows._onboard_filtered_model", return_value=model),
            mock.patch("runtime_manager.workflows.sync_runtime", return_value=["sync"]),
            mock.patch("runtime_manager.workflows._onboard_bootstrap_detail", return_value=("ok", {"tasks": []})),
            mock.patch("runtime_manager.workflows._onboard_up_detail", return_value=("ok", {"services": []})),
            mock.patch("runtime_manager.workflows.sync_context", side_effect=RuntimeError("context bad")),
            mock.patch(
                "runtime_manager.workflows._onboard_verify_detail",
                return_value=("fail", {"failures": ["doctor"]}, True),
            ),
            mock.patch("runtime_manager.workflows._onboard_next_actions", return_value=["fix doctor"]),
            mock.patch("runtime_manager.workflows.log_runtime_event") as event,
            mock.patch("runtime_manager.workflows.emit_json") as emit_json,
        ):
            self.assertEqual(
                run_onboard(
                    root_dir=root,
                    client_id="acme",
                    label=None,
                    default_cwd=None,
                    root_path=None,
                    blueprint_name=None,
                    set_args=[],
                    dry_run=False,
                    force=False,
                    wait_seconds=1.0,
                    fmt="json",
                ),
                workflows_module.EXIT_DRIFT,
            )

        payload = emit_json.call_args.args[0]
        self.assertEqual([step["step"] for step in payload["steps"]], ["scaffold", "sync", "bootstrap", "up", "context", "verify"])
        self.assertEqual(payload["steps"][4]["status"], "fail")
        event.assert_called_once()

    def test_onboard_verify_detail_reports_fail_warn_and_ok_status(self) -> None:
        fail = text_renderers_module.CheckResult("fail", "doctor", "bad", {"issue": "missing"})
        warn = text_renderers_module.CheckResult("warn", "doctor", "warn", {})
        passed = text_renderers_module.CheckResult("pass", "doctor", "ok", {})

        with mock.patch("runtime_manager.workflows.doctor_results", return_value=[passed]):
            self.assertEqual(
                workflows_module._onboard_verify_detail({}, Path("/repo")),  # noqa: SLF001
                ("ok", {"checks": [{"status": "pass", "code": "doctor", "message": "ok", "details": {}}]}, False),
            )

        with mock.patch("runtime_manager.workflows.doctor_results", return_value=[warn]):
            status, detail, has_fail = workflows_module._onboard_verify_detail({}, Path("/repo"))  # noqa: SLF001
        self.assertEqual(status, "warn")
        self.assertFalse(has_fail)
        self.assertEqual(detail["checks"][0]["status"], "warn")

        with mock.patch("runtime_manager.workflows.doctor_results", return_value=[warn, fail]):
            status, detail, has_fail = workflows_module._onboard_verify_detail({}, Path("/repo"))  # noqa: SLF001
        self.assertEqual(status, "fail")
        self.assertTrue(has_fail)
        self.assertEqual([item["status"] for item in detail["checks"]], ["warn", "fail"])

    def test_workflow_helpers_cover_first_box_mount_mcp_focus_and_persist_paths(self) -> None:
        args = workflows_module._first_box_onboard_args(  # noqa: SLF001
            cid="acme",
            label="Acme",
            default_cwd="/repo",
            root_path="/workspace",
            blueprint_name="python",
            set_args=["tier=local", "owner=ops"],
            force=True,
            wait_seconds=1.5,
        )
        self.assertEqual(args[:6], ["onboard", "acme", "--wait-seconds", "1.5", "--format", "json"])
        self.assertIn("--label", args)
        self.assertIn("--default-cwd", args)
        self.assertIn("--root-path", args)
        self.assertIn("--blueprint", args)
        self.assertEqual(args.count("--set"), 2)
        self.assertEqual(args[-1], "--force")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            swimmers = root / "swimmers"
            swimmers.mkdir()
            fake_lib = types.ModuleType("lib")
            fake_runtime_model = types.ModuleType("lib.runtime_model")
            fake_runtime_model.runtime_path_to_host_path = lambda _root, _env, _runtime_path: swimmers
            old_lib = sys.modules.get("lib")
            old_runtime_model = sys.modules.get("lib.runtime_model")
            sys.modules["lib"] = fake_lib
            sys.modules["lib.runtime_model"] = fake_runtime_model
            try:
                mounts: dict[str, str] = {}
                workflows_module._add_swimmers_compose_mount(  # noqa: SLF001
                    root,
                    {"env": {"SKILLBOX_SWIMMERS_REPO": "/swimmers"}},
                    mounts,
                )
            finally:
                if old_lib is None:
                    sys.modules.pop("lib", None)
                else:
                    sys.modules["lib"] = old_lib
                if old_runtime_model is None:
                    sys.modules.pop("lib.runtime_model", None)
                else:
                    sys.modules["lib.runtime_model"] = old_runtime_model
            self.assertEqual(mounts, {"/swimmers": str(swimmers)})

            self.assertEqual(
                workflows_module._prune_child_compose_mounts(  # noqa: SLF001
                    {
                        "/repo": "/host/repo",
                        "/repo/app": "/host/repo/app",
                        "/other": "/host/other",
                    }
                ),
                {"/other": "/host/other", "/repo": "/host/repo"},
            )

            with (
                mock.patch("runtime_manager.workflows.load_runtime_env", return_value={"ROOT": "/workspace"}),
                mock.patch("runtime_manager.workflows.translated_runtime_env", return_value={"ROOT": str(root)}),
                mock.patch(
                    "runtime_manager.workflows.translate_runtime_paths",
                    side_effect=lambda value, _runtime_env, _translated_env: value.replace("/workspace", str(root)),
                ),
                mock.patch(
                    "runtime_manager.workflows.absolutize_local_path_argument",
                    side_effect=lambda _root, value: f"abs:{value}",
                ),
            ):
                translated = workflows_module.translate_mcp_server_config(
                    root,
                    {"command": "/workspace/bin/mcp", "args": ["/workspace/config.json", 5]},
                )
            self.assertEqual(translated["command"], f"abs:{root}/bin/mcp")
            self.assertEqual(translated["args"], [f"abs:{root}/config.json", "abs:5"])

            detail: dict[str, object] = {}
            stray: list[str] = []
            with (
                mock.patch("runtime_manager.workflows.send_mcp_message") as send_message,
                mock.patch(
                    "runtime_manager.workflows.read_mcp_response",
                    return_value=({"tools": [{"name": "status"}, {"name": ""}, "ignored"]}, ["noise"]),
                ),
            ):
                workflows_module._smoke_mcp_list_tools(mock.Mock(), detail, stray)  # noqa: SLF001
            self.assertEqual(send_message.call_count, 2)
            self.assertEqual(stray, ["noise"])
            self.assertEqual(detail["tool_names"], ["status"])

            with (
                mock.patch("runtime_manager.workflows.send_mcp_message"),
                mock.patch(
                    "runtime_manager.workflows.read_mcp_response",
                    return_value=({"tools": {"bad": True}}, []),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "tools/list did not return"):
                    workflows_module._smoke_mcp_list_tools(mock.Mock(), {}, [])  # noqa: SLF001

            tasks = [
                {"id": "fresh", "bridge_id": "bridge"},
                {"id": "stale", "bridge_id": "other"},
                {"id": "plain"},
            ]
            with redirect_stdout(io.StringIO()) as stdout:
                to_run = workflows_module._focus_tasks_requiring_run(  # noqa: SLF001
                    tasks,
                    {"bridge": {"fresh": True}},
                    is_json=False,
                )
            self.assertEqual([task["id"] for task in to_run], ["stale", "plain"])
            self.assertIn("[skip] fresh", stdout.getvalue())

            focus_path = root / "focus.json"
            ctx_yaml = root / "context.yaml"
            ctx_yaml.write_text("client_id: acme\n", encoding="utf-8")
            steps: list[dict[str, object]] = []
            with (
                mock.patch(
                    "runtime_manager.workflows.client_context_location",
                    return_value=({}, ctx_yaml, Path("/runtime/context.yaml")),
                ),
                mock.patch("runtime_manager.workflows.time.time", return_value=42.0),
            ):
                workflows_module._focus_persist_step(  # noqa: SLF001
                    focus_path,
                    {"active_profiles": ["dev", "core"]},
                    "acme",
                    ["api"],
                    root,
                    steps,
                    is_json=True,
                )
            focus_data = json.loads(focus_path.read_text(encoding="utf-8"))
            self.assertEqual(focus_data["active_profiles"], ["core", "dev"])
            self.assertEqual(focus_data["skill_context_path"], "/runtime/context.yaml")
            self.assertEqual(steps[-1]["status"], "ok")

            bad_focus_path = root / "focus-dir"
            bad_focus_path.mkdir()
            failed_steps: list[dict[str, object]] = []
            with mock.patch(
                "runtime_manager.workflows.client_context_location",
                return_value=({}, root / "missing.yaml", Path("/runtime/missing.yaml")),
            ):
                workflows_module._focus_persist_step(  # noqa: SLF001
                    bad_focus_path,
                    {},
                    "acme",
                    [],
                    root,
                    failed_steps,
                    is_json=True,
                )
            self.assertEqual(failed_steps[-1]["status"], "fail")


class WorkflowAcceptanceHotspotTests(unittest.TestCase):
    def test_finalize_and_smoke_mcp_server_cover_success_failure_and_process_tails(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.communicate.return_value = ("one\n\n" + "\n".join(f"out{i}" for i in range(12)), "err\n")
        proc.returncode = 0

        stdout_tail, stderr_tail, exit_code = workflows_module.finalize_mcp_process(proc)

        proc.terminate.assert_called_once()
        self.assertEqual(stdout_tail[0], "out2")
        self.assertEqual(stdout_tail[-1], "out11")
        self.assertEqual(stderr_tail, ["err"])
        self.assertEqual(exit_code, 0)

        timeout_proc = mock.Mock()
        timeout_proc.poll.return_value = 123
        timeout_proc.communicate.side_effect = [
            workflows_module.subprocess.TimeoutExpired("mcp", 0.5),
            ("late out\n", "late err\n"),
        ]
        timeout_proc.returncode = -9

        self.assertEqual(
            workflows_module.finalize_mcp_process(timeout_proc),
            (["late out"], ["late err"], -9),
        )
        timeout_proc.kill.assert_called_once()

        self.assertEqual(
            workflows_module.smoke_mcp_server(Path("/repo"), "skillbox", {}),
            {
                "command": "",
                "args": [],
                "status": "fail",
                "error": "MCP server 'skillbox' has no command configured.",
            },
        )
        with mock.patch("runtime_manager.workflows._start_mcp_smoke_process", side_effect=OSError("missing binary")):
            self.assertEqual(
                workflows_module.smoke_mcp_server(Path("/repo"), "skillbox", {"command": "missing"}),
                {"command": "missing", "args": [], "status": "fail", "error": "missing binary"},
            )

        proc = mock.Mock()

        def initialize(_proc: object, detail: dict[str, object], stray: list[str]) -> None:
            detail["server_info"] = {"name": "skillbox"}
            stray.append("startup noise")

        def list_tools(_proc: object, detail: dict[str, object], stray: list[str]) -> None:
            detail["tool_names"] = ["status"]
            stray.append("tool noise")

        with (
            mock.patch("runtime_manager.workflows._start_mcp_smoke_process", return_value=proc),
            mock.patch("runtime_manager.workflows._smoke_mcp_initialize", side_effect=initialize),
            mock.patch("runtime_manager.workflows._smoke_mcp_list_tools", side_effect=list_tools),
            mock.patch("runtime_manager.workflows._record_mcp_process_tail") as record_tail,
        ):
            ok = workflows_module.smoke_mcp_server(
                Path("/repo"),
                "skillbox",
                {"command": "python3", "args": ["mcp.py"]},
            )
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["tool_names"], ["status"])
        record_tail.assert_called_once_with(proc, ok, ["startup noise", "tool noise"])

        with (
            mock.patch("runtime_manager.workflows._start_mcp_smoke_process", return_value=proc),
            mock.patch("runtime_manager.workflows._smoke_mcp_initialize", side_effect=RuntimeError("bad handshake")),
            mock.patch("runtime_manager.workflows._record_mcp_process_tail") as record_tail,
        ):
            failed = workflows_module.smoke_mcp_server(
                Path("/repo"),
                "skillbox",
                {"command": "python3"},
            )
        self.assertEqual(failed["status"], "fail")
        self.assertEqual(failed["error"], "bad handshake")
        record_tail.assert_called_once_with(proc, failed, [])

    def test_selected_mcp_server_configs_translates_required_and_skips_optional_unmanageable(self) -> None:
        model = {
            "services": [
                {"id": "worker-mcp", "kind": "mcp", "mcp_server": "worker", "required": True},
                {"id": "optional-mcp", "kind": "mcp", "mcp_server": "optional", "required": False},
            ]
        }
        configs = {
            "skillbox": {"command": "python3", "args": ["mcp_server.py"]},
            "worker": {"command": "node", "args": ["worker.js"]},
        }

        with (
            mock.patch("runtime_manager.workflows.load_mcp_server_configs", return_value=configs),
            mock.patch(
                "runtime_manager.workflows.translate_mcp_server_config",
                side_effect=lambda _root, config: config | {"translated": True},
            ),
            mock.patch("runtime_manager.workflows.service_supports_lifecycle", return_value=(False, "external")),
        ):
            selected, names = workflows_module.selected_mcp_server_configs(Path("/repo"), model)

        self.assertEqual(names, ["skillbox", "worker"])
        self.assertEqual(selected["skillbox"]["translated"], True)
        self.assertEqual(selected["worker"]["command"], "node")
        self.assertNotIn("optional", selected)

        with (
            mock.patch("runtime_manager.workflows.load_mcp_server_configs", return_value={"skillbox": configs["skillbox"]}),
            mock.patch("runtime_manager.workflows.translate_mcp_server_config", side_effect=lambda _root, config: config),
        ):
            with self.assertRaisesRegex(RuntimeError, "MCP server 'worker' is not configured"):
                workflows_module.selected_mcp_server_configs(Path("/repo"), model)

    def test_acceptance_probe_profile_and_mcp_helpers_report_invalid_inputs(self) -> None:
        overlay_path = Path("/clients/personal/overlay.yaml")
        self.assertEqual(
            workflows_module._acceptance_probe_profiles({"profiles": [" core ", "", "local"]}, overlay_path),  # noqa: SLF001
            ["core", "local"],
        )
        with self.assertRaisesRegex(RuntimeError, "profiles.*list"):
            workflows_module._acceptance_probe_profiles({"profiles": "core"}, overlay_path)  # noqa: SLF001
        with self.assertRaisesRegex(RuntimeError, "profiles.*at least one"):
            workflows_module._acceptance_probe_profiles({"profiles": [""]}, overlay_path)  # noqa: SLF001
        with self.assertRaisesRegex(RuntimeError, "command.*non-empty list"):
            workflows_module._acceptance_probe_command({"command": []}, overlay_path)  # noqa: SLF001
        with self.assertRaisesRegex(RuntimeError, "command.*non-empty values"):
            workflows_module._acceptance_probe_command({"command": ["python3", ""]}, overlay_path)  # noqa: SLF001

        model = {"services": [{"id": "optional", "required": False}, {"id": "required", "required": True}]}
        services_by_id = workflows_module._mcp_services_by_id(model)  # noqa: SLF001
        with mock.patch("runtime_manager.workflows.service_supports_lifecycle", return_value=(False, "")):
            self.assertEqual(
                workflows_module._optional_mcp_service_skip_reason(model, services_by_id, "optional"),  # noqa: SLF001
                "backing service unavailable",
            )
        self.assertIsNone(workflows_module._optional_mcp_service_skip_reason(model, services_by_id, "required"))  # noqa: SLF001
        self.assertIsNone(workflows_module._optional_mcp_service_skip_reason(model, services_by_id, None))  # noqa: SLF001

    def test_run_client_acceptance_probe_reports_success_and_failure_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            probe = {
                "command": ["python3", "probe.py"],
                "cwd": str(root),
                "env": {"PROBE_URL": "http://127.0.0.1"},
                "timeout_seconds": 2.0,
            }
            completed = mock.Mock(returncode=0, stdout="line1\n\nline2\n", stderr="warn\n")
            with (
                mock.patch("runtime_manager.workflows.load_runtime_env", return_value={"SKILLBOX_INGRESS_PUBLIC_PORT": "8080"}),
                mock.patch("runtime_manager.workflows.translated_runtime_env", return_value={}),
                mock.patch("runtime_manager.workflows.subprocess.run", return_value=completed) as run,
            ):
                ok, detail = workflows_module.run_client_acceptance_probe(
                    root_dir=root,
                    client_id="personal",
                    profiles=["local"],
                    probe=probe,
                )

            self.assertTrue(ok)
            self.assertEqual(detail["exit_code"], 0)
            self.assertEqual(detail["stdout_tail"], ["line1", "line2"])
            self.assertEqual(detail["stderr_tail"], ["warn"])
            self.assertEqual(detail["env_keys"], ["PROBE_URL", "SKILLBOX_INGRESS_PUBLIC_PORT"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2.0)
            self.assertEqual(run.call_args.kwargs["env"]["SKILLBOX_ACCEPTANCE_CLIENT_ID"], "personal")

            missing_probe = probe | {"cwd": str(root / "missing")}
            with (
                mock.patch("runtime_manager.workflows.load_runtime_env", return_value={}),
                mock.patch("runtime_manager.workflows.translated_runtime_env", return_value={}),
                mock.patch("runtime_manager.workflows.subprocess.run") as run,
            ):
                ok, detail = workflows_module.run_client_acceptance_probe(
                    root_dir=root,
                    client_id="personal",
                    profiles=[],
                    probe=missing_probe,
                )
            self.assertFalse(ok)
            self.assertIn("Probe cwd does not exist", detail["error"])
            run.assert_not_called()

            with (
                mock.patch("runtime_manager.workflows.load_runtime_env", return_value={}),
                mock.patch("runtime_manager.workflows.translated_runtime_env", return_value={}),
                mock.patch("runtime_manager.workflows.subprocess.run", side_effect=workflows_module.subprocess.TimeoutExpired("probe", 2)),
            ):
                ok, detail = workflows_module.run_client_acceptance_probe(
                    root_dir=root,
                    client_id="personal",
                    profiles=[],
                    probe=probe,
                )
            self.assertFalse(ok)
            self.assertIn("timed out after 2.0 seconds", detail["error"])

            with (
                mock.patch("runtime_manager.workflows.load_runtime_env", return_value={}),
                mock.patch("runtime_manager.workflows.translated_runtime_env", return_value={}),
                mock.patch("runtime_manager.workflows.subprocess.run", side_effect=OSError("permission denied")),
            ):
                ok, detail = workflows_module.run_client_acceptance_probe(
                    root_dir=root,
                    client_id="personal",
                    profiles=[],
                    probe=probe,
                )
            self.assertFalse(ok)
            self.assertEqual(detail["error"], "permission denied")

    def test_read_mcp_response_filters_strays_and_reports_errors(self) -> None:
        class FakeSelector:
            def __init__(self) -> None:
                self.stream: io.StringIO | None = None
                self.closed = False

            def register(self, stream: io.StringIO, _event: object) -> None:
                self.stream = stream

            def select(self, _timeout: float) -> list[object]:
                if self.stream is None:
                    return []
                return [object()] if self.stream.tell() < len(self.stream.getvalue()) else []

            def close(self) -> None:
                self.closed = True

        proc = mock.Mock()
        proc.stdout = io.StringIO(
            "not-json\n"
            '{"id": 99, "result": {"ignored": true}}\n'
            '{"id": 7, "result": {"ok": true}}\n'
        )
        proc.poll.return_value = None
        with mock.patch("runtime_manager.workflows.selectors.DefaultSelector", side_effect=FakeSelector):
            result, stray = workflows_module.read_mcp_response(proc, 7, timeout_seconds=1)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(stray, ["not-json", '{"id": 99, "result": {"ignored": true}}'])

        for payload, message in [
            ('{"id": 7, "error": {"message": "nope"}}\n', "nope"),
            ('{"id": 7, "error": "plain"}\n', "plain"),
            ('{"id": 7, "result": ["bad"]}\n', "non-object result"),
        ]:
            proc = mock.Mock()
            proc.stdout = io.StringIO(payload)
            proc.poll.return_value = None
            with (
                mock.patch("runtime_manager.workflows.selectors.DefaultSelector", side_effect=FakeSelector),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                workflows_module.read_mcp_response(proc, 7, timeout_seconds=1)

        proc = mock.Mock(stdout=io.StringIO(""))
        proc.poll.return_value = 2
        proc.returncode = 2
        with (
            mock.patch("runtime_manager.workflows.selectors.DefaultSelector", side_effect=FakeSelector),
            self.assertRaisesRegex(RuntimeError, "exited with code 2"),
        ):
            workflows_module.read_mcp_response(proc, 7, timeout_seconds=0)

        proc = mock.Mock(stdout=io.StringIO(""))
        proc.poll.return_value = None
        with (
            mock.patch("runtime_manager.workflows.selectors.DefaultSelector", side_effect=FakeSelector),
            self.assertRaisesRegex(RuntimeError, "Timed out waiting"),
        ):
            workflows_module.read_mcp_response(proc, 7, timeout_seconds=0)


class WorkflowFocusStewardshipHotspotTests(unittest.TestCase):
    def test_focus_bridge_verify_step_reports_missing_outputs_and_success(self) -> None:
        self.assertIsNone(workflows_module._focus_bridge_verify_step([], "acme", [], True))  # noqa: SLF001

        steps: list[dict[str, object]] = []
        bridges = [{"id": "fresh"}, {"id": "missing"}]
        with (
            mock.patch(
                "runtime_manager.workflows.bridge_outputs_state",
                side_effect=[{"state": "ok"}, {"state": "missing", "missing": ["/tmp/.env"]}],
            ),
            mock.patch("runtime_manager.workflows.emit_json") as emit_json,
        ):
            self.assertEqual(
                workflows_module._focus_bridge_verify_step(bridges, "acme", steps, True),  # noqa: SLF001
                workflows_module.EXIT_ERROR,
            )
        self.assertEqual(steps[0]["status"], "fail")
        payload = emit_json.call_args.args[0]
        self.assertEqual(payload["error"]["type"], "LOCAL_RUNTIME_ENV_OUTPUT_MISSING")
        self.assertTrue(payload["error"]["recoverable"])

        steps = []
        with mock.patch("runtime_manager.workflows.bridge_outputs_state", return_value={"state": "ok"}):
            self.assertIsNone(workflows_module._focus_bridge_verify_step(bridges, "acme", steps, False))  # noqa: SLF001
        self.assertEqual(steps[0]["detail"], {"bridges": 2})

    def test_focus_local_runtime_section_uses_shared_reconcile_or_legacy_bridges(self) -> None:
        steps: list[dict[str, object]] = []
        model = {"services": [{"id": "api", "state": "running"}]}
        with (
            mock.patch("runtime_manager.workflows.local_runtime_overlay_path", return_value=Path("/overlay.yaml")),
            mock.patch(
                "runtime_manager.workflows.reconcile_local_runtime_env",
                return_value={"status": "ok", "actions": ["write env"]},
            ) as reconcile,
            mock.patch(
                "runtime_manager.workflows.local_runtime_focus_payload",
                return_value={"local_runtime": {"status": "ready"}},
            ),
        ):
            section = workflows_module._focus_local_runtime_section(  # noqa: SLF001
                model,
                "local-core",
                [],
                "acme",
                {},
                steps,
                True,
            )
        self.assertEqual(section, {"status": "ready"})
        reconcile.assert_called_once_with(model, "local-core", overlay_path=Path("/overlay.yaml"), dry_run=False)
        self.assertEqual(steps[0]["status"], "ok")

        steps = []
        with (
            mock.patch("runtime_manager.workflows.local_runtime_overlay_path", return_value=Path("/overlay.yaml")),
            mock.patch(
                "runtime_manager.workflows.reconcile_local_runtime_env",
                return_value={"status": "blocked", "error": {"type": "LOCAL_RUNTIME_BLOCKED"}},
            ),
            mock.patch(
                "runtime_manager.workflows.local_runtime_focus_payload",
                return_value={"local_runtime": {"status": "blocked"}},
            ),
        ):
            section = workflows_module._focus_local_runtime_section(  # noqa: SLF001
                model,
                "local-core",
                [],
                "acme",
                {},
                steps,
                True,
            )
        self.assertEqual(section, {"status": "blocked"})
        self.assertEqual(steps[0]["status"], "fail")

        steps = []
        with (
            mock.patch("runtime_manager.workflows.local_runtime_overlay_path", side_effect=RuntimeError("bad overlay")),
        ):
            section = workflows_module._focus_local_runtime_section(  # noqa: SLF001
                model,
                "local-core",
                [],
                "acme",
                {},
                steps,
                True,
            )
        self.assertIsNone(section)
        self.assertEqual(steps[0]["detail"], {"error": "bad overlay"})

        self.assertIsNone(
            workflows_module._focus_local_runtime_section(model, None, [], "acme", {}, [], True)  # noqa: SLF001
        )
        with mock.patch(
            "runtime_manager.workflows.bridge_outputs_state",
            side_effect=[{"state": "ok"}, {"state": "missing"}],
        ):
            legacy = workflows_module._focus_local_runtime_section(  # noqa: SLF001
                model,
                None,
                [{"id": "fresh"}, {"id": "stale"}],
                "acme",
                {"services": [{"id": "api", "state": "running"}]},
                [],
                True,
            )
        self.assertEqual(
            legacy,
            {
                "env_bridge": [{"id": "fresh", "status": "ready"}, {"id": "stale", "status": "missing"}],
                "services": [{"id": "api", "state": "running"}],
            },
        )

    def test_focus_bridge_freshness_step_records_overlay_and_freshness_action(self) -> None:
        empty_bridges, empty_detail = workflows_module._focus_bridge_freshness_step(  # noqa: SLF001
            {"bridges": []},
            "acme",
            [],
            True,
        )
        self.assertEqual(empty_bridges, [])
        self.assertEqual(empty_detail, {})

        steps: list[dict[str, object]] = []
        model = {
            "clients": [{"id": "acme", "_overlay_path": "/clients/acme/overlay.yaml"}],
            "bridges": [{"id": "fresh"}, {"id": "stale"}],
        }

        def freshness(bridge: dict[str, object], overlay_path: str | None) -> dict[str, object]:
            return {"fresh": bridge["id"] == "fresh", "overlay_path": overlay_path}

        with mock.patch("runtime_manager.workflows.bridge_freshness", side_effect=freshness):
            bridges, detail = workflows_module._focus_bridge_freshness_step(  # noqa: SLF001
                model,
                "acme",
                steps,
                True,
            )

        self.assertEqual(bridges, model["bridges"])
        self.assertFalse(all(item["fresh"] for item in detail.values()))
        self.assertEqual(detail["fresh"]["overlay_path"], "/clients/acme/overlay.yaml")
        self.assertEqual(steps[0]["step"], "bridge-check")
        self.assertEqual(steps[0]["detail"]["action"], "will re-run stale bridges")

        steps = []
        with mock.patch("runtime_manager.workflows.bridge_freshness", return_value={"fresh": True}):
            workflows_module._focus_bridge_freshness_step(model, "acme", steps, True)  # noqa: SLF001
        self.assertEqual(steps[0]["detail"]["action"], "skip (fresh)")

    def test_stewardship_focus_and_pulse_evidence_reports_missing_invalid_stale_and_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            now = 200000.0
            missing_focus = workflows_module._stewardship_focus_evidence(root, "acme", now)  # noqa: SLF001

            focus_path = root / workflows_module.FOCUS_STATE_REL
            focus_path.parent.mkdir(parents=True)
            focus_path.write_text("{not-json", encoding="utf-8")
            invalid_focus = workflows_module._stewardship_focus_evidence(root, "acme", now)  # noqa: SLF001

            focus_path.write_text(
                json.dumps(
                    {
                        "client_id": "other",
                        "active_profiles": ["core"],
                        "focused_at": now - workflows_module.STEWARDSHIP_STALE_EVIDENCE_SECONDS - 1,
                        "skill_context_path": "/context.yaml",
                    }
                ),
                encoding="utf-8",
            )
            other_focus = workflows_module._stewardship_focus_evidence(root, "acme", now)  # noqa: SLF001

            pulse_candidates = workflows_module._stewardship_pulse_candidates(  # noqa: SLF001
                root,
                {
                    "logs": [
                        {"id": "runtime", "host_path": str(root / "logs" / "runtime")},
                        {"id": "other", "host_path": str(root / "elsewhere")},
                    ]
                },
            )
            pulse_state = root / ".skillbox-state" / "logs" / "runtime" / "pulse.state.json"
            pulse_state.parent.mkdir(parents=True)
            pulse_state.write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "updated_at": now - workflows_module.STEWARDSHIP_STALE_EVIDENCE_SECONDS - 1,
                        "cycle_count": 4,
                        "heals": 2,
                        "events_emitted": 3,
                        "active_clients": ["acme"],
                        "active_profiles": ["core"],
                    }
                ),
                encoding="utf-8",
            )
            pulse = workflows_module._stewardship_pulse_evidence(root, {"logs": []}, now)  # noqa: SLF001

        self.assertEqual(missing_focus["status"], "missing")
        self.assertEqual(invalid_focus["status"], "invalid")
        self.assertIn("Invalid JSON", invalid_focus["error"])
        self.assertEqual(other_focus["status"], "other_client")
        self.assertTrue(other_focus["stale"])
        self.assertEqual(other_focus["skill_context_path"], "/context.yaml")
        self.assertEqual(len(pulse_candidates), len({str(path) for path in pulse_candidates}))
        self.assertEqual(pulse["status"], "present")
        self.assertTrue(pulse["stale"])
        self.assertEqual(pulse["pid"], 123)
        self.assertIn(".skillbox-state/logs/runtime/pulse.state.json", pulse["path"])


class ValidationAndMmdxHotspotTests(unittest.TestCase):
    def test_build_effective_skill_owners_skips_invalid_inputs_and_uses_last_declared_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first.yaml"
            second = root / "second.yaml"
            invalid = root / "invalid.yaml"
            first.write_text(
                "version: 2\nskill_repos:\n  - path: ./skills\n    pick: [shared, first-only]\n",
                encoding="utf-8",
            )
            second.write_text(
                "version: 2\nskill_repos:\n  - path: ./skills\n    pick: [shared, second-only]\n",
                encoding="utf-8",
            )
            invalid.write_text("version: [bad", encoding="utf-8")

            declared, owners = validation_module._build_effective_skill_owners(  # noqa: SLF001
                {
                    "skills": [
                        {"id": "ignored", "kind": "packaged-skill-set"},
                        {"id": "missing", "kind": "skill-repo-set", "skill_repos_config_host_path": str(root / "missing.yaml")},
                        {"id": "invalid", "kind": "skill-repo-set", "skill_repos_config_host_path": str(invalid)},
                        {"id": "first", "kind": "skill-repo-set", "skill_repos_config_host_path": str(first)},
                        {"id": "second", "kind": "skill-repo-set", "skill_repos_config_host_path": str(second)},
                    ]
                }
            )

        self.assertEqual(declared["first"], {"shared", "first-only"})
        self.assertEqual(declared["second"], {"shared", "second-only"})
        self.assertEqual(owners["first-only"], "first")
        self.assertEqual(owners["shared"], "second")

    def test_skillset_install_state_reports_every_target_and_bundle_state(self) -> None:
        failures, warnings = validation_module._check_skillset_install_state(  # noqa: SLF001
            {"id": "skills"},
            {
                "lock_present": True,
                "skills": [
                    {
                        "name": "alpha",
                        "bundle_state": "drift",
                        "targets": [
                            {"id": "codex", "state": "drift"},
                            {"id": "claude", "state": "untracked"},
                            {"id": "home", "state": "missing"},
                        ],
                    },
                    {
                        "name": "beta",
                        "bundle_state": "untracked",
                        "targets": [{"id": "codex", "state": "ok"}],
                    },
                    {
                        "name": "gamma",
                        "bundle_state": "ok",
                        "targets": [],
                    },
                ],
            },
        )

        self.assertEqual(
            failures,
            [
                "skills: bundle digest drift detected for alpha",
                "skills: installed drift for alpha in codex",
                "skills: unmanaged install for alpha in claude",
                "skills: bundle beta is not represented in the lockfile",
            ],
        )
        self.assertEqual(warnings, ["skills: missing install for alpha in home"])

        failures, warnings = validation_module._check_skillset_install_state(  # noqa: SLF001
            {"id": "skills"},
            {
                "lock_present": False,
                "skills": [
                    {
                        "name": "beta",
                        "bundle_state": "untracked",
                        "targets": [{"id": "codex", "state": "missing"}],
                    }
                ],
            },
        )
        self.assertEqual(failures, [])
        self.assertEqual(warnings, ["skills: missing install for beta in codex"])

    def test_mmdx_match_text_lines_renders_matches_and_alternates(self) -> None:
        self.assertEqual(mmdx_open_module._mmdx_match_text_lines({}), [])  # noqa: SLF001
        self.assertEqual(
            mmdx_open_module._mmdx_match_text_lines(  # noqa: SLF001
                {"matches": [{"rel_path": "a.mmdx", "modified_at": "today"}]}
            ),
            ["  - a.mmdx today"],
        )
        self.assertEqual(
            mmdx_open_module._mmdx_match_text_lines(  # noqa: SLF001
                {
                    "selected": {"rel_path": "a.mmdx"},
                    "matches": [
                        {"rel_path": "a.mmdx", "score": 10},
                        {"rel_path": "b.mmdx", "score": 8},
                        {"rel_path": "c.mmdx"},
                        {"rel_path": "d.mmdx", "score": 1},
                        {"rel_path": "e.mmdx", "score": 0},
                    ],
                }
            ),
            ["alternates:", "  - b.mmdx score=8", "  - c.mmdx", "  - d.mmdx score=1"],
        )

    def test_mmdx_payload_text_and_script_candidates_cover_error_viewer_and_env_paths(self) -> None:
        stdout_lines, stderr_lines = mmdx_open_module.mmdx_payload_text_lines(
            {
                "error": {
                    "type": "mmdx_no_match",
                    "message": "not found",
                    "recovery_hint": "try again",
                    "next_actions": ["mmdx --no-open"],
                }
            }
        )
        self.assertEqual(stdout_lines, [])
        self.assertEqual(
            stderr_lines,
            [
                "mmdx: error mmdx_no_match",
                "not found",
                "hint: try again",
                "next: mmdx --no-open",
            ],
        )

        stdout_lines, stderr_lines = mmdx_open_module.mmdx_payload_text_lines(
            {
                "action": "opened",
                "cwd": "/repo",
                "selected": {"rel_path": "docs/a.mmdx", "score": 9},
                "viewer": {"url": "http://127.0.0.1:8080"},
                "returned": 2,
                "scanned": 20,
                "truncated": True,
                "import_candidate_note": "Generated/build artifact roots are omitted.",
                "matches": [
                    {"rel_path": "docs/a.mmdx", "score": 9},
                    {"rel_path": "docs/b.mmdx", "score": 7},
                ],
                "next_actions": ["mmdx docs/a.mmdx --no-open --format json", "mmdx --no-open"],
            }
        )
        self.assertEqual(stderr_lines, [])
        self.assertIn("path: docs/a.mmdx", stdout_lines)
        self.assertIn("note: Generated/build artifact roots are omitted.", stdout_lines)
        self.assertIn("url: http://127.0.0.1:8080", stdout_lines)
        self.assertIn(f"truncated: true (scan limit {mmdx_open_module.MMDX_MAX_SCAN_FILES})", stdout_lines)
        self.assertIn("alternates:", stdout_lines)
        self.assertIn("next:", stdout_lines)

        with (
            mock.patch.dict(
                "runtime_manager.mmdx_open.os.environ",
                {
                    "SKILLBOX_MMDX_SCRIPT": "/custom/mmd.py",
                    "MMDX_SCRIPT": "/fallback/mmd.py",
                    "SKILLBOX_MMDX_SKILL_DIR": "/skill-dir",
                    "MMDX_SKILL_DIR": "/other-skill-dir",
                },
                clear=False,
            ),
            mock.patch("runtime_manager.mmdx_open.Path.home", return_value=Path("/home/user")),
        ):
            candidates = mmdx_open_module._mmdx_script_candidates(Path("/repo"))  # noqa: SLF001
        self.assertEqual(candidates[0], Path("/custom/mmd.py"))
        self.assertEqual(candidates[2], Path("/skill-dir/scripts/mmd.py"))
        self.assertIn(Path("/home/user/.agents/skills/mmdx/scripts/mmd.py"), candidates)


class RuntimeTextRendererHotspotTests(unittest.TestCase):
    def test_print_doctor_text_renders_details_and_summary_counts(self) -> None:
        results = [
            text_renderers_module.CheckResult(
                status="pass",
                code="ok",
                message="all good",
            ),
            text_renderers_module.CheckResult(
                status="warn",
                code="warn",
                message="needs attention",
                details={"paths": ["/a", "/b"], "empty": [], "count": 2},
            ),
            text_renderers_module.CheckResult(
                status="fail",
                code="fail",
                message="broken",
            ),
        ]

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            text_renderers_module.print_doctor_text(results)
        output = buffer.getvalue()

        self.assertIn("PASS ok: all good", output)
        self.assertIn("WARN warn: needs attention", output)
        self.assertIn("     paths: /a, /b", output)
        self.assertIn("     count: 2", output)
        self.assertNotIn("empty:", output)
        self.assertIn("summary: 1 passed, 1 warnings, 1 failed", output)

    def test_print_status_text_renders_all_runtime_sections(self) -> None:
        payload = {
            "clients": [{"id": "personal"}],
            "default_client": "personal",
            "active_clients": ["personal"],
            "active_profiles": ["core"],
            "repos": [
                {
                    "id": "app",
                    "present": True,
                    "git": True,
                    "branch": "main",
                    "dirty": 1,
                    "untracked": 2,
                }
            ],
            "artifacts": [{"id": "tool", "state": "ok", "source_kind": "file"}],
            "env_files": [{"id": "env", "state": "missing", "source_kind": "manual"}],
            "skills": [
                {
                    "id": "skills",
                    "lock_present": False,
                    "lock_error": "bad lock",
                    "skills": [{"targets": [{"state": "ok"}, {"state": "drift"}]}],
                }
            ],
            "tasks": [{"id": "sync", "state": "pending", "depends_on": ["prepare"]}],
            "services": [
                {
                    "id": "api",
                    "state": "running",
                    "pid": 123,
                    "depends_on": ["db"],
                    "bootstrap_tasks": ["sync"],
                    "ownership_state": "covered",
                },
                {
                    "id": "worker",
                    "state": "declared",
                    "managed": False,
                    "manager_reason": "external process",
                },
            ],
            "parity_ledger": {"deferred_surfaces": ["billing"]},
            "blocked_services": ["worker"],
            "ingress": {
                "routes": [
                    {
                        "id": "api-route",
                        "listener": "public",
                        "path": "/api",
                        "service_id": "api",
                        "request_url": "http://127.0.0.1:8080/api",
                    }
                ]
            },
            "logs": [
                {"id": "api-log", "present": True, "files": 2, "bytes": 2048},
                {"id": "worker-log", "present": False},
            ],
            "checks": [{"id": "ready", "ok": True}, {"id": "missing", "ok": False}],
        }

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            text_renderers_module.print_status_text(payload)
        output = buffer.getvalue()

        self.assertIn("clients: personal", output)
        self.assertIn("  - app: present, git main, 1 dirty, 2 untracked", output)
        self.assertIn("  - skills: lock invalid, 1 skills, 1/2 targets healthy", output)
        self.assertIn("  - sync: pending, depends on prepare", output)
        self.assertIn("  - api [covered]: running (pid 123), depends on db, bootstrap sync", output)
        self.assertIn("  - worker: declared (external process)", output)
        self.assertIn("deferred surfaces (parity ledger):\n  - billing", output)
        self.assertIn("blocked services:\n  - worker", output)
        self.assertIn("  - api-route: public /api -> api @ http://127.0.0.1:8080/api", output)
        self.assertIn("  - api-log: 2 files, 2.0KiB", output)
        self.assertIn("  - missing: missing", output)

    def test_print_service_actions_text_renders_sync_tasks_and_service_results(self) -> None:
        payload = {
            "sync_actions": ["clone repo", "write env"],
            "tasks": [
                {"id": "prepare", "result": "done", "target": "/tmp/ready"},
                {"id": "sync"},
            ],
            "services": [
                {"id": "api", "result": "started", "pid": 123},
                {"id": "worker", "result": "skipped", "reason": "external"},
                {"id": "cron"},
            ],
        }

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            text_renderers_module.print_service_actions_text(payload)
        output = buffer.getvalue()

        self.assertIn("sync:\n  - clone repo\n  - write env", output)
        self.assertIn("tasks:\n  - prepare: done (/tmp/ready)\n  - sync: unknown", output)
        self.assertIn("services:", output)
        self.assertIn("  - api: started (pid 123)", output)
        self.assertIn("  - worker: skipped (external)", output)
        self.assertIn("  - cron: unknown", output)

    def test_print_service_logs_text_renders_deferred_missing_lines_and_empty_logs(self) -> None:
        payload = {
            "services": [
                {
                    "id": "worker",
                    "deferred": True,
                    "ownership_state": "deferred",
                    "next_action": "configure owner",
                },
                {"id": "missing", "log_file": "/logs/missing.log", "present": False},
                {"id": "api", "log_file": "/logs/api.log", "present": True, "lines": ["ready"]},
                {"id": "empty", "log_file": "/logs/empty.log", "present": True, "lines": []},
            ]
        }

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            text_renderers_module.print_service_logs_text(payload)
        output = buffer.getvalue()

        self.assertIn("[worker] (parity ledger: deferred)", output)
        self.assertIn("  next action: configure owner", output)
        self.assertIn("[missing] /logs/missing.log\n(missing)", output)
        self.assertIn("[api] /logs/api.log\nready", output)
        self.assertIn("[empty] /logs/empty.log\n(empty)", output)

    def test_print_client_blueprints_text_renders_empty_and_variable_summaries(self) -> None:
        empty_buffer = io.StringIO()
        with redirect_stdout(empty_buffer):
            text_renderers_module.print_client_blueprints_text([])
        self.assertEqual(empty_buffer.getvalue().strip(), "No client blueprints found.")

        blueprints = [
            {"id": "minimal", "description": "", "variables": []},
            {
                "id": "saas",
                "description": "SaaS app",
                "variables": [
                    {"name": "DOMAIN", "required": True},
                    {"name": "REGION", "default": "us-east-1"},
                    {"name": "OPTIONAL"},
                ],
            },
        ]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            text_renderers_module.print_client_blueprints_text(blueprints)
        output = buffer.getvalue()

        self.assertIn("minimal: No description.\n  vars: none", output)
        self.assertIn("saas: SaaS app", output)
        self.assertIn("vars: DOMAIN (required), REGION (default: us-east-1), OPTIONAL", output)

    def test_print_render_text_includes_runtime_sections(self) -> None:
        model = {
            "clients": [{"id": "personal"}, {"id": "team"}],
            "selection": {"default_client": "personal"},
            "active_clients": ["personal"],
            "active_profiles": ["core"],
            "manifest_file": "/repo/runtime.yaml",
            "repos": [{"id": "repo", "kind": "git", "path": "/repo"}],
            "artifacts": [{"id": "artifact", "kind": "archive", "path": "/a"}],
            "env_files": [{"id": "env", "kind": "dotenv", "path": "/.env"}],
            "skills": [
                {
                    "id": "skills",
                    "kind": "skill-repo-set",
                    "skill_repos_config": "/skills.yaml",
                }
            ],
            "tasks": [
                {"id": "prepare", "kind": "task"},
                {"id": "sync", "kind": "task", "depends_on": ["prepare"]},
            ],
            "services": [
                {
                    "id": "api",
                    "kind": "service",
                    "profiles": ["core"],
                    "depends_on": ["db"],
                    "bootstrap_tasks": ["sync"],
                }
            ],
            "logs": [{"id": "api-log", "path": "/logs"}],
            "checks": [{"id": "health", "type": "path_exists"}],
            "bridges": [
                {
                    "id": "legacy",
                    "env_tier": "local",
                    "legacy_targets": ["old-api"],
                }
            ],
            "ingress_routes": [
                {
                    "id": "api-route",
                    "listener": "public",
                    "path": "/api",
                    "service_id": "api",
                    "match": "prefix",
                }
            ],
        }

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_render_text(model)
        output = buffer.getvalue()

        self.assertIn("clients: personal, team", output)
        self.assertIn("active profiles: core", output)
        self.assertIn("  - sync: task depends on prepare", output)
        self.assertIn("  - api: service [core] depends on db bootstrap sync", output)
        self.assertIn("bridges: 1", output)
        self.assertIn("ingress routes: 1", output)

    def test_print_client_diff_text_renders_changed_sections_and_files(self) -> None:
        payload = {
            "client_id": "acme",
            "target_dir": "/out",
            "current_dir": "/current",
            "changed": True,
            "candidate": {"payload_tree_sha256": "candidate-sha"},
            "current": {},
            "summary": {"added": 1, "changed": 1, "removed": 1, "unchanged": 2},
            "publish_metadata": {
                "matches_candidate": False,
                "changed_fields": ["version", "notes"],
            },
            "runtime_changes": {
                "changed_sections": ["services"],
                "sections": {
                    "services": {
                        "added": ["api"],
                        "removed": ["old-api"],
                        "changed": ["worker"],
                    }
                },
            },
            "files": {
                "added": ["new.txt"],
                "removed": ["old.txt"],
                "changed": [{"path": "changed.txt"}],
            },
        }

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_client_diff_text(payload)
        output = buffer.getvalue()

        self.assertIn("client: acme", output)
        self.assertIn("current_payload_tree_sha256: (none)", output)
        self.assertIn("publish_metadata_fields: version, notes", output)
        self.assertIn("  - services: added api; removed old-api; changed worker", output)
        self.assertIn("added_files:\n  - new.txt", output)
        self.assertIn("changed_files:\n  - changed.txt", output)


class ContextRenderingHotspotTests(unittest.TestCase):
    def test_live_and_repo_status_lines_render_empty_and_populated_tables(self) -> None:
        self.assertEqual(context_rendering_module._live_status_lines([]), [])  # noqa: SLF001
        self.assertEqual(context_rendering_module._repo_state_lines([{"id": "plain", "git": False}]), [])  # noqa: SLF001

        live_lines = "\n".join(
            context_rendering_module._live_status_lines(  # noqa: SLF001
                [
                    {"id": "api", "state": "running", "pid": 123, "healthy": True},
                    {"id": "worker", "state": "starting", "healthy": False},
                ]
            )
        )
        repo_lines = "\n".join(
            context_rendering_module._repo_state_lines(  # noqa: SLF001
                [
                    {
                        "id": "app",
                        "git": True,
                        "branch": "main",
                        "dirty": 1,
                        "untracked": 2,
                        "last_commit": "abc123",
                    }
                ]
            )
        )

        self.assertIn("| api | running | 123 | yes |", live_lines)
        self.assertIn("| worker | starting | - | no |", live_lines)
        self.assertIn("| app | `main` | 1 | 2 | abc123 |", repo_lines)

    def test_write_agent_context_files_handles_dry_run_symlink_idempotence_and_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            context_dir = root / "context"

            dry_actions = context_rendering_module.write_agent_context_files(
                "body",
                root_dir=root,
                dry_run=True,
                context_dir=context_dir,
                action_prefix="write-context",
                event_subject="dry",
            )

            with mock.patch("runtime_manager.context_rendering.log_runtime_event") as log_event:
                write_actions = context_rendering_module.write_agent_context_files(
                    "body",
                    root_dir=root,
                    dry_run=False,
                    context_dir=context_dir,
                    action_prefix="write-context",
                    event_subject="client",
                )
                repeat_actions = context_rendering_module.write_agent_context_files(
                    "new body",
                    root_dir=root,
                    dry_run=False,
                    context_dir=context_dir,
                    action_prefix="write-context",
                    event_subject="client",
                )

            regular_dir = root / "regular"
            regular_dir.mkdir()
            (regular_dir / "AGENTS.md").write_text("old file\n", encoding="utf-8")
            replace_actions = context_rendering_module.write_agent_context_files(
                "regular body",
                root_dir=root,
                dry_run=False,
                context_dir=regular_dir,
                action_prefix="write-context",
            )
            context_text = (context_dir / "CLAUDE.md").read_text(encoding="utf-8")
            context_agents_is_symlink = (context_dir / "AGENTS.md").is_symlink()
            regular_agents_is_symlink = (regular_dir / "AGENTS.md").is_symlink()

        self.assertEqual(
            dry_actions,
            [
                "write-context: context/CLAUDE.md",
                "symlink-context: context/AGENTS.md -> CLAUDE.md",
            ],
        )
        self.assertEqual(
            write_actions,
            [
                "write-context: context/CLAUDE.md",
                "symlink-context: context/AGENTS.md -> CLAUDE.md",
            ],
        )
        self.assertEqual(repeat_actions, ["write-context: context/CLAUDE.md", "exists: context/AGENTS.md -> CLAUDE.md"])
        self.assertEqual(
            replace_actions,
            [
                "write-context: regular/CLAUDE.md",
                "symlink-context: regular/AGENTS.md -> CLAUDE.md",
            ],
        )
        self.assertEqual(context_text, "new body")
        self.assertTrue(context_agents_is_symlink)
        self.assertTrue(regular_agents_is_symlink)
        log_event.assert_called_once_with(
            "context.generated",
            "client",
            {"output_dir": "context"},
            root_dir=root,
        )

    def test_context_service_lines_render_manageable_and_unmanageable_services(self) -> None:
        model = {
            "services": [
                {
                    "id": "api",
                    "kind": "daemon",
                    "profiles": ["core", "dev"],
                    "depends_on": ["db"],
                },
                {"id": "worker", "kind": "service", "profiles": []},
            ]
        }

        with mock.patch(
            "runtime_manager.context_rendering.service_supports_lifecycle",
            side_effect=[(True, None), (False, "external process")],
        ):
            lines = context_rendering_module._context_service_lines(model, " CLIENT=acme")  # noqa: SLF001

        rendered = "\n".join(lines)
        self.assertIn("- **api** (daemon, core, dev) (depends on: db)", rendered)
        self.assertIn("make runtime-up CLIENT=acme PROFILE=dev SERVICE=api", rendered)
        self.assertIn("- **worker** (service, core) — external process", rendered)

    def test_session_state_lines_escape_labels_and_last_events(self) -> None:
        lines = context_rendering_module._session_state_lines(  # noqa: SLF001
            [
                {
                    "client_id": "personal",
                    "session_id": "sess-1",
                    "status": "active",
                    "updated_at": 0,
                    "label": "ship|it",
                    "last_event_type": "note",
                    "last_message": "needs|escaping",
                },
                {
                    "client_id": "acme",
                    "session_id": "sess-2",
                    "status": "done",
                    "updated_at": 1,
                    "goal": "finish",
                    "last_event_type": "",
                    "last_message": "",
                },
            ]
        )

        rendered = "\n".join(lines)
        self.assertIn("## Sessions", rendered)
        self.assertIn("| personal | `sess-1` | active | - | ship\\|it | note needs\\|escaping |", rendered)
        self.assertIn("| acme | `sess-2` | done |", rendered)
        self.assertIn("| finish | - |", rendered)
        self.assertEqual(context_rendering_module._session_state_lines([]), [])  # noqa: SLF001

    def test_collect_attention_items_summarizes_checks_services_and_recent_errors(self) -> None:
        attention = context_rendering_module._collect_attention_items(  # noqa: SLF001
            {
                "checks": [
                    {"id": "ready", "type": "path_exists", "ok": False},
                    {"id": "ok", "type": "path_exists", "ok": True},
                ],
                "logs": [
                    {
                        "id": "api-log",
                        "scanned_file": "api.log",
                        "recent_errors": ["old", "first", "second", "third"],
                    },
                    {"id": "empty-log", "recent_errors": []},
                ],
            },
            [
                {"id": "api", "state": "stopped"},
                {"id": "worker", "state": "starting"},
                {"id": "cache", "state": "running"},
            ],
        )

        self.assertIn("CHECK FAIL: **ready** (path_exists)", attention)
        self.assertIn("SERVICE DOWN: **api** (state: stopped)", attention)
        self.assertIn("SERVICE STARTING: **worker** — may not be healthy yet", attention)
        self.assertIn("RECENT ERRORS in **api-log** (api.log):", attention)
        self.assertNotIn("  `old`", attention)
        self.assertEqual(attention[-3:], ["  `first`", "  `second`", "  `third`"])

    def test_resolve_context_paths_only_resolves_explicit_relative_context_keys(self) -> None:
        client_dir = Path("/clients/acme")
        resolved = context_rendering_module._resolve_context_paths(  # noqa: SLF001
            {
                "cwd_match": ["repos/app", "~/repos/shared", "/workspace/app"],
                "plans": {
                    "plan_root": "plans/released",
                    "plan_index": "${PLAN_INDEX}",
                    "source_docs": ["docs/a.md", "https://example.test/docs"],
                },
                "deploy": {"legacy_ssh_key": "~/.ssh/key"},
                "not_a_context_key": "relative/value",
            },
            client_dir,
        )

        self.assertEqual(resolved["cwd_match"][0], "/clients/acme/repos/app")
        self.assertEqual(resolved["cwd_match"][1], "~/repos/shared")
        self.assertEqual(resolved["cwd_match"][2], "/workspace/app")
        self.assertEqual(resolved["plans"]["plan_root"], "/clients/acme/plans/released")
        self.assertEqual(resolved["plans"]["plan_index"], "${PLAN_INDEX}")
        self.assertEqual(resolved["plans"]["source_docs"][0], "/clients/acme/docs/a.md")
        self.assertEqual(resolved["plans"]["source_docs"][1], "https://example.test/docs")
        self.assertEqual(resolved["deploy"]["legacy_ssh_key"], "~/.ssh/key")
        self.assertEqual(resolved["not_a_context_key"], "relative/value")

    def test_generate_skill_context_writes_active_client_context_and_supports_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            env = {
                "SKILLBOX_CLIENTS_ROOT": "/workspace/workspace/clients",
                "SKILLBOX_CLIENTS_HOST_ROOT": str(root / "clients"),
            }
            model = {
                "env": env,
                "active_clients": ["acme"],
                "clients": [
                    {
                        "id": "acme",
                        "context": {
                            "cwd_match": ["repos/app"],
                            "workflow_builder": {"workflow_root": "workflows"},
                        },
                    },
                    {"id": "inactive", "context": {"plan_root": "plans"}},
                    {"id": "invalid", "context": "not-a-map"},
                ],
            }

            dry_actions = context_rendering_module.generate_skill_context(
                model,
                root,
                dry_run=True,
            )
            write_actions = context_rendering_module.generate_skill_context(
                model,
                root,
                dry_run=False,
            )

            context_path = root / "clients" / "acme" / "context.yaml"
            context_text = context_path.read_text(encoding="utf-8")

        self.assertEqual(dry_actions, ["write-skill-context: clients/acme/context.yaml"])
        self.assertEqual(write_actions, ["write-skill-context: clients/acme/context.yaml"])
        self.assertIn("# AUTO-GENERATED by focus. Do not edit.", context_text)
        self.assertIn("client_id: acme", context_text)
        self.assertIn(str(root / "clients" / "acme" / "repos" / "app"), context_text)
        self.assertIn(str(root / "clients" / "acme" / "workflows"), context_text)


class PublishMetadataHotspotTests(unittest.TestCase):
    def test_diff_runtime_models_reports_changed_named_sections_and_profiles(self) -> None:
        current_model = {
            "active_profiles": ["core", "dev", "dev", ""],
            "active_clients": ["personal"],
            "services": [
                {"id": "api", "image": "v1"},
                {"id": "old"},
                {"id": ""},
                "ignored",
            ],
            "tasks": [{"id": "prepare"}],
        }
        candidate_model = {
            "active_profiles": ["core", "prod"],
            "active_clients": ["personal", "team"],
            "services": [
                {"id": "api", "image": "v2"},
                {"id": "worker"},
            ],
            "tasks": [{"id": "prepare"}],
        }

        diff = publish_module.diff_runtime_models(current_model, candidate_model)

        self.assertEqual(diff["active_profiles"], {"added": ["prod"], "removed": ["dev"], "unchanged": 1})
        self.assertEqual(diff["active_clients"], {"added": ["team"], "removed": [], "unchanged": 1})
        self.assertIn("services", diff["changed_sections"])
        self.assertNotIn("tasks", diff["changed_sections"])
        self.assertEqual(diff["sections"]["services"]["added"], ["worker"])
        self.assertEqual(diff["sections"]["services"]["removed"], ["old"])
        self.assertEqual(diff["sections"]["services"]["changed"], ["api"])

    def test_build_acceptance_and_publish_metadata_normalize_payloads(self) -> None:
        acceptance_payload = {
            "active_profiles": ["core"],
            "ready": True,
            "summary": {"passed": 3},
            "steps": [
                {"step": "doctor-pre", "status": "warn"},
                {"step": "focus", "detail": {"services": ["worker", " api ", "", "api"]}},
                {"step": "mcp-smoke", "detail": {"servers_ok": [" skillbox ", "github", "github"]}},
                {"step": "doctor-post", "status": "pass"},
                {"step": ""},
                "ignored",
            ],
        }
        bundle = {
            "client_id": "personal",
            "projection": {
                "version": 2,
                "overlay_mode": "client",
                "active_profiles": ["core"],
                "active_clients": ["personal"],
                "default_client": "",
            },
            "payload_tree_sha256": "payload-sha",
            "all_entries": [("workspace/runtime.yaml", "sha1"), ("runtime-model.json", "sha2")],
            "runtime_manifest_rel": "workspace/runtime.yaml",
            "runtime_model_rel": "runtime-model.json",
        }

        with mock.patch.object(publish_module.time, "strftime", return_value="2026-05-05T01:02:03Z"):
            acceptance = publish_module.build_client_acceptance_metadata(
                bundle,
                acceptance_payload,
                client_id="personal",
                source_commit="source-sha",
            )
            publish = publish_module.build_client_publish_metadata(
                bundle,
                client_id="personal",
                source_commit="source-sha",
                acceptance_payload=acceptance,
            )

        self.assertEqual(acceptance["accepted_at"], "2026-05-05T01:02:03Z")
        self.assertEqual(acceptance["doctor_pre"], "warn")
        self.assertEqual(acceptance["doctor_post"], "pass")
        self.assertEqual(acceptance["services"], ["api", "worker"])
        self.assertEqual(acceptance["mcp_servers"], ["github", "skillbox"])
        self.assertTrue(publish_module.acceptance_metadata_matches(acceptance, acceptance))
        self.assertFalse(publish_module.acceptance_metadata_matches(None, acceptance))
        self.assertEqual(
            publish_module.summarize_acceptance_metadata(acceptance),
            {
                "present": True,
                "accepted_at": "2026-05-05T01:02:03Z",
                "source_commit": "source-sha",
                "active_profiles": ["core"],
                "services": ["api", "worker"],
                "mcp_servers": ["github", "skillbox"],
            },
        )
        self.assertEqual(publish["published_at"], "2026-05-05T01:02:03Z")
        self.assertEqual(publish["projection_version"], 2)
        self.assertEqual(publish["default_client"], "personal")
        self.assertEqual(publish["file_count"], 2)
        self.assertEqual(publish["current_dir"], "clients/personal/current")
        self.assertEqual(publish["acceptance"], "clients/personal/acceptance.json")
        self.assertTrue(publish["acceptance_present"])
        self.assertEqual(publish["acceptance_profiles"], ["core"])
        self.assertEqual(
            publish_module.summarize_acceptance_metadata(None),
            {
                "present": False,
                "accepted_at": None,
                "source_commit": None,
                "active_profiles": [],
                "services": [],
                "mcp_servers": [],
            },
        )

    def test_publish_deploy_metadata_stale_checks_payload_commit_and_archive(self) -> None:
        bundle = {"payload_tree_sha256": "Payload-SHA"}

        with tempfile.TemporaryDirectory() as tmpdir:
            client_root = Path(tmpdir)
            archive_path = client_root / "artifacts" / "source.tar.gz"
            archive_path.parent.mkdir()
            archive_path.write_text("archive", encoding="utf-8")
            metadata = {
                "archive": "artifacts/source.tar.gz",
                "payload_tree_sha256": "payload-sha",
                "source_commit": "source-sha",
            }

            self.assertFalse(
                publish_module._publish_deploy_metadata_stale(  # noqa: SLF001
                    current_deploy_metadata=metadata,
                    client_root=client_root,
                    bundle=bundle,
                    source_commit="source-sha",
                )
            )

            stale_cases = [
                {**metadata, "payload_tree_sha256": "other"},
                {**metadata, "source_commit": "other"},
                {**metadata, "archive": ""},
                {**metadata, "archive": "artifacts/missing.tar.gz"},
            ]
            for stale_metadata in stale_cases:
                with self.subTest(stale_metadata=stale_metadata):
                    self.assertTrue(
                        publish_module._publish_deploy_metadata_stale(  # noqa: SLF001
                            current_deploy_metadata=stale_metadata,
                            client_root=client_root,
                            bundle=bundle,
                            source_commit="source-sha",
                        )
                    )

    def test_resolve_publish_deploy_write_state_reuses_or_rebuilds_archive(self) -> None:
        bundle = {"payload_tree_sha256": "payload-sha", "projection": {"active_profiles": ["core"]}}
        with self.assertRaisesRegex(RuntimeError, "requires a git-backed source checkout"):
            publish_module._resolve_publish_deploy_write_state(  # noqa: SLF001
                source_commit=None,
                bundle=bundle,
                cid="personal",
                root_dir=Path("/repo"),
                deploy_artifacts_dir=Path("/artifacts"),
                current_deploy_metadata=None,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts = Path(tmpdir)
            archive_path = artifacts / "skillbox-source-sha12.tar.gz"
            archive_path.write_text("archive", encoding="utf-8")
            candidate = {"archive": "deploy-artifacts/skillbox-source-sha12.tar.gz"}

            with (
                mock.patch("runtime_manager.publish._publish_deploy_archive_paths", return_value=(candidate["archive"], archive_path.name)),
                mock.patch("runtime_manager.publish.file_sha256", return_value="sha256"),
                mock.patch("runtime_manager.publish.build_client_deploy_metadata", return_value=candidate),
                mock.patch("runtime_manager.publish.deploy_metadata_matches", return_value=True),
            ):
                self.assertEqual(
                    publish_module._resolve_publish_deploy_write_state(  # noqa: SLF001
                        source_commit="source-sha123456",
                        bundle=bundle,
                        cid="personal",
                        root_dir=Path("/repo"),
                        deploy_artifacts_dir=artifacts,
                        current_deploy_metadata=candidate,
                    ),
                    (candidate, False, False),
                )

            rebuilt = {"archive": candidate["archive"], "archive_sha256": "rebuilt"}
            with (
                mock.patch("runtime_manager.publish._publish_deploy_archive_paths", return_value=(candidate["archive"], archive_path.name)),
                mock.patch("runtime_manager.publish.file_sha256", side_effect=["old", "rebuilt"]),
                mock.patch("runtime_manager.publish.build_client_deploy_metadata", side_effect=[candidate, rebuilt]),
                mock.patch("runtime_manager.publish.deploy_metadata_matches", return_value=False),
                mock.patch("runtime_manager.publish.remove_path") as remove_path,
                mock.patch("runtime_manager.publish.write_client_source_archive") as write_archive,
            ):
                self.assertEqual(
                    publish_module._resolve_publish_deploy_write_state(  # noqa: SLF001
                        source_commit="source-sha123456",
                        bundle=bundle,
                        cid="personal",
                        root_dir=Path("/repo"),
                        deploy_artifacts_dir=artifacts,
                        current_deploy_metadata=candidate,
                    ),
                    (rebuilt, True, False),
                )
            remove_path.assert_called_once_with(artifacts)
            write_archive.assert_called_once()

    def test_diff_client_bundle_covers_validation_errors_and_success_cleanup(self) -> None:
        with (
            mock.patch("runtime_manager.publish.validate_client_id", return_value="personal"),
            mock.patch("runtime_manager.publish.resolve_client_publish_target_dir", return_value=Path("/target")),
            mock.patch("runtime_manager.publish.git_repo_state", return_value={"git": False}),
        ):
            with self.assertRaisesRegex(RuntimeError, "target must be a git repo"):
                publish_module.diff_client_bundle(
                    Path("/repo"),
                    "personal",
                    target_dir_arg=None,
                )

        with (
            mock.patch("runtime_manager.publish.validate_client_id", return_value="personal"),
            mock.patch("runtime_manager.publish.resolve_client_publish_target_dir", return_value=Path("/target")),
            mock.patch("runtime_manager.publish.git_repo_state", return_value={"git": True}),
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot combine --from-bundle"):
                publish_module.diff_client_bundle(
                    Path("/repo"),
                    "personal",
                    target_dir_arg=None,
                    from_bundle_arg="/bundle",
                    profiles=["core"],
                )

        candidate_bundle = {
            "payload_tree_sha256": "candidate",
            "projection": {"active_profiles": ["core"], "overlay_mode": "client"},
            "all_entries": [("runtime.yaml", "sha")],
        }
        current_bundle = {
            "payload_tree_sha256": "current",
            "projection": {"active_profiles": ["core"], "overlay_mode": "client"},
            "all_entries": [],
        }
        paths = {
            "client_root": Path("/target/clients/personal"),
            "current_dir": Path("/target/clients/personal/current"),
            "publish_metadata_path": Path("/target/clients/personal/publish.json"),
            "acceptance_metadata_path": Path("/target/clients/personal/acceptance.json"),
        }
        temp_bundle = mock.Mock()
        with (
            mock.patch("runtime_manager.publish.validate_client_id", return_value="personal"),
            mock.patch("runtime_manager.publish.resolve_client_publish_target_dir", return_value=Path("/target")),
            mock.patch("runtime_manager.publish.git_repo_state", return_value={"git": True}),
            mock.patch("runtime_manager.publish.git_head_commit", return_value="source-sha"),
            mock.patch(
                "runtime_manager.publish._resolve_diff_bundle",
                return_value=(Path("/bundle"), temp_bundle),
            ),
            mock.patch("runtime_manager.publish.load_client_projection_bundle", return_value=candidate_bundle),
            mock.patch("runtime_manager.publish.bundle_runtime_model", side_effect=[{"candidate": True}, {"current": True}]),
            mock.patch("runtime_manager.publish._publish_paths_payload", return_value=paths),
            mock.patch(
                "runtime_manager.publish._load_current_diff_bundle",
                return_value=(current_bundle, {"current": True}),
            ),
            mock.patch(
                "runtime_manager.publish._diff_candidate_against_current",
                return_value=({"summary": {}, "added": [], "removed": [], "changed": []}, {}, {}),
            ),
            mock.patch("runtime_manager.publish._optional_json", side_effect=[{"publish": True}, {"accepted": True}]),
            mock.patch("runtime_manager.publish.build_client_publish_metadata", return_value={"expected": True}),
            mock.patch("runtime_manager.publish.diff_publish_metadata", return_value={"matches_candidate": False}),
            mock.patch("runtime_manager.publish.bundle_matches_publish_target", return_value=False),
            mock.patch("runtime_manager.publish._client_diff_payload", return_value={"changed": True}) as payload,
        ):
            self.assertEqual(
                publish_module.diff_client_bundle(
                    Path("/repo"),
                    "personal",
                    target_dir_arg=None,
                    profiles=["core"],
                ),
                {"changed": True},
            )

        self.assertIn("compare-current", payload.call_args.kwargs["actions"][-1])
        temp_bundle.cleanup.assert_called_once()

    def test_diff_publish_metadata_and_bundle_match_publish_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            current_dir = root / "current"
            current_dir.mkdir()
            (current_dir / "runtime.yaml").write_text("runtime\n", encoding="utf-8")
            publish_metadata_path = root / "publish.json"
            entries = publish_module.directory_file_entries(current_dir)
            bundle = {
                "client_id": "personal",
                "payload_tree_sha256": "payload-sha",
                "all_entries": entries,
            }
            expected_publish = {"client_id": "personal", "payload_tree_sha256": "payload-sha"}
            publish_metadata_path.write_text(json.dumps(expected_publish), encoding="utf-8")

            self.assertTrue(
                publish_module.bundle_matches_publish_target(bundle, current_dir, publish_metadata_path)
            )

            drift = publish_module.diff_publish_metadata(
                expected_publish,
                {"client_id": "personal", "payload_tree_sha256": "new-sha", "version": 1},
            )
            self.assertTrue(drift["present"])
            self.assertFalse(drift["matches_candidate"])
            self.assertIn("version", drift["changed_fields"])
            self.assertIn("payload_tree_sha256", drift["changed_fields"])

            publish_metadata_path.write_text("{not-json", encoding="utf-8")
            self.assertFalse(
                publish_module.bundle_matches_publish_target(bundle, current_dir, publish_metadata_path)
            )
            publish_metadata_path.write_text(
                json.dumps({"client_id": "other", "payload_tree_sha256": "payload-sha"}),
                encoding="utf-8",
            )
            self.assertFalse(
                publish_module.bundle_matches_publish_target(bundle, current_dir, publish_metadata_path)
            )
            self.assertFalse(
                publish_module.bundle_matches_publish_target(bundle, root / "missing", publish_metadata_path)
            )

    def test_commit_client_publish_handles_noop_commit_and_errors(self) -> None:
        def result(returncode: int, stdout: str = "", stderr: str = "") -> mock.Mock:
            return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

        with mock.patch(
            "runtime_manager.publish.run_command",
            side_effect=[result(0), result(0)],
        ) as run_command:
            commit_hash = publish_module.commit_client_publish(Path("/repo"), "personal")

        self.assertEqual(commit_hash, "")
        self.assertEqual(run_command.call_args_list[0].args[0], ["git", "add", "-A", "--", "clients/personal"])
        self.assertEqual(run_command.call_args_list[1].args[0], ["git", "diff", "--cached", "--quiet"])

        with (
            mock.patch(
                "runtime_manager.publish.run_command",
                side_effect=[result(0), result(1), result(0)],
            ) as run_command,
            mock.patch("runtime_manager.publish.git_head_commit", return_value="commit-sha"),
        ):
            commit_hash = publish_module.commit_client_publish(Path("/repo"), "personal")

        self.assertEqual(commit_hash, "commit-sha")
        self.assertEqual(
            run_command.call_args_list[2].args[0],
            ["git", "commit", "-m", "chore(client-publish): publish personal bundle"],
        )

        with mock.patch("runtime_manager.publish.run_command", return_value=result(2, stderr="add failed")):
            with self.assertRaisesRegex(RuntimeError, "add failed"):
                publish_module.commit_client_publish(Path("/repo"), "personal")

    def test_validate_publish_target_rejects_non_git_and_blocked_dirty_paths(self) -> None:
        with mock.patch("runtime_manager.publish.git_repo_state", return_value={"git": False}):
            with self.assertRaisesRegex(RuntimeError, "target must be a git repo"):
                publish_module._validate_publish_target(Path("/target"))  # noqa: SLF001

        with (
            mock.patch("runtime_manager.publish.git_repo_state", return_value={"git": True}),
            mock.patch("runtime_manager.publish.git_dirty_paths", return_value=["README.md"]),
        ):
            with self.assertRaisesRegex(RuntimeError, "dirty working tree"):
                publish_module._validate_publish_target(Path("/target"))  # noqa: SLF001

        with (
            mock.patch("runtime_manager.publish.git_repo_state", return_value={"git": True}),
            mock.patch(
                "runtime_manager.publish.git_dirty_paths",
                return_value=["clients/personal/current/runtime.yaml"],
            ),
        ):
            publish_module._validate_publish_target(Path("/target"))  # noqa: SLF001

    def test_run_acceptance_for_publish_returns_ready_payload_and_reports_failures(self) -> None:
        ready_payload = {"ready": True, "steps": []}
        with mock.patch(
            "runtime_manager.workflows.run_manage_json_command",
            return_value=(publish_module.EXIT_OK, ready_payload),
        ) as run_acceptance:
            payload = publish_module._run_acceptance_for_publish(  # noqa: SLF001
                Path("/repo"),
                "personal",
                ["core", "mcp"],
            )

        self.assertIs(payload, ready_payload)
        self.assertEqual(
            run_acceptance.call_args.args,
            (Path("/repo"), ["acceptance", "personal", "--profile", "core", "--profile", "mcp", "--format", "json"]),
        )

        with mock.patch(
            "runtime_manager.workflows.run_manage_json_command",
            return_value=(1, {"ready": False, "error": {"message": "not ready"}}),
        ):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                publish_module._run_acceptance_for_publish(Path("/repo"), "personal", None)  # noqa: SLF001

    def test_apply_publish_changes_stages_metadata_removals_deploy_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            client_root = root / "clients" / "personal"
            current_dir = client_root / "current"
            publish_metadata_path = client_root / "publish.json"
            acceptance_metadata_path = client_root / "acceptance.json"
            deploy_metadata_path = client_root / "deploy.json"
            deploy_artifacts_dir = client_root / "artifacts"
            bundle_dir = root / "bundle"
            bundle_dir.mkdir()
            bundle = {
                "projection": {},
                "payload_tree_sha256": "payload-sha",
                "all_entries": [],
                "runtime_manifest_rel": "runtime.yaml",
                "runtime_model_rel": "runtime-model.json",
            }
            actions: list[str] = []

            with (
                mock.patch("runtime_manager.publish.stage_bundle_for_publish") as stage,
                mock.patch("runtime_manager.publish.write_json_file") as write_json,
                mock.patch("runtime_manager.publish.build_client_publish_metadata", return_value={"publish": True}),
                mock.patch("runtime_manager.publish.commit_client_publish", return_value="commit-sha") as commit,
            ):
                commit_hash = publish_module._apply_publish_changes(  # noqa: SLF001
                    payload_changed=True,
                    acceptance_metadata={"accepted": True},
                    acceptance_removed=False,
                    deploy_metadata={"archive": "artifacts/source.tar.gz"},
                    deploy_removed=False,
                    bundle=bundle,
                    bundle_dir=bundle_dir,
                    current_dir=current_dir,
                    publish_metadata_path=publish_metadata_path,
                    acceptance_metadata_path=acceptance_metadata_path,
                    deploy_metadata_path=deploy_metadata_path,
                    deploy_artifacts_dir=deploy_artifacts_dir,
                    client_root=client_root,
                    target_dir=root,
                    cid="personal",
                    source_commit="source-sha",
                    commit=True,
                    actions=actions,
                )

            self.assertEqual(commit_hash, "commit-sha")
            stage.assert_called_once_with(bundle_dir, current_dir)
            commit.assert_called_once_with(root, "personal")
            self.assertEqual(
                [call.args[0] for call in write_json.call_args_list],
                [acceptance_metadata_path, publish_metadata_path, deploy_metadata_path],
            )
            self.assertIn("write-file: clients/personal/artifacts/source.tar.gz", actions)
            self.assertIn("git-commit: commit-sha", actions)

            removal_actions: list[str] = []
            with (
                mock.patch("runtime_manager.publish.stage_bundle_for_publish"),
                mock.patch("runtime_manager.publish.write_json_file"),
                mock.patch("runtime_manager.publish.remove_path") as remove_path,
                mock.patch("runtime_manager.publish.build_client_publish_metadata", return_value={"publish": True}),
            ):
                commit_hash = publish_module._apply_publish_changes(  # noqa: SLF001
                    payload_changed=False,
                    acceptance_metadata=None,
                    acceptance_removed=True,
                    deploy_metadata=None,
                    deploy_removed=True,
                    bundle=bundle,
                    bundle_dir=bundle_dir,
                    current_dir=current_dir,
                    publish_metadata_path=publish_metadata_path,
                    acceptance_metadata_path=acceptance_metadata_path,
                    deploy_metadata_path=deploy_metadata_path,
                    deploy_artifacts_dir=deploy_artifacts_dir,
                    client_root=client_root,
                    target_dir=root,
                    cid="personal",
                    source_commit=None,
                    commit=False,
                    actions=removal_actions,
                )

            self.assertIsNone(commit_hash)
            self.assertEqual(
                [call.args[0] for call in remove_path.call_args_list],
                [acceptance_metadata_path, deploy_artifacts_dir, deploy_metadata_path],
            )
            self.assertIn("remove-file: clients/personal/acceptance.json", removal_actions)
            self.assertIn("remove-path: clients/personal/artifacts", removal_actions)

    def test_publish_client_bundle_returns_noop_or_applies_changed_bundle(self) -> None:
        bundle = {
            "projection": {"active_profiles": ["core"]},
            "payload_tree_sha256": "payload-sha",
            "all_entries": [],
        }
        target_dir = Path("/target")
        paths = {
            "client_root": target_dir / "clients" / "personal",
            "current_dir": target_dir / "clients" / "personal" / "current",
            "publish_metadata_path": target_dir / "clients" / "personal" / "publish.json",
            "acceptance_metadata_path": target_dir / "clients" / "personal" / "acceptance.json",
            "deploy_metadata_path": target_dir / "clients" / "personal" / "deploy.json",
            "deploy_artifacts_dir": target_dir / "clients" / "personal" / "artifacts",
        }

        def common_patches(
            *,
            change_state: tuple[bool, bool, bool],
            deploy_state: tuple[dict[str, object] | None, bool, bool],
        ) -> list[mock._patch]:
            return [
                mock.patch("runtime_manager.publish.validate_client_id", return_value="personal"),
                mock.patch("runtime_manager.publish.resolve_client_publish_target_dir", return_value=target_dir),
                mock.patch("runtime_manager.publish._validate_publish_target"),
                mock.patch("runtime_manager.publish._validate_publish_args"),
                mock.patch("runtime_manager.publish.git_head_commit", return_value="source-sha"),
                mock.patch("runtime_manager.publish._publish_acceptance_payload", return_value=None),
                mock.patch(
                    "runtime_manager.publish._resolve_publish_bundle",
                    return_value=(Path("/bundle"), "use-bundle: bundle", None),
                ),
                mock.patch("runtime_manager.publish.load_client_projection_bundle", return_value=bundle),
                mock.patch("runtime_manager.publish._publish_acceptance_metadata", return_value=None),
                mock.patch("runtime_manager.publish._publish_paths_payload", return_value=paths),
                mock.patch("runtime_manager.publish._optional_json", return_value=None),
                mock.patch("runtime_manager.publish._compute_publish_change_state", return_value=change_state),
                mock.patch("runtime_manager.publish._resolve_publish_deploy_state", return_value=deploy_state),
            ]

        patches = common_patches(change_state=(False, False, False), deploy_state=(None, False, False))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
            payload = publish_module.publish_client_bundle(
                Path("/repo"),
                "personal",
                target_dir_arg="/target",
            )

        self.assertFalse(payload["changed"])
        self.assertIn("publish-noop: clients/personal/current", payload["actions"])

        patches = common_patches(change_state=(True, False, False), deploy_state=(None, False, False))
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            patches[11],
            patches[12],
            mock.patch("runtime_manager.publish._apply_publish_changes", return_value="commit-sha") as apply_changes,
        ):
            payload = publish_module.publish_client_bundle(
                Path("/repo"),
                "personal",
                target_dir_arg="/target",
                commit=True,
            )

        self.assertTrue(payload["changed"])
        self.assertTrue(payload["committed"])
        self.assertEqual(payload["commit_hash"], "commit-sha")
        apply_changes.assert_called_once()

    def test_client_open_helpers_handle_bundle_separation_focus_actions_and_mcp_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_dir = root / "bundle"
            output_dir = root / "output"
            child_output = bundle_dir / "child"
            bundle_dir.mkdir()
            output_dir.mkdir()
            child_output.mkdir()

            publish_module._ensure_client_open_bundle_is_separate(bundle_dir, output_dir)  # noqa: SLF001
            with self.assertRaisesRegex(RuntimeError, "requires an output directory separate"):
                publish_module._ensure_client_open_bundle_is_separate(bundle_dir, child_output)  # noqa: SLF001

            with mock.patch("runtime_manager.publish.write_json_file", return_value=True) as write_json:
                action = publish_module._write_client_open_mcp_config(  # noqa: SLF001
                    root,
                    output_dir,
                    {"skillbox": {"command": "python3"}},
                )

        self.assertEqual(action, "write-file: output/.mcp.json")
        write_json.assert_called_once()
        self.assertEqual(
            publish_module._client_open_focus_actions(  # noqa: SLF001
                {
                    "steps": [
                        {"step": "sync", "detail": {"actions": ["clone repo", "", 42]}},
                        {"step": "noop", "detail": {"actions": "not-a-list"}},
                    ]
                }
            ),
            ["clone repo", "42"],
        )

    def test_open_client_surface_projected_combines_project_focus_context_and_mcp_actions(self) -> None:
        focus_payload = {
            "steps": [
                {"step": "sync", "detail": {"actions": ["sync action"]}},
                {"step": "up", "detail": {"actions": ["up action"]}},
            ],
            "summary": {"ok": True},
        }
        filtered_model = {"active_profiles": ["core"], "active_clients": ["personal"]}
        with (
            mock.patch(
                "runtime_manager.publish.project_client_bundle",
                return_value={
                    "actions": ["project action"],
                    "payload_tree_sha256": "payload-sha",
                    "file_count": 3,
                },
            ) as project,
            mock.patch(
                "runtime_manager.workflows.run_manage_json_command",
                return_value=(publish_module.EXIT_DRIFT, focus_payload),
            ) as focus,
            mock.patch("runtime_manager.publish._client_open_filtered_model", return_value=filtered_model),
            mock.patch("runtime_manager.publish.generate_skill_context", return_value=["context action"]),
            mock.patch(
                "runtime_manager.workflows.selected_mcp_server_configs",
                return_value=({"skillbox": {"command": "python3"}}, ["skillbox"]),
            ),
            mock.patch("runtime_manager.publish._write_client_open_mcp_config", return_value="write-file: .mcp.json"),
        ):
            payload, code = publish_module._open_client_surface_projected(  # noqa: SLF001
                Path("/repo"),
                "personal",
                Path("/output"),
                ["dev"],
            )

        self.assertEqual(code, publish_module.EXIT_DRIFT)
        self.assertEqual(payload["focus"]["status"], "warn")
        self.assertEqual(payload["focus"]["step_names"], ["sync", "up"])
        self.assertEqual(
            payload["actions"],
            ["project action", "sync action", "up action", "context action", "write-file: .mcp.json"],
        )
        project.assert_called_once_with(
            Path("/repo"),
            "personal",
            profiles=["dev"],
            output_dir_arg="/output",
            dry_run=False,
            force=True,
        )
        self.assertEqual(
            focus.call_args.args,
            (
                Path("/repo"),
                [
                    "focus",
                    "personal",
                    "--profile",
                    "dev",
                    "--context-dir",
                    "/output",
                    "--format",
                    "json",
                ],
            ),
        )

        with (
            mock.patch("runtime_manager.publish.project_client_bundle", return_value={"actions": []}),
            mock.patch(
                "runtime_manager.workflows.run_manage_json_command",
                return_value=(1, {"error": {"message": "focus failed"}}),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "focus failed"):
                publish_module._open_client_surface_projected(Path("/repo"), "personal", Path("/output"), None)  # noqa: SLF001


class SkillInventoryHotspotTests(unittest.TestCase):
    def test_collect_skill_inventory_classifies_expected_extra_and_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_dir = root / "bundles"
            install_root = root / "installed"
            bundle_dir.mkdir()
            install_root.mkdir()
            manifest = root / "manifest.txt"
            sources = root / "sources.json"
            lock = root / "lock.json"

            manifest.write_text("alpha\nmissing\n", encoding="utf-8")
            sources.write_text("{}", encoding="utf-8")
            lock.write_text(json.dumps({"version": 1, "skills": []}), encoding="utf-8")
            with zipfile.ZipFile(bundle_dir / "alpha.skill", "w") as archive:
                archive.writestr("alpha/SKILL.md", "# Alpha\n")
            with zipfile.ZipFile(bundle_dir / "extra.skill", "w") as archive:
                archive.writestr("extra/SKILL.md", "# Extra\n")
            (install_root / "alpha").mkdir()
            (install_root / "alpha" / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")

            inventory = collect_skill_inventory(
                {
                    "id": "packaged",
                    "kind": "packaged-skill-set",
                    "bundle_dir": "/bundles",
                    "bundle_dir_host_path": str(bundle_dir),
                    "manifest": "/manifest.txt",
                    "manifest_host_path": str(manifest),
                    "sources_config": "/sources.json",
                    "sources_config_host_path": str(sources),
                    "lock_path": "/lock.json",
                    "lock_path_host_path": str(lock),
                    "install_targets": [
                        {
                            "id": "codex",
                            "path": "/skills",
                            "host_path": str(install_root),
                        }
                    ],
                }
            )

        skills_by_name = {skill["name"]: skill for skill in inventory["skills"]}
        self.assertEqual(inventory["expected_skills"], ["alpha", "missing"])
        self.assertEqual(inventory["missing_bundles"], ["missing"])
        self.assertEqual(inventory["extra_bundles"], ["extra"])
        self.assertEqual(skills_by_name["alpha"]["bundle_state"], "untracked")
        self.assertEqual(skills_by_name["alpha"]["targets"][0]["state"], "untracked")
        self.assertEqual(skills_by_name["missing"]["bundle_state"], "missing")
        self.assertEqual(skills_by_name["extra"]["bundle_state"], "untracked")


class ManifestValidationHotspotTests(unittest.TestCase):
    def test_declared_skill_names_reads_pick_entries_and_repo_names(self) -> None:
        declared = validation_module._declared_skill_names(  # noqa: SLF001
            {
                "skill_repos": [
                    {"pick": ["alpha", "beta", ""]},
                    {"repo": "https://example.test/org/gamma"},
                    {"repo": "delta"},
                    {"repo": ""},
                    {},
                ]
            }
        )

        self.assertEqual(declared, {"alpha", "beta", "gamma", "delta"})

    def test_shared_client_planning_sources_require_shared_skill_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            overlay_dir = root / "clients" / "acme"
            shared_dir = root / "clients" / "_shared" / "skills"
            local_dir = overlay_dir / "local-skills"
            shared_dir.mkdir(parents=True)
            local_dir.mkdir(parents=True)
            config_path = overlay_dir / "skill-repos.yaml"
            config = {
                "skill_repos": [
                    {"path": "../_shared/skills", "pick": ["domain-planner"]},
                    {"path": "local-skills", "pick": ["domain-reviewer"]},
                    {"path": "missing-skills", "pick": ["divide-and-conquer"]},
                    {"path": "local-skills", "pick": ["skill-issue"]},
                    {
                        "path": "local-skills",
                        "pick": ["domain-scaffolder"],
                        validation_module.VENDORED_SHARED_SKILLS_ESCAPE_HATCH: True,
                    },
                    {"pick": ["domain-planner"]},
                ]
            }

            failures = validation_module._validate_shared_client_planning_sources(  # noqa: SLF001
                {"id": "acme-skills", "client": "acme"},
                config_path,
                config,
            )
            no_client_failures = validation_module._validate_shared_client_planning_sources(  # noqa: SLF001
                {"id": "global-skills"},
                config_path,
                config,
            )

        self.assertEqual(no_client_failures, [])
        self.assertEqual(len(failures), 2)
        self.assertIn("skill_repos[1] sources protected planning skills domain-reviewer", failures[0])
        self.assertIn("skill_repos[2] references missing local path", failures[1])

    def test_check_artifact_entries_reports_invalid_kinds_sync_and_url_contracts(self) -> None:
        issues = validation_module._check_artifact_entries(  # noqa: SLF001
            {
                "artifacts": [
                    {
                        "client": "ghost",
                        "source": {"kind": "ftp"},
                        "sync": {"mode": "bad"},
                    },
                    {
                        "id": "download",
                        "path": "/bin/tool",
                        "source": {
                            "kind": "url",
                            "url": "http://example.test/tool",
                            "sha256": "0" * 64,
                        },
                    },
                    {
                        "id": "url-ok",
                        "path": "/bin/ok",
                        "source": {
                            "kind": "url",
                            "url": "https://example.test/tool",
                            "sha256": "a" * 64,
                        },
                    },
                    {
                        "id": "file-ok",
                        "path": "/bin/file",
                        "source": {"kind": "file"},
                    },
                    {"id": "manual-ok", "path": "/manual", "source": {}},
                ]
            },
            {"acme"},
        )

        self.assertIn("every artifact entry must have an id", issues)
        self.assertIn("artifact (missing id) is missing path", issues)
        self.assertIn("artifact None references unknown client 'ghost'", issues)
        self.assertIn("artifact None has unsupported source.kind 'ftp'", issues)
        self.assertIn("artifact None has unsupported sync.mode 'bad'", issues)
        self.assertIn(
            "artifact download download url must use https: http://example.test/tool",
            issues,
        )

    def test_check_skill_entries_reports_required_fields_sync_and_target_contracts(self) -> None:
        issues = validation_module._check_skill_entries(  # noqa: SLF001
            {
                "skills": [
                    {
                        "client": "ghost",
                        "sync": {"mode": "bad"},
                    },
                    {
                        "id": "repo-set",
                        "kind": "skill-repo-set",
                        "sync": {"mode": "clone-and-install"},
                        "install_targets": [{"id": "codex", "path": "/skills"}],
                    },
                    {
                        "id": "targets",
                        "kind": "skill-repo-set",
                        "skill_repos_config": "/skill-repos.yaml",
                        "lock_path": "/skill-repos.lock.json",
                        "sync": {"mode": "clone-and-install"},
                        "install_targets": [
                            {"id": "codex", "path": "/skills"},
                            {"id": "codex"},
                            {"path": "/missing-id"},
                        ],
                    },
                ]
            },
            {"acme"},
        )

        self.assertIn("every skills entry must have an id", issues)
        self.assertIn("skill set None references unknown client 'ghost'", issues)
        self.assertIn("skill set (missing id) is missing bundle_dir", issues)
        self.assertIn("skill set (missing id) is missing manifest", issues)
        self.assertIn("skill set (missing id) is missing sources_config", issues)
        self.assertIn("skill set (missing id) is missing lock_path", issues)
        self.assertIn("skill set None has unsupported sync.mode 'bad'", issues)
        self.assertIn("skill set None must declare at least one install target", issues)
        self.assertIn("skill set repo-set is missing skill_repos_config", issues)
        self.assertIn("skill set repo-set is missing lock_path", issues)
        self.assertIn("skill set targets contains duplicate target ids: codex", issues)
        self.assertIn("skill set targets target codex is missing path", issues)
        self.assertIn("skill set targets contains a target without an id", issues)

    def test_validate_connector_contract_reports_pass_and_contract_violations(self) -> None:
        passed = validation_module.validate_connector_contract(
            {
                "env": {"SKILLBOX_FWC_CONNECTORS": "github,slack,linear"},
                "clients": [
                    {
                        "id": "personal",
                        "_overlay_path": "/clients/personal/overlay.yaml",
                        "connectors": [
                            {"id": "github", "capabilities": ["issues,prs"]},
                            "slack",
                        ],
                    },
                    {"id": "acme", "connectors": "linear"},
                    {"id": "empty"},
                ],
            }
        )[0]

        self.assertEqual(passed.status, "pass")
        self.assertEqual(passed.details["box_superset"], ["github", "slack", "linear"])
        self.assertEqual(
            passed.details["clients"],
            [
                {
                    "client_id": "personal",
                    "connectors": ["github", "slack"],
                    "overlay_path": "/clients/personal/overlay.yaml",
                },
                {"client_id": "acme", "connectors": ["linear"], "overlay_path": ""},
            ],
        )

        failed = validation_module.validate_connector_contract(
            {
                "env": {"SKILLBOX_FWC_CONNECTORS": "github"},
                "clients": [
                    {
                        "id": "personal",
                        "_overlay_path": "/clients/personal/overlay.yaml",
                        "connectors": [
                            "github",
                            "github",
                            "postgres",
                            "",
                            42,
                            {"id": "bad-scopes", "scopes": ["nope"]},
                        ],
                    },
                    {"id": "team", "connectors": {"bad": True}},
                ],
            }
        )[0]

        self.assertEqual(failed.status, "fail")
        issues = failed.details["issues"]
        self.assertIn("client personal connectors[4] is empty", issues)
        self.assertIn("client personal connectors[5] must be a string or mapping, got int", issues)
        self.assertIn("client personal connector 'bad-scopes' scopes must be a mapping", issues)
        self.assertIn("client personal declares duplicate connectors: github", issues)
        self.assertTrue(
            any(
                "client personal in /clients/personal/overlay.yaml declares connectors outside "
                "SKILLBOX_FWC_CONNECTORS: bad-scopes, postgres" in issue
                for issue in issues
            )
        )
        self.assertIn("client team connectors must be a comma-separated string or a list", issues)

    def test_validate_skillset_locks_and_installs_reports_config_and_lock_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing_skillset = {
                "id": "missing-config",
                "kind": "skill-repo-set",
                "skill_repos_config_host_path": str(root / "missing.yaml"),
                "lock_path_host_path": str(root / "missing.lock.json"),
            }

            missing = validation_module._validate_skillset_locks_and_installs(  # noqa: SLF001
                missing_skillset,
                {},
                {},
                set(),
            )

            bad_config = root / "bad-skill-repos.yaml"
            bad_config.write_text("not: relevant\n", encoding="utf-8")
            bad_skillset = {
                "id": "bad-config",
                "kind": "skill-repo-set",
                "skill_repos_config_host_path": str(bad_config),
                "lock_path_host_path": str(root / "bad.lock.json"),
            }
            with mock.patch(
                "runtime_manager.validation.load_skill_repos_config",
                side_effect=RuntimeError("invalid skill repo config"),
            ):
                bad = validation_module._validate_skillset_locks_and_installs(  # noqa: SLF001
                    bad_skillset,
                    {},
                    {},
                    set(),
                )

            config = root / "skill-repos.yaml"
            config.write_text("skill_repos:\n  - pick: [alpha]\n", encoding="utf-8")
            lock_missing_skillset = {
                "id": "lock-missing",
                "kind": "skill-repo-set",
                "skill_repos_config_host_path": str(config),
                "lock_path_host_path": str(root / "missing.lock.json"),
            }
            with mock.patch(
                "runtime_manager.validation.load_skill_repos_config",
                return_value={"skill_repos": [{"pick": ["alpha"]}]},
            ):
                lock_missing = validation_module._validate_skillset_locks_and_installs(  # noqa: SLF001
                    lock_missing_skillset,
                    {"lock-missing": {"alpha"}},
                    {"alpha": "lock-missing"},
                    {"alpha"},
                )

            invalid_lock = root / "invalid.lock.json"
            invalid_lock.write_text("{not-json", encoding="utf-8")
            invalid_lock_skillset = lock_missing_skillset | {
                "id": "invalid-lock",
                "lock_path_host_path": str(invalid_lock),
            }
            with mock.patch(
                "runtime_manager.validation.load_skill_repos_config",
                return_value={"skill_repos": [{"pick": ["alpha"]}]},
            ):
                invalid = validation_module._validate_skillset_locks_and_installs(  # noqa: SLF001
                    invalid_lock_skillset,
                    {"invalid-lock": {"alpha"}},
                    {"alpha": "invalid-lock"},
                    {"alpha"},
                )

            wrong_version_lock = root / "wrong-version.lock.json"
            wrong_version_lock.write_text(
                json.dumps({"version": -1, "config_sha": "stale", "skills": []}),
                encoding="utf-8",
            )
            wrong_version_skillset = lock_missing_skillset | {
                "id": "wrong-version",
                "lock_path_host_path": str(wrong_version_lock),
            }
            with mock.patch(
                "runtime_manager.validation.load_skill_repos_config",
                return_value={"skill_repos": [{"pick": ["alpha"]}]},
            ):
                wrong_version = validation_module._validate_skillset_locks_and_installs(  # noqa: SLF001
                    wrong_version_skillset,
                    {"wrong-version": {"alpha"}},
                    {"alpha": "wrong-version"},
                    {"alpha"},
                )

        self.assertIn("missing-config: skill_repos config missing", missing[0][0])
        self.assertEqual(bad[0], ["bad-config: invalid skill repo config"])
        self.assertEqual(lock_missing[3], [f"lock-missing: lockfile missing at {root / 'missing.lock.json'} — run sync"])
        self.assertIn("Invalid JSON", invalid[2][0])
        self.assertIn("wrong-version: lockfile version -1 does not match", wrong_version[2][0])

    def test_validate_skillset_locks_and_installs_reports_install_drift_and_pass_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = root / "skill-repos.yaml"
            config.write_text(
                "skill_repos:\n  - pick: [alpha, beta, missing, foreign]\n",
                encoding="utf-8",
            )
            install_root = root / "installed"
            alpha_dir = install_root / "alpha"
            beta_dir = install_root / "beta"
            extra_dir = install_root / "extra"
            alpha_dir.mkdir(parents=True)
            beta_dir.mkdir()
            extra_dir.mkdir()
            (alpha_dir / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
            (beta_dir / "SKILL.md").write_text("# Beta\n", encoding="utf-8")
            (extra_dir / "SKILL.md").write_text("# Extra\n", encoding="utf-8")
            stale_lock = root / "stale.lock.json"
            stale_lock.write_text(
                json.dumps(
                    {
                        "version": validation_module.SKILL_REPOS_LOCKFILE_VERSION,
                        "config_sha": "old-config-sha",
                        "skills": [
                            {"name": "alpha", "install_tree_sha": "not-the-current-tree"},
                            {"name": "beta", "install_tree_sha": validation_module.directory_tree_sha256(beta_dir)},
                            {"name": "foreign", "install_tree_sha": "owned-elsewhere"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stale_skillset = {
                "id": "repo-set",
                "kind": "skill-repo-set",
                "skill_repos_config_host_path": str(config),
                "lock_path_host_path": str(stale_lock),
                "install_targets": [{"id": "codex", "host_path": str(install_root)}],
            }

            with mock.patch(
                "runtime_manager.validation.load_skill_repos_config",
                return_value={"skill_repos": [{"pick": ["alpha", "beta", "missing", "foreign"]}]},
            ):
                stale = validation_module._validate_skillset_locks_and_installs(  # noqa: SLF001
                    stale_skillset,
                    {"repo-set": {"alpha", "beta", "missing", "foreign"}},
                    {"alpha": "repo-set", "beta": "repo-set", "missing": "repo-set", "foreign": "other-set"},
                    {"alpha", "beta", "missing", "foreign"},
                )

            pass_lock = root / "pass.lock.json"
            pass_lock.write_text(
                json.dumps(
                    {
                        "version": validation_module.SKILL_REPOS_LOCKFILE_VERSION,
                        "config_sha": validation_module.file_sha256(config),
                        "skills": [
                            {"name": "alpha", "install_tree_sha": validation_module.directory_tree_sha256(alpha_dir)},
                            {"name": "beta", "install_tree_sha": validation_module.directory_tree_sha256(beta_dir)},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            pass_skillset = stale_skillset | {"id": "pass-set", "lock_path_host_path": str(pass_lock)}
            with mock.patch(
                "runtime_manager.validation.load_skill_repos_config",
                return_value={"skill_repos": [{"pick": ["alpha", "beta"]}]},
            ):
                passed = validation_module._validate_skillset_locks_and_installs(  # noqa: SLF001
                    pass_skillset,
                    {"pass-set": {"alpha", "beta"}},
                    {"alpha": "pass-set", "beta": "pass-set"},
                    {"alpha", "beta", "extra"},
                )

        self.assertEqual(stale[2], ["repo-set: config changed since last sync (config_sha mismatch)"])
        self.assertIn("repo-set: SKILL_INSTALL_STALE: alpha in codex", stale[4][0])
        self.assertIn("repo-set: SKILL_NOT_INSTALLED: missing not in lockfile", stale[4])
        self.assertEqual(stale[5], ["repo-set: SKILL_EXTRA_INSTALLED: extra is installed but not declared"])
        self.assertEqual(passed, ([], [], [], [], [], []))

    def test_validate_skill_locks_and_state_covers_empty_skip_and_bucketed_results(self) -> None:
        self.assertEqual(validation_module.validate_skill_locks_and_state({"skills": []}), [])

        with (
            mock.patch("runtime_manager.validation.collect_skill_inventory", return_value={}),
            mock.patch("runtime_manager.validation._check_skillset_required_bundles", return_value=["missing bundle"]),
        ):
            required_missing = validation_module.validate_skill_locks_and_state(
                {"skills": [{"id": "bundle-set"}]}
            )
        self.assertEqual(required_missing[0].status, "fail")
        self.assertEqual(required_missing[0].details["issues"], ["missing bundle"])

        with (
            mock.patch("runtime_manager.validation.collect_skill_inventory", return_value={}),
            mock.patch("runtime_manager.validation._check_skillset_required_bundles", return_value=[]),
            mock.patch("runtime_manager.validation._check_skillset_bundle_drift", return_value=(["bundle fail"], ["bundle warn"])),
            mock.patch("runtime_manager.validation._check_skillset_lockfile", return_value=(["lock fail"], ["lock warn"])),
            mock.patch("runtime_manager.validation._check_skillset_install_state", return_value=(["install fail"], ["install warn"])),
        ):
            bucketed = validation_module.validate_skill_locks_and_state(
                {"skills": [{"id": "bundle-set"}, {"id": "repo-set", "kind": "skill-repo-set"}]}
            )
        self.assertEqual([result.status for result in bucketed], ["fail", "fail", "fail"])
        self.assertEqual(bucketed[1].details["issues"], ["lock fail"])
        self.assertEqual(bucketed[2].details["issues"], ["install fail"])

    def test_skill_repo_set_wrappers_collect_declared_names_and_distribution_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = root / "skill-repos.yaml"
            config.write_text(
                f"version: {validation_module.SKILL_REPOS_CONFIG_VERSION}\n"
                "skill_repos:\n"
                "  - pick: [alpha]\n"
                "    path: ./alpha\n"
                "  - repo: owner/beta\n"
                "    ref: main\n",
                encoding="utf-8",
            )
            model = {
                "skills": [
                    {"id": "plain"},
                    {
                        "id": "repo-set",
                        "kind": "skill-repo-set",
                        "skill_repos_config_host_path": str(config),
                    },
                    {
                        "id": "missing",
                        "kind": "skill-repo-set",
                        "skill_repos_config_host_path": str(root / "missing.yaml"),
                    },
                ]
            }

            self.assertEqual(
                validation_module._collect_all_declared_skill_names(model),  # noqa: SLF001
                {"alpha", "beta"},
            )

        distribution_result = text_renderers_module.CheckResult("pass", "distribution", "ok", {})
        with mock.patch(
            "runtime_manager.distribution.doctor.validate_distribution_doctor_checks",
            return_value=[distribution_result],
        ):
            self.assertEqual(
                validation_module.validate_skill_repo_sets({"skills": [{"id": "plain"}]}),
                [distribution_result],
            )

        repo_result = text_renderers_module.CheckResult("warn", "skill-repo-lock", "warn", {})
        with (
            mock.patch(
                "runtime_manager.distribution.doctor.validate_distribution_doctor_checks",
                return_value=[distribution_result],
            ),
            mock.patch("runtime_manager.validation._build_effective_skill_owners", return_value=({"repo-set": {"alpha"}}, {"alpha": "repo-set"})),
            mock.patch("runtime_manager.validation._collect_all_declared_skill_names", return_value={"alpha"}),
            mock.patch(
                "runtime_manager.validation._validate_skillset_locks_and_installs",
                return_value=(["config fail"], [], [], [], [], []),
            ),
            mock.patch("runtime_manager.validation._build_skill_repo_results", return_value=[repo_result]) as build_results,
        ):
            self.assertEqual(
                validation_module.validate_skill_repo_sets(
                    {"skills": [{"id": "repo-set", "kind": "skill-repo-set"}]}
                ),
                [repo_result, distribution_result],
            )
        build_results.assert_called_once()

    def test_install_skillset_bundles_reports_dry_run_and_extract_hashes(self) -> None:
        skillset = {"install_targets": [{"id": "codex", "host_path": "/target"}]}
        inventory = {
            "expected_skills": ["alpha"],
            "bundles": {"alpha": {"host_path": "/bundles/alpha.skill"}},
        }

        hashes, actions = validation_module._install_skillset_bundles(  # noqa: SLF001
            skillset,
            inventory,
            dry_run=True,
        )
        self.assertEqual(hashes, {"alpha": {}})
        self.assertEqual(actions, ["install-skill: /bundles/alpha.skill -> /target/alpha"])

        with mock.patch("runtime_manager.publish.extract_bundle_to_target", return_value="tree-sha") as extract:
            hashes, actions = validation_module._install_skillset_bundles(  # noqa: SLF001
                skillset,
                inventory,
                dry_run=False,
            )
        self.assertEqual(hashes, {"alpha": {"codex": "tree-sha"}})
        self.assertEqual(actions, ["install-skill: /bundles/alpha.skill -> /target/alpha"])
        extract.assert_called_once()

    def test_manifest_validation_helpers_cover_env_service_duplicate_and_lock_failures(self) -> None:
        duplicate_model = {
            "selection": {"default_client": "missing"},
            "clients": [{"id": "acme"}, {"id": "acme"}, {}],
            "repos": [{"id": "repo", "path": "/repo"}, {"id": "repo", "path": "/repo"}],
            "artifacts": [{"id": "artifact", "path": "/artifact"}, {"id": "artifact", "path": "/artifact"}],
            "env_files": [{"id": "env", "path": "/env"}, {"id": "env", "path": "/env"}],
            "skills": [{"id": "skills"}, {"id": "skills"}],
            "tasks": [{"id": "task"}, {"id": "task"}],
            "services": [{"id": "service"}, {"id": "service"}],
            "logs": [{"id": "log", "path": "/log"}, {"id": "log", "path": "/log"}],
            "checks": [{"id": "check"}, {"id": "check"}],
            "ingress_routes": [{"id": "route"}, {"id": "route"}],
        }
        duplicate_issues, declared_clients = validation_module._check_top_level_duplicates(  # noqa: SLF001
            duplicate_model
        )
        self.assertEqual(declared_clients, {"acme"})
        self.assertIn("clients contain duplicate ids: acme", duplicate_issues)
        self.assertIn("every client entry must have an id", duplicate_issues)
        self.assertIn("selection.default_client references unknown client 'missing'", duplicate_issues)
        self.assertIn("env_files contain duplicate paths: /env", duplicate_issues)

        env_issues = validation_module._check_env_file_entries(  # noqa: SLF001
            {
                "env_files": [
                    {
                        "client": "ghost",
                        "repo": "missing-repo",
                        "source": {"kind": "bad"},
                        "sync": {"mode": "bad"},
                    },
                    {
                        "id": "file-env",
                        "path": "/env/file.env",
                        "source": {"kind": "file"},
                    },
                ]
            },
            {"acme"},
            {"repo"},
        )
        self.assertIn("every env_files entry must have an id", env_issues)
        self.assertIn("env file (missing id) is missing path", env_issues)
        self.assertIn("env file None references unknown client 'ghost'", env_issues)
        self.assertIn("env file None references unknown repo 'missing-repo'", env_issues)
        self.assertIn("env file None has unsupported source.kind 'bad'", env_issues)
        self.assertIn("env file None has unsupported sync.mode 'bad'", env_issues)
        self.assertIn("env file file-env is file-backed but missing source.path", env_issues)

        service_issues, dependency_map, services_by_id, service_ids = validation_module._check_service_entries(  # noqa: SLF001
            {
                "services": [
                    {
                        "client": "ghost",
                        "repo": "missing-repo",
                        "artifact": "missing-artifact",
                        "log": "missing-log",
                    },
                    {
                        "id": "api",
                        "command": "python api.py",
                        "depends_on": ["artifact"],
                        "bootstrap_tasks": ["task"],
                    },
                ]
            },
            {"acme"},
            {"repo"},
            {"artifact"},
            {"log"},
            {"task"},
        )
        self.assertIn("every service entry must have an id", service_issues)
        self.assertIn("service None references unknown client 'ghost'", service_issues)
        self.assertIn("service None references unknown repo 'missing-repo'", service_issues)
        self.assertIn("service None references unknown artifact 'missing-artifact'", service_issues)
        self.assertIn("service None references unknown log 'missing-log'", service_issues)
        self.assertIn("service (missing id) is missing command", service_issues)
        self.assertEqual(dependency_map["api"], ["artifact"])
        self.assertEqual(services_by_id["api"]["command"], "python api.py")
        self.assertEqual(service_ids, {"api"})

        skillset = {"id": "repo-set", "install_targets": [{"id": "codex"}, {"id": "claude"}]}
        inventory = {
            "manifest_sha256": "manifest-sha",
            "sources_config_sha256": "sources-sha",
            "expected_skills": ["missing", "alpha"],
            "bundles": {
                "alpha": {
                    "bundle_sha256": "bundle-sha",
                    "bundle_tree_sha256": "tree-sha",
                }
            },
        }
        header_failures = validation_module._lockfile_header_failures(  # noqa: SLF001
            skillset,
            inventory,
            {
                "version": -1,
                "id": "other",
                "manifest_sha256": "old",
                "sources_config_sha256": "old",
            },
        )
        self.assertEqual(len(header_failures), 4)
        self.assertEqual(
            validation_module._lockfile_header_failures(  # noqa: SLF001
                skillset,
                inventory,
                {
                    "version": validation_module.LOCKFILE_VERSION,
                    "id": "repo-set",
                    "manifest_sha256": "manifest-sha",
                    "sources_config_sha256": "sources-sha",
                },
            ),
            [],
        )
        record_failures = validation_module._lockfile_skill_record_failures(  # noqa: SLF001
            skillset,
            inventory,
            {
                "alpha": {
                    "bundle_sha256": "old-bundle",
                    "bundle_tree_sha256": "old-tree",
                    "targets_by_id": {"unexpected": {}},
                }
            },
        )
        self.assertIn("repo-set: lockfile is missing skill missing", record_failures)
        self.assertIn("repo-set: lockfile bundle digest is stale for alpha", record_failures)
        self.assertIn("repo-set: lockfile bundle tree digest is stale for alpha", record_failures)
        self.assertIn("repo-set: lockfile contains unexpected targets for alpha: unexpected", record_failures)
        self.assertIn("repo-set: lockfile is missing targets for alpha: claude, codex", record_failures)


class OnboardWorkflowHotspotTests(unittest.TestCase):
    def test_run_onboard_dry_run_emits_scaffold_and_skip_steps(self) -> None:
        emitted: list[dict] = []
        with (
            mock.patch("runtime_manager.workflows.validate_client_id", return_value="acme"),
            mock.patch("runtime_manager.workflows.parse_key_value_assignments", return_value={"tier": "dev"}),
            mock.patch(
                "runtime_manager.workflows.scaffold_client_overlay",
                return_value=(["would-write: overlay.yaml"], {"blueprint": "default"}),
            ),
            mock.patch("runtime_manager.workflows.emit_json", side_effect=emitted.append),
        ):
            code = run_onboard(
                root_dir=Path("/repo"),
                client_id="acme",
                label=None,
                default_cwd=None,
                root_path=None,
                blueprint_name=None,
                set_args=["tier=dev"],
                dry_run=True,
                force=False,
                wait_seconds=0.0,
                fmt="json",
            )

        self.assertEqual(code, 0)
        self.assertEqual([step["step"] for step in emitted[0]["steps"]], [
            "scaffold",
            "sync",
            "bootstrap",
            "up",
            "context",
            "verify",
        ])
        self.assertEqual(emitted[0]["steps"][0]["status"], "ok")
        self.assertTrue(all(step["status"] == "skip" for step in emitted[0]["steps"][1:]))

    def test_run_onboard_scaffold_failure_emits_classified_error(self) -> None:
        emitted: list[dict] = []
        with (
            mock.patch("runtime_manager.workflows.validate_client_id", side_effect=RuntimeError("bad client")),
            mock.patch("runtime_manager.workflows.classify_error", return_value={"error": {"message": "bad client"}}),
            mock.patch("runtime_manager.workflows.emit_json", side_effect=emitted.append),
        ):
            code = run_onboard(
                root_dir=Path("/repo"),
                client_id="bad",
                label=None,
                default_cwd=None,
                root_path=None,
                blueprint_name=None,
                set_args=[],
                dry_run=False,
                force=False,
                wait_seconds=0.0,
                fmt="json",
            )

        self.assertEqual(code, 1)
        self.assertEqual(emitted[0]["steps"], [{"step": "scaffold", "status": "fail", "detail": {"error": "bad client"}}])
        self.assertEqual(emitted[0]["error"]["message"], "bad client")


if __name__ == "__main__":
    unittest.main()
