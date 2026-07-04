from __future__ import annotations

import json
import sys
import unittest
from contextlib import ExitStack
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from tests.helpers import make_runtime_model, make_temp_workspace


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

PULSE = SourceFileLoader(
    "skillbox_pulse_chaos_tests",
    str((ENV_MANAGER_DIR / "pulse.py").resolve()),
).load_module()


def _service(service_id: str = "api", **overrides: object) -> dict[str, object]:
    service = {
        "id": service_id,
        "kind": "http",
        "command": "python3 -m http.server 9999",
        "repo": "app",
        "required": True,
        "supervise": True,
        "healthcheck": {"type": "http", "url": "http://127.0.0.1:9999"},
    }
    service.update(overrides)
    return service


def _model(*services: dict[str, object], checks: list[dict[str, object]] | None = None) -> dict[str, object]:
    return make_runtime_model(services=list(services), checks=checks or [])


def _event_names(event_mock: mock.Mock) -> list[str]:
    return [str(call.args[0]) for call in event_mock.call_args_list]


def _assert_event_shape(testcase: unittest.TestCase, event_mock: mock.Mock) -> None:
    for call in event_mock.call_args_list:
        testcase.assertGreaterEqual(len(call.args), 3)
        testcase.assertIsInstance(call.args[0], str)
        testcase.assertIsInstance(call.args[1], str)
        testcase.assertIsInstance(call.args[2], dict)


def _patch_common(
    *,
    model: dict[str, object],
    service_snapshots: list[dict[str, dict[str, object]]],
    monotonic_values: list[float] | None = None,
    restart_result: bool | list[bool] = False,
) -> tuple[mock.Mock, mock.Mock, tuple[object, ...]]:
    event_mock = mock.Mock()
    if isinstance(restart_result, list):
        restart_mock = mock.Mock(side_effect=restart_result)
    else:
        restart_mock = mock.Mock(return_value=restart_result)
    patches = (
        mock.patch.object(PULSE, "_load_pulse_model", return_value=(model, {"core"}, {"personal"})),
        mock.patch.object(PULSE, "_model_config_hash", return_value="hash-1"),
        mock.patch.object(PULSE, "_snapshot_services", side_effect=service_snapshots),
        mock.patch.object(PULSE, "_snapshot_checks", return_value={}),
        mock.patch.object(PULSE, "_scan_rogue_listeners", return_value=[]),
        mock.patch.object(PULSE, "runtime_pressure_advisory", return_value={"warnings": []}),
        mock.patch.object(PULSE, "service_supports_lifecycle", return_value=(True, "")),
        mock.patch.object(PULSE, "_restart_service", restart_mock),
        mock.patch.object(PULSE, "log_runtime_event", event_mock),
        mock.patch.object(PULSE, "log"),
        mock.patch.object(PULSE, "_write_pulse_state"),
        mock.patch.object(PULSE.time, "monotonic", side_effect=monotonic_values or list(range(len(service_snapshots)))),
    )
    return event_mock, restart_mock, patches


