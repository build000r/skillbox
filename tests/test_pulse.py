from __future__ import annotations

import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
