from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
SCRIPTS_DIR = ROOT_DIR / "scripts"
for path in (ENV_MANAGER_DIR, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

PULSE = SourceFileLoader("skillbox_pulse_regression", str(ENV_MANAGER_DIR / "pulse.py")).load_module()

from runtime_manager import runtime_ops as OPS  # noqa: E402


class PortGuardRegressionTests(unittest.TestCase):
    """Regression coverage for the five-layer port-guard success criteria."""

    def _guard_model(self, root: Path, app: Path) -> dict[str, object]:
        return {
            "root_dir": str(root),
            "env": {"SKILLBOX_NETWORK_POSTURE": "tailnet_only"},
            "repos": [{"id": "app", "host_path": str(app)}],
            "services": [
                {
                    "id": "app-web",
                    "client": "personal",
                    "profiles": ["local-all"],
                    "repo": "app",
                    "command": "npm run dev -- --host 127.0.0.1 --port 5173",
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:5173/health"},
                }
            ],
        }

    def test_CRITERION_1_direct_dev_command_blocks_with_exact_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "app"
            scripts = root / "scripts"
            app.mkdir()
            scripts.mkdir()
            guard = scripts / "guard-dev-port.sh"
            guard.write_text((ROOT_DIR / "scripts" / "guard-dev-port.sh").read_text(encoding="utf-8"), encoding="utf-8")
            guard.chmod(0o755)
            env = os.environ.copy()
            env["SKILLBOX_ROOT"] = str(root)
            env["SKILLBOX_PORT_GUARD_MODEL_JSON"] = json.dumps(self._guard_model(root, app))
            payload = {"tool_name": "Bash", "tool_input": {"command": "npm run dev", "cwd": str(app)}}

            result = subprocess.run(
                ["bash", str(guard)],
                cwd=root,
                input=json.dumps(payload),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED: direct dev server command in managed repo", result.stderr)
        self.assertIn("declared port: 5173", result.stderr)
        self.assertIn("python3 .env-manager/manage.py up --client personal --profile local-all --service app-web", result.stderr)

    def test_CRITERION_2_full_bypass_dev_signature_is_reaped_after_grace(self) -> None:
        state = PULSE.PulseState()
        candidate = {
            "key": "333:5174:old",
            "pid": 333,
            "port": 5174,
            "comm": "node",
            "cmdline": "node vite --host 0.0.0.0 --port 5174",
            "start_time": "old",
            "signature": "vite",
            "enforcement": "dev-server",
            "reason": "dev_server_signature",
            "declared_port": False,
        }
        state.port_sentinel_first_seen[candidate["key"]] = 1.0
        state.port_sentinel_counters["rogues_seen"] = 1
        state.port_sentinel_counters["wildcard_criticals"] = 1

        with (
            mock.patch.object(PULSE, "_port_sentinel_config", return_value=("enforce", 5.0)),  # noqa: SLF001
            mock.patch.object(PULSE, "_scan_rogue_listeners", return_value=[candidate]),  # noqa: SLF001
            mock.patch.object(PULSE, "_terminate_rogue", return_value="terminated") as terminate,  # noqa: SLF001
            mock.patch.object(PULSE, "log_runtime_event"),
            mock.patch.object(PULSE, "log"),
        ):
            PULSE._reconcile_port_sentinel({}, state, now=10.0)  # noqa: SLF001

        terminate.assert_called_once_with(candidate)
        self.assertEqual(state.port_sentinel_counters["rogues_reaped"], 1)
        self.assertEqual(state.port_sentinel_counters["wildcard_criticals"], 1)

    def test_CRITERION_2_mutation_sanity_observe_mode_never_reaps(self) -> None:
        state = PULSE.PulseState()
        candidate = {
            "key": "333:5174:old",
            "pid": 333,
            "port": 5174,
            "comm": "node",
            "cmdline": "node vite --host 0.0.0.0 --port 5174",
            "start_time": "old",
            "signature": "vite",
            "enforcement": "dev-server",
            "reason": "dev_server_signature",
            "declared_port": False,
        }

        with (
            mock.patch.object(PULSE, "_port_sentinel_config", return_value=("observe", 0.0)),  # noqa: SLF001
            mock.patch.object(PULSE, "_scan_rogue_listeners", return_value=[candidate]),  # noqa: SLF001
            mock.patch.object(PULSE, "_terminate_rogue") as terminate,  # noqa: SLF001
            mock.patch.object(PULSE, "log_runtime_event"),
            mock.patch.object(PULSE, "log"),
        ):
            PULSE._reconcile_port_sentinel({}, state, now=10.0)  # noqa: SLF001

        terminate.assert_not_called()
        self.assertEqual(state.port_sentinel_counters["rogues_reaped"], 0)

    def test_CRITERION_3_post_bind_verification_catches_silent_port_hop(self) -> None:
        service = {
            "id": "web",
            "kind": "http",
            "healthcheck": {"type": "http", "url": "http://127.0.0.1:5173/health"},
        }
        with mock.patch.object(
            OPS,
            "_process_tree_listener_snapshot",  # noqa: SLF001
            return_value={
                "pid": 123,
                "pids": [123],
                "listeners": [{"pid": 123, "port": 5174}],
                "observed_ports": [5174],
                "source": "test",
            },
        ):
            verification = OPS._verify_service_declared_ports({"services": [service]}, service, 123, attempts=1)  # noqa: SLF001

        self.assertEqual(verification["state"], "mismatch")
        self.assertEqual(verification["declared_ports"], [5173])
        self.assertEqual(verification["observed_ports"], [5174])

    def test_CRITERION_4_counters_survive_pulse_restart_hydration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = PULSE.pulse_state_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "port_sentinel": {
                            "rogues_seen": 5,
                            "rogues_reaped": 2,
                            "wildcard_criticals": 1,
                            "first_seen_at": "2026-06-01T00:00:00Z",
                            "last_seen_at": "2026-06-02T00:00:00Z",
                        }
                    }
                ),
                encoding="utf-8",
            )

            state = PULSE.load_pulse_state(root)

        self.assertEqual(state.port_sentinel_counters["rogues_seen"], 5)
        self.assertEqual(state.port_sentinel_counters["rogues_reaped"], 2)
        self.assertEqual(state.port_sentinel_counters["wildcard_criticals"], 1)
        self.assertEqual(state.port_sentinel_counters["last_seen_at"], "2026-06-02T00:00:00Z")

    def test_CRITERION_4_mutation_sanity_missing_state_resets_counter_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = PULSE.load_pulse_state(Path(tmpdir))

        self.assertEqual(state.port_sentinel_counters["rogues_seen"], 0)
        self.assertNotIn("first_seen_at", state.port_sentinel_counters)

    def test_CRITERION_5_tailnet_only_rejects_wildcard_dev_listener_contract(self) -> None:
        model = {
            "env": {"SKILLBOX_NETWORK_POSTURE": "tailnet_only"},
            "services": [
                {
                    "id": "web",
                    "kind": "http",
                    "command": "npm run dev -- --host 0.0.0.0 --port 5173",
                    "healthcheck": {"type": "http", "url": "http://0.0.0.0:5173/health"},
                }
            ],
            "ingress_routes": [],
        }

        results = OPS.validate_port_registry(model)

        wildcard = [result for result in results if result.code == OPS.PORT_WILDCARD_BIND]
        self.assertEqual(len(wildcard), 1)
        self.assertEqual(wildcard[0].status, "fail")


if __name__ == "__main__":
    unittest.main()