class PulseChaosTests(unittest.TestCase):
    def setUp(self) -> None:
        PULSE._shutdown = False
        PULSE._runtime_dir_cache.clear()

    def tearDown(self) -> None:
        PULSE._shutdown = False
        PULSE._runtime_dir_cache.clear()

    def test_restart_cap_stops_after_max_attempts_and_declares_terminal_state(self) -> None:
        cycle_count = PULSE.MAX_RESTART_ATTEMPTS + 3
        model = _model(_service("api"))
        snapshots = [{"api": {"state": "declared"}} for _ in range(cycle_count)]
        monotonic_values = [index * (PULSE.RESTART_BACKOFF_SECONDS + 1) for index in range(cycle_count)]

        with make_temp_workspace({}) as root:
            state = PULSE.PulseState()
            event_mock, restart_mock, patches = _patch_common(
                model=model,
                service_snapshots=snapshots,
                monotonic_values=monotonic_values,
                restart_result=False,
            )
            with ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                for _ in snapshots:
                    PULSE.reconcile_once(root, state)

            self.assertEqual(restart_mock.call_count, PULSE.MAX_RESTART_ATTEMPTS)
            self.assertEqual(state.restart_attempts["api"], PULSE.MAX_RESTART_ATTEMPTS)
            self.assertEqual(state.service_states["api"], "declared")
            names = _event_names(event_mock)
            self.assertIn("pulse.service_down", names)
            self.assertIn("pulse.restart_suppressed", names)
            suppressed = [call.args[2] for call in event_mock.call_args_list if call.args[0] == "pulse.restart_suppressed"]
            self.assertTrue(suppressed)
            self.assertEqual(suppressed[-1]["max_attempts"], PULSE.MAX_RESTART_ATTEMPTS)
            _assert_event_shape(self, event_mock)

    def test_recovery_after_cap_current_semantics_reset_and_rearm_counter(self) -> None:
        model = _model(_service("api"))
        failed = [{"api": {"state": "declared"}} for _ in range(PULSE.MAX_RESTART_ATTEMPTS + 1)]
        snapshots = failed + [{"api": {"state": "running", "pid": 10}}, {"api": {"state": "declared"}}]
        monotonic_values = [index * (PULSE.RESTART_BACKOFF_SECONDS + 1) for index in range(len(snapshots))]

        with make_temp_workspace({}) as root:
            state = PULSE.PulseState()
            event_mock, restart_mock, patches = _patch_common(
                model=model,
                service_snapshots=snapshots,
                monotonic_values=monotonic_values,
                restart_result=False,
            )
            with ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                for _ in snapshots:
                    PULSE.reconcile_once(root, state)

            self.assertEqual(restart_mock.call_count, PULSE.MAX_RESTART_ATTEMPTS + 1)
            self.assertEqual(state.restart_attempts["api"], 1)
            self.assertEqual(state.service_states["api"], "declared")
            names = _event_names(event_mock)
            self.assertIn("pulse.service_state_changed", names)
            self.assertIn("pulse.service_crashed", names)
            _assert_event_shape(self, event_mock)

    def test_corrupt_state_file_starts_clean_with_warning_and_valid_state_snapshot(self) -> None:
        model = _model(_service("api"))
        with make_temp_workspace({}) as root:
            state_path = PULSE.pulse_state_path(root)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text("{truncated", encoding="utf-8")

            def reconcile_once(root_dir: Path, state: object, **kwargs: object) -> None:
                PULSE._write_pulse_state(
                    root_dir,
                    state,
                    now=0.0,
                    auto_restart=True,
                    auto_sync=False,
                    active_clients={"personal"},
                    active_profiles={"core"},
                    unhealthy_grace_seconds=PULSE.DEFAULT_UNHEALTHY_GRACE_SECONDS,
                )
                PULSE._shutdown = True

            with (
                mock.patch.object(PULSE, "existing_pid", return_value=None),
                mock.patch.object(PULSE, "_open_log"),
                mock.patch.object(PULSE, "write_pid"),
                mock.patch.object(PULSE.signal, "signal"),
                mock.patch.object(PULSE, "_load_pulse_model", return_value=(model, {"core"}, {"personal"})),
                mock.patch.object(PULSE, "reconcile_once", side_effect=reconcile_once),
                mock.patch.object(PULSE, "remove_pid"),
                mock.patch.object(PULSE, "log_runtime_event") as event_mock,
                mock.patch.object(PULSE, "log") as log_mock,
                mock.patch.object(PULSE.time, "sleep") as sleep_mock,
            ):
                self.assertEqual(PULSE.run_daemon(root, interval=1), 0)

            snapshot = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["cycle_count"], 0)
            self.assertTrue(any(call.args[:2] == ("warn", "failed to read pulse state; starting clean") for call in log_mock.call_args_list))
            self.assertEqual(_event_names(event_mock), ["pulse.started", "pulse.stopped"])
            _assert_event_shape(self, event_mock)
            sleep_mock.assert_not_called()

    def test_sigterm_mid_cycle_shuts_down_cleanly_and_leaves_valid_state(self) -> None:
        with make_temp_workspace({}) as root:
            state_path = PULSE.pulse_state_path(root)

            def reconcile_once(root_dir: Path, state: object, **kwargs: object) -> None:
                state.cycle_count += 1
                PULSE._handle_signal(PULSE.signal.SIGTERM, None)
                PULSE._write_pulse_state(
                    root_dir,
                    state,
                    now=3.0,
                    auto_restart=True,
                    auto_sync=False,
                    active_clients={"personal"},
                    active_profiles={"core"},
                    unhealthy_grace_seconds=PULSE.DEFAULT_UNHEALTHY_GRACE_SECONDS,
                )

            with (
                mock.patch.object(PULSE, "existing_pid", return_value=None),
                mock.patch.object(PULSE, "_open_log"),
                mock.patch.object(PULSE, "write_pid"),
                mock.patch.object(PULSE.signal, "signal"),
                mock.patch.object(PULSE, "reconcile_once", side_effect=reconcile_once),
                mock.patch.object(PULSE, "remove_pid"),
                mock.patch.object(PULSE, "log_runtime_event") as event_mock,
                mock.patch.object(PULSE, "log"),
                mock.patch.object(PULSE.time, "sleep") as sleep_mock,
            ):
                self.assertEqual(PULSE.run_daemon(root, interval=1), 0)

            snapshot = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["cycle_count"], 1)
            self.assertEqual(_event_names(event_mock), ["pulse.started", "pulse.stopped"])
            self.assertEqual(event_mock.call_args_list[-1].args[2]["cycles"], 1)
            _assert_event_shape(self, event_mock)
            sleep_mock.assert_not_called()

    def test_config_hash_change_reloads_model_and_prunes_stale_services(self) -> None:
        old_model = _model(_service("old-api"))
        new_model = _model(_service("new-api"))
        with make_temp_workspace({"workspace": {"runtime.yaml": "version: 1\n"}}) as root:
            state = PULSE.PulseState()
            with (
                mock.patch.object(
                    PULSE,
                    "_load_pulse_model",
                    side_effect=[(old_model, {"core"}, {"personal"}), (new_model, {"core"}, {"personal"})],
                ) as load_model,
                mock.patch.object(PULSE, "_model_config_hash", side_effect=["hash-old", "hash-new"]),
                mock.patch.object(
                    PULSE,
                    "_snapshot_services",
                    side_effect=[
                        {"old-api": {"state": "running", "pid": 1}},
                        {"new-api": {"state": "running", "pid": 2}},
                    ],
                ),
                mock.patch.object(PULSE, "_snapshot_checks", return_value={}),
                mock.patch.object(PULSE, "_scan_rogue_listeners", return_value=[]),
                mock.patch.object(PULSE, "runtime_pressure_advisory", return_value={"warnings": []}),
                mock.patch.object(PULSE, "_restart_service") as restart,
                mock.patch.object(PULSE, "_write_pulse_state"),
                mock.patch.object(PULSE, "log_runtime_event") as event_mock,
                mock.patch.object(PULSE, "log"),
                mock.patch.object(PULSE.time, "monotonic", side_effect=[1.0, 2.0]),
            ):
                PULSE.reconcile_once(root, state)
                PULSE.reconcile_once(root, state)

            self.assertEqual(load_model.call_count, 2)
            self.assertEqual(state.service_states, {"new-api": "running"})
            self.assertNotIn("old-api", state.restart_attempts)
            restart.assert_not_called()
            config_events = [call for call in event_mock.call_args_list if call.args[0] == "pulse.config_changed"]
            self.assertEqual(len(config_events), 1)
            self.assertEqual(config_events[0].args[2]["old_hash"], "hash-old")
            self.assertEqual(config_events[0].args[2]["new_hash"], "hash-new")
            _assert_event_shape(self, event_mock)

    def test_flapping_healthcheck_stays_bounded_by_grace_and_does_not_spam_unhealthy_events(self) -> None:
        model = _model(_service("api"))
        snapshots = [
            {"api": {"state": "running", "pid": 1}},
            {"api": {"state": "starting", "pid": 1}},
            {"api": {"state": "running", "pid": 1}},
            {"api": {"state": "starting", "pid": 1}},
            {"api": {"state": "running", "pid": 1}},
            {"api": {"state": "starting", "pid": 1}},
            {"api": {"state": "running", "pid": 1}},
        ]
        with make_temp_workspace({}) as root:
            state = PULSE.PulseState()
            event_mock, restart_mock, patches = _patch_common(
                model=model,
                service_snapshots=snapshots,
                monotonic_values=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                restart_result=True,
            )
            with ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                for _ in snapshots:
                    PULSE.reconcile_once(root, state, unhealthy_grace_seconds=10.0)

            restart_mock.assert_not_called()
            self.assertNotIn("api", state.unhealthy_since)
            names = _event_names(event_mock)
            self.assertNotIn("pulse.service_unhealthy", names)
            self.assertLessEqual(names.count("pulse.service_state_changed"), len(snapshots) - 1)
            _assert_event_shape(self, event_mock)


if __name__ == "__main__":
    unittest.main()
