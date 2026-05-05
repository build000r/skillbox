from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
PULSE_MODULE = SourceFileLoader(
    "skillbox_pulse",
    str((ENV_MANAGER_DIR / "pulse.py").resolve()),
).load_module()


class PulseTests(unittest.TestCase):
    def test_main_routes_status_stop_and_run_with_env_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                mock.patch.object(PULSE_MODULE.sys, "argv", ["pulse.py", "--root-dir", str(root), "status"]),
                mock.patch.object(PULSE_MODULE, "print_status", return_value=7) as print_status,
            ):
                self.assertEqual(PULSE_MODULE.main(), 7)
            print_status.assert_called_once_with(root.resolve())

            with (
                mock.patch.object(PULSE_MODULE.sys, "argv", ["pulse.py", "--root-dir", str(root), "stop"]),
                mock.patch.object(PULSE_MODULE, "existing_pid", return_value=None),
                mock.patch.object(PULSE_MODULE.os, "kill") as kill,
            ):
                self.assertEqual(PULSE_MODULE.main(), 0)
            kill.assert_not_called()

            with (
                mock.patch.object(PULSE_MODULE.sys, "argv", ["pulse.py", "--root-dir", str(root), "stop"]),
                mock.patch.object(PULSE_MODULE, "existing_pid", return_value=123),
                mock.patch.object(PULSE_MODULE.os, "kill") as kill,
            ):
                self.assertEqual(PULSE_MODULE.main(), 0)
            kill.assert_called_once_with(123, PULSE_MODULE.signal.SIGTERM)

            env = {
                "SKILLBOX_PULSE_INTERVAL": "9",
                "SKILLBOX_PULSE_CLIENTS": "personal, team",
                "SKILLBOX_PULSE_PROFILES": "core local",
                "SKILLBOX_PULSE_UNHEALTHY_GRACE_SECONDS": "12.5",
            }
            with (
                mock.patch.object(PULSE_MODULE.sys, "argv", ["pulse.py", "--root-dir", str(root), "run"]),
                mock.patch.dict(PULSE_MODULE.os.environ, env, clear=False),
                mock.patch.object(PULSE_MODULE, "run_daemon", return_value=3) as run_daemon,
            ):
                self.assertEqual(PULSE_MODULE.main(), 3)

            run_daemon.assert_called_once_with(
                root.resolve(),
                interval=9,
                auto_restart=True,
                auto_sync=False,
                active_clients=["personal", "team"],
                active_profiles=["core", "local"],
                unhealthy_grace_seconds=12.5,
            )

    def test_main_cli_run_options_override_restart_sync_and_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            argv = [
                "pulse.py",
                "--root-dir",
                str(root),
                "run",
                "--interval",
                "4",
                "--no-restart",
                "--auto-sync",
                "--client",
                "personal",
                "--client",
                "team",
                "--profile",
                "local",
                "--unhealthy-grace-seconds",
                "2.5",
            ]
            with (
                mock.patch.object(PULSE_MODULE.sys, "argv", argv),
                mock.patch.object(PULSE_MODULE, "run_daemon", return_value=0) as run_daemon,
            ):
                self.assertEqual(PULSE_MODULE.main(), 0)

            run_daemon.assert_called_once_with(
                root.resolve(),
                interval=4,
                auto_restart=False,
                auto_sync=True,
                active_clients=["personal", "team"],
                active_profiles=["local"],
                unhealthy_grace_seconds=2.5,
            )

    def test_run_daemon_handles_existing_pid_and_single_shutdown_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.object(PULSE_MODULE, "existing_pid", return_value=456):
                self.assertEqual(PULSE_MODULE.run_daemon(root), 1)

            PULSE_MODULE._shutdown = False

            def reconcile_once(_root: Path, state: object, **_kwargs: object) -> None:
                state.cycle_count = 1
                PULSE_MODULE._shutdown = True

            with (
                mock.patch.object(PULSE_MODULE, "existing_pid", return_value=None),
                mock.patch.object(PULSE_MODULE, "_open_log"),
                mock.patch.object(PULSE_MODULE, "write_pid"),
                mock.patch.object(PULSE_MODULE.signal, "signal"),
                mock.patch.object(PULSE_MODULE, "log_runtime_event") as log_runtime_event,
                mock.patch.object(PULSE_MODULE, "log"),
                mock.patch.object(PULSE_MODULE, "reconcile_once", side_effect=reconcile_once),
                mock.patch.object(PULSE_MODULE, "remove_pid") as remove_pid,
            ):
                self.assertEqual(PULSE_MODULE.run_daemon(root, interval=1, active_clients=["personal"]), 0)

            self.assertEqual(log_runtime_event.call_args_list[0].args[:2], ("pulse.started", "daemon"))
            self.assertEqual(log_runtime_event.call_args_list[-1].args[:2], ("pulse.stopped", "daemon"))
            remove_pid.assert_called_once_with(root)
            PULSE_MODULE._shutdown = False

    def test_scope_float_and_restart_cleanup_helpers_handle_edge_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cleanup_path = root / "cache"
            cleanup_path.mkdir()
            (root / "cache.stale-pulse-20260505-010203").mkdir()
            missing = root / "missing"

            with (
                mock.patch.object(PULSE_MODULE.time, "strftime", return_value="20260505-010203"),
                mock.patch.object(PULSE_MODULE, "log"),
            ):
                moved = PULSE_MODULE._move_restart_cleanup_paths(  # noqa: SLF001
                    {"id": "web", "restart_cleanup_paths": ["", "missing", "cache"]},
                    root,
                )

            self.assertFalse(cleanup_path.exists())
            self.assertEqual(
                moved,
                [
                    {
                        "from": str(cleanup_path),
                        "to": str(root / "cache.stale-pulse-20260505-010203-1"),
                    }
                ],
            )
            self.assertFalse(missing.exists())
            self.assertEqual(
                PULSE_MODULE._move_restart_cleanup_paths({"restart_cleanup_paths": "bad"}, root),  # noqa: SLF001
                [],
            )

            with mock.patch.dict(PULSE_MODULE.os.environ, {"PULSE_SCOPE": "a,b c"}, clear=False):
                self.assertEqual(PULSE_MODULE._scope_from_cli_or_env(None, "PULSE_SCOPE"), ["a", "b", "c"])  # noqa: SLF001
            self.assertEqual(PULSE_MODULE._scope_from_cli_or_env([" x ", ""], "PULSE_SCOPE"), ["x"])  # noqa: SLF001

            with (
                mock.patch.dict(PULSE_MODULE.os.environ, {"PULSE_FLOAT": "bad"}, clear=False),
                mock.patch.object(PULSE_MODULE, "log") as log,
            ):
                self.assertEqual(PULSE_MODULE._float_from_cli_or_env(None, "PULSE_FLOAT", 4.0), 4.0)  # noqa: SLF001
            log.assert_called_once()
            self.assertEqual(PULSE_MODULE._float_from_cli_or_env(3.5, "PULSE_FLOAT", 4.0), 3.5)  # noqa: SLF001

    def test_pid_hash_status_state_and_config_change_helpers_cover_persisted_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pid_path = root / PULSE_MODULE.PID_REL
            with mock.patch.object(PULSE_MODULE.os, "getpid", return_value=321):
                self.assertEqual(PULSE_MODULE.write_pid(root), pid_path)
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "321\n")

            with mock.patch.object(PULSE_MODULE, "process_is_running", return_value=True):
                self.assertEqual(PULSE_MODULE.existing_pid(root), 321)
            pid_path.write_text("bad\n", encoding="utf-8")
            self.assertIsNone(PULSE_MODULE.existing_pid(root))
            pid_path.write_text("456\n", encoding="utf-8")
            with mock.patch.object(PULSE_MODULE, "process_is_running", return_value=False):
                self.assertIsNone(PULSE_MODULE.existing_pid(root))
            self.assertFalse(pid_path.exists())
            PULSE_MODULE.remove_pid(root)

            runtime_yaml = root / "workspace" / "runtime.yaml"
            runtime_yaml.parent.mkdir()
            runtime_yaml.write_text("version: 1\n", encoding="utf-8")
            overlay = root / "clients" / "personal" / "overlay.yaml"
            overlay.parent.mkdir(parents=True)
            overlay.write_text("client:\n  id: personal\n", encoding="utf-8")
            (root / ".env").write_text("A=1\n", encoding="utf-8")
            with (
                mock.patch.object(PULSE_MODULE, "load_runtime_env", return_value={}),
                mock.patch.object(PULSE_MODULE, "client_overlay_paths", return_value=[overlay]),
            ):
                self.assertEqual(len(PULSE_MODULE._model_config_hash(root)), 16)  # noqa: SLF001

            check_path = root / "ready"
            check_path.write_text("ok\n", encoding="utf-8")
            model = {
                "services": [{"id": "api"}],
                "checks": [
                    {"id": "ready", "type": "path_exists", "host_path": str(check_path)},
                    {"id": "custom", "type": "external"},
                ],
            }
            with mock.patch.object(PULSE_MODULE, "probe_service", return_value={"state": "running"}):
                self.assertEqual(PULSE_MODULE._snapshot_services(model), {"api": {"state": "running"}})  # noqa: SLF001
            self.assertEqual(PULSE_MODULE._snapshot_checks(model), {"ready": True, "custom": True})  # noqa: SLF001

            with mock.patch.object(PULSE_MODULE, "build_runtime_model", side_effect=RuntimeError("bad model")):
                self.assertIsNone(PULSE_MODULE._load_pulse_model(root, None, None))  # noqa: SLF001
            with (
                mock.patch.object(PULSE_MODULE, "build_runtime_model", return_value={"clients": []}),
                mock.patch.object(PULSE_MODULE, "normalize_active_profiles", return_value={"core"}),
                mock.patch.object(PULSE_MODULE, "normalize_active_clients", side_effect=RuntimeError("bad client")),
                mock.patch.object(PULSE_MODULE, "filter_model", return_value={"filtered": True}),
            ):
                loaded = PULSE_MODULE._load_pulse_model(root, ["missing"], ["core"])  # noqa: SLF001
            self.assertEqual(loaded, ({"filtered": True}, {"core"}, set()))

            state = PULSE_MODULE.PulseState()
            with mock.patch.object(PULSE_MODULE, "_model_config_hash", return_value="hash-1"):
                PULSE_MODULE._handle_pulse_config_change(root, state, model, auto_sync=False)  # noqa: SLF001
            self.assertEqual(state.config_hash, "hash-1")

            with (
                mock.patch.object(PULSE_MODULE, "_model_config_hash", return_value="hash-2"),
                mock.patch.object(PULSE_MODULE, "sync_runtime", return_value=["sync"]),
                mock.patch.object(PULSE_MODULE, "log_runtime_event") as event,
                mock.patch.object(PULSE_MODULE, "log"),
            ):
                PULSE_MODULE._handle_pulse_config_change(root, state, model, auto_sync=True)  # noqa: SLF001
            self.assertEqual(state.config_hash, "hash-2")
            self.assertEqual(state.events_emitted, 2)
            self.assertEqual(event.call_args_list[0].args[:2], ("pulse.config_changed", "runtime"))
            self.assertEqual(event.call_args_list[1].args[:2], ("pulse.auto_sync", "runtime"))

            with (
                mock.patch.object(PULSE_MODULE, "sync_runtime", side_effect=RuntimeError("sync failed")),
                mock.patch.object(PULSE_MODULE, "log") as log,
            ):
                PULSE_MODULE._pulse_auto_sync(model, state)  # noqa: SLF001
            self.assertIn("auto-sync failed", log.call_args.args[1])

            with (
                mock.patch.object(PULSE_MODULE, "existing_pid", return_value=None),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(PULSE_MODULE.print_status(root), 0)
            self.assertIn("not running", stdout.getvalue())

            with (
                mock.patch.object(PULSE_MODULE, "existing_pid", return_value=999),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(PULSE_MODULE.print_status(root), 0)
            self.assertIn("running (pid 999), no state file", stdout.getvalue())

            state_path = root / PULSE_MODULE.STATE_REL
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text("{bad-json", encoding="utf-8")
            with (
                mock.patch.object(PULSE_MODULE, "existing_pid", return_value=999),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(PULSE_MODULE.print_status(root), 1)
            self.assertIn("error reading state", stdout.getvalue())
            self.assertEqual(PULSE_MODULE.read_state(root)["state_error"], "failed to read state file")

            state_path.write_text(
                json.dumps(
                    {
                        "pid": 321,
                        "updated_at": 10.0,
                        "interval": 3,
                        "cycle_count": 2,
                        "heals": 1,
                        "events_emitted": 4,
                        "service_states": {"api": "running", "worker": "down", "job": "starting"},
                        "check_states": {"ready": True, "db": False},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(PULSE_MODULE, "existing_pid", return_value=321),
                mock.patch.object(PULSE_MODULE.time, "time", return_value=16.0),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(PULSE_MODULE.print_status(root), 0)
            output = stdout.getvalue()
            self.assertIn("pulse: running (pid 321)", output)
            self.assertIn("+ api: running", output)
            self.assertIn("failed checks: db", output)

            with (
                mock.patch.object(PULSE_MODULE, "existing_pid", return_value=321),
                mock.patch.object(PULSE_MODULE.time, "time", return_value=16.0),
            ):
                state_payload = PULSE_MODULE.read_state(root)
            self.assertTrue(state_payload["running"])
            self.assertEqual(state_payload["seconds_since_tick"], 6.0)

            PULSE_MODULE._shutdown = False
            with mock.patch.object(PULSE_MODULE, "log") as log:
                PULSE_MODULE._handle_signal(15, None)  # noqa: SLF001
            self.assertTrue(PULSE_MODULE._shutdown)
            self.assertIn("received signal 15", log.call_args.args[1])
            PULSE_MODULE._shutdown = False

    def test_pulse_service_transition_logs_crashes_changes_and_autorestart(self) -> None:
        state = PULSE_MODULE.PulseState()
        service = {"id": "web", "command": "python3 -m http.server"}

        with (
            mock.patch.object(PULSE_MODULE, "log_runtime_event") as event,
            mock.patch.object(PULSE_MODULE, "log") as log,
            mock.patch.object(PULSE_MODULE, "_service_can_autorestart", return_value=True),
            mock.patch.object(PULSE_MODULE, "_restart_with_backoff", return_value=True) as restart,
        ):
            result = PULSE_MODULE._pulse_service_transition(  # noqa: SLF001
                {},
                state,
                service,
                "web",
                "running",
                "down",
                auto_restart=True,
                now=100.0,
            )

        self.assertEqual(result, "running")
        self.assertEqual(state.events_emitted, 1)
        event.assert_called_once_with("pulse.service_crashed", "web", {"from": "running", "to": "down"})
        log.assert_called_once_with("warn", "service web: running -> down")
        restart.assert_called_once_with({}, state, service, "web", now=100.0, reason="crashed")

        with (
            mock.patch.object(PULSE_MODULE, "log_runtime_event") as event,
            mock.patch.object(PULSE_MODULE, "log") as log,
            mock.patch.object(PULSE_MODULE, "_service_can_autorestart", return_value=True),
            mock.patch.object(PULSE_MODULE, "_restart_with_backoff") as restart,
        ):
            result = PULSE_MODULE._pulse_service_transition(  # noqa: SLF001
                {},
                state,
                service,
                "web",
                "down",
                "running",
                auto_restart=True,
                now=101.0,
            )

        self.assertEqual(result, "running")
        event.assert_called_once_with("pulse.service_state_changed", "web", {"from": "down", "to": "running"})
        log.assert_called_once_with("info", "service web: down -> running")
        restart.assert_not_called()

        with (
            mock.patch.object(PULSE_MODULE, "log_runtime_event"),
            mock.patch.object(PULSE_MODULE, "log"),
            mock.patch.object(PULSE_MODULE, "_service_can_autorestart", return_value=False),
            mock.patch.object(PULSE_MODULE, "_restart_with_backoff") as restart,
        ):
            result = PULSE_MODULE._pulse_service_transition(  # noqa: SLF001
                {},
                state,
                service,
                "web",
                "starting",
                "declared",
                auto_restart=True,
                now=102.0,
            )

        self.assertEqual(result, "declared")
        restart.assert_not_called()

    def test_reconcile_scopes_to_requested_client_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = {
                "clients": [{"id": "personal"}],
                "selection": {},
                "services": [],
                "checks": [],
            }
            captured: dict[str, set[str]] = {}

            def fake_filter(
                raw_model: dict,
                profiles: set[str],
                clients: set[str],
            ) -> dict:
                captured["profiles"] = profiles
                captured["clients"] = clients
                return raw_model | {
                    "active_profiles": sorted(profiles),
                    "active_clients": sorted(clients),
                }

            state = PULSE_MODULE.PulseState()
            with (
                mock.patch.object(PULSE_MODULE, "build_runtime_model", return_value=model),
                mock.patch.object(PULSE_MODULE, "filter_model", side_effect=fake_filter),
                mock.patch.object(PULSE_MODULE, "_model_config_hash", return_value="hash"),
                mock.patch.object(PULSE_MODULE, "_snapshot_services", return_value={}),
                mock.patch.object(PULSE_MODULE, "_snapshot_checks", return_value={}),
            ):
                PULSE_MODULE.reconcile_once(
                    root,
                    state,
                    active_clients=["personal"],
                    active_profiles=["local-core"],
                )

            self.assertEqual(captured["clients"], {"personal"})
            self.assertEqual(captured["profiles"], {"core", "local-core"})
            snapshot = (root / "logs" / "runtime" / "pulse.state.json").read_text(encoding="utf-8")
            self.assertIn('"active_clients": [\n    "personal"\n  ]', snapshot)
            self.assertIn('"local-core"', snapshot)

    def test_live_unhealthy_http_service_restarts_after_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = {
                "id": "web",
                "kind": "http",
                "healthcheck": {"type": "http", "url": "http://127.0.0.1:3001"},
            }
            model = {
                "clients": [{"id": "personal"}],
                "selection": {},
                "services": [service],
                "checks": [],
            }
            state = PULSE_MODULE.PulseState()
            state.service_states["web"] = "starting"
            state.unhealthy_since["web"] = 10.0

            with (
                mock.patch.object(PULSE_MODULE, "build_runtime_model", return_value=model),
                mock.patch.object(PULSE_MODULE, "filter_model", side_effect=lambda m, _p, _c: m),
                mock.patch.object(PULSE_MODULE, "_model_config_hash", return_value="hash"),
                mock.patch.object(
                    PULSE_MODULE,
                    "_snapshot_services",
                    return_value={"web": {"state": "starting", "pid": 123, "url": "http://127.0.0.1:3001"}},
                ),
                mock.patch.object(PULSE_MODULE, "_snapshot_checks", return_value={}),
                mock.patch.object(PULSE_MODULE, "service_supports_lifecycle", return_value=(True, "")),
                mock.patch.object(PULSE_MODULE, "_restart_service", return_value=True) as restart_service,
                mock.patch.object(PULSE_MODULE, "log_runtime_event"),
                mock.patch.object(PULSE_MODULE.time, "monotonic", return_value=75.0),
            ):
                PULSE_MODULE.reconcile_once(
                    root,
                    state,
                    active_clients=["personal"],
                    active_profiles=["local-core"],
                    unhealthy_grace_seconds=30.0,
                )

            restart_service.assert_called_once()
            self.assertEqual(state.service_states["web"], "running")
            self.assertEqual(state.heals, 1)
            self.assertNotIn("web", state.unhealthy_since)

    def test_supervised_down_service_starts_on_first_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = {
                "id": "web",
                "kind": "http",
                "supervise": True,
                "healthcheck": {"type": "http", "url": "http://127.0.0.1:3001"},
            }
            model = {
                "clients": [{"id": "personal"}],
                "selection": {},
                "services": [service],
                "checks": [],
            }
            state = PULSE_MODULE.PulseState()

            with (
                mock.patch.object(PULSE_MODULE, "build_runtime_model", return_value=model),
                mock.patch.object(PULSE_MODULE, "filter_model", side_effect=lambda m, _p, _c: m),
                mock.patch.object(PULSE_MODULE, "_model_config_hash", return_value="hash"),
                mock.patch.object(
                    PULSE_MODULE,
                    "_snapshot_services",
                    return_value={"web": {"state": "down", "pid": None, "url": "http://127.0.0.1:3001"}},
                ),
                mock.patch.object(PULSE_MODULE, "_snapshot_checks", return_value={}),
                mock.patch.object(PULSE_MODULE, "service_supports_lifecycle", return_value=(True, "")),
                mock.patch.object(PULSE_MODULE, "_restart_service", return_value=True) as restart_service,
                mock.patch.object(PULSE_MODULE, "log_runtime_event"),
                mock.patch.object(PULSE_MODULE.time, "monotonic", return_value=10.0),
            ):
                PULSE_MODULE.reconcile_once(
                    root,
                    state,
                    active_clients=["personal"],
                    active_profiles=["local-core"],
                )

            restart_service.assert_called_once()
            self.assertEqual(state.service_states["web"], "running")
            self.assertEqual(state.heals, 1)

    def test_restart_service_uses_service_start_wait_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "logs"
            pid_file = log_dir / "web.pid"
            log_file = log_dir / "web.log"
            service = {
                "id": "web",
                "command": "python3 -m http.server 3001",
                "start_wait_seconds": 90,
            }
            model = {"root_dir": str(root)}
            process = mock.Mock()
            process.pid = 123

            with (
                mock.patch.object(PULSE_MODULE, "service_paths", return_value={
                    "log_dir": log_dir,
                    "pid_file": pid_file,
                    "log_file": log_file,
                }),
                mock.patch.object(PULSE_MODULE, "translated_runtime_command", return_value=("echo ok", {})),
                mock.patch.object(PULSE_MODULE, "resolve_runtime_command_cwd", return_value=root),
                mock.patch.object(PULSE_MODULE.subprocess, "Popen", return_value=process),
                mock.patch.object(PULSE_MODULE, "wait_for_service_health", return_value={"state": "ok"}) as wait_health,
                mock.patch.object(PULSE_MODULE, "log_runtime_event"),
            ):
                ok = PULSE_MODULE._restart_service(model, service, reason="test")

            self.assertTrue(ok)
            wait_health.assert_called_once_with(service, process, 90.0)

    def test_reconcile_pulse_checks_logs_failures_and_recoveries(self) -> None:
        state = PULSE_MODULE.PulseState()
        model: dict[str, object] = {}

        with mock.patch.object(PULSE_MODULE, "_snapshot_checks", return_value={"db": True}):
            PULSE_MODULE._reconcile_pulse_checks(model, state)  # noqa: SLF001
        self.assertEqual(state.check_states, {"db": True})
        self.assertEqual(state.events_emitted, 0)

        with (
            mock.patch.object(PULSE_MODULE, "_snapshot_checks", return_value={"db": False, "cache": True}),
            mock.patch.object(PULSE_MODULE, "log_runtime_event") as event,
            mock.patch.object(PULSE_MODULE, "log") as log,
        ):
            PULSE_MODULE._reconcile_pulse_checks(model, state)  # noqa: SLF001

        event.assert_called_once_with("pulse.check_failed", "db", {"ok": False})
        log.assert_called_once_with("warn", "check db: failed")
        self.assertEqual(state.check_states, {"db": False, "cache": True})
        self.assertEqual(state.events_emitted, 1)

        with (
            mock.patch.object(PULSE_MODULE, "_snapshot_checks", return_value={"db": True, "cache": True}),
            mock.patch.object(PULSE_MODULE, "log_runtime_event") as event,
            mock.patch.object(PULSE_MODULE, "log") as log,
        ):
            PULSE_MODULE._reconcile_pulse_checks(model, state)  # noqa: SLF001

        event.assert_called_once_with("pulse.check_recovered", "db", {"ok": True})
        log.assert_called_once_with("info", "check db: recovered")
        self.assertEqual(state.events_emitted, 2)

    def test_print_pulse_services_and_checks_cover_marker_branches(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            PULSE_MODULE._print_pulse_services(  # noqa: SLF001
                {"api": "running", "web": "down", "worker": "starting"}
            )
            PULSE_MODULE._print_pulse_checks({"db": False, "cache": True})  # noqa: SLF001
            PULSE_MODULE._print_pulse_checks({"db": True, "cache": True})  # noqa: SLF001
            PULSE_MODULE._print_pulse_services({})  # noqa: SLF001
            PULSE_MODULE._print_pulse_checks({})  # noqa: SLF001

        output = stdout.getvalue()
        self.assertIn("+ api: running", output)
        self.assertIn("- web: down", output)
        self.assertIn("~ worker: starting", output)
        self.assertIn("failed checks: db", output)
        self.assertIn("checks: all passing (2)", output)


if __name__ == "__main__":
    unittest.main()
