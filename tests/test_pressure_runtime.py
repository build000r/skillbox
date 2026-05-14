from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.runtime_ops import (  # noqa: E402
    _runtime_blocked_services,
    probe_service,
    service_supports_lifecycle,
)
from runtime_manager.shared import build_runtime_model  # noqa: E402
from runtime_manager.validation import filter_model, normalize_active_clients, normalize_active_profiles  # noqa: E402


class PressureRuntimeTests(unittest.TestCase):
    def test_pressure_tools_profile_declares_optional_rch_and_sbh_surfaces(self) -> None:
        raw_model = build_runtime_model(ROOT_DIR)
        pressure_model = filter_model(
            raw_model,
            normalize_active_profiles(["pressure-tools"]),
            normalize_active_clients(raw_model, []),
        )
        core_model = filter_model(
            raw_model,
            normalize_active_profiles([]),
            normalize_active_clients(raw_model, []),
        )

        pressure_artifact_ids = {artifact["id"] for artifact in pressure_model["artifacts"]}
        pressure_service_ids = {service["id"] for service in pressure_model["services"]}
        pressure_check_ids = {check["id"] for check in pressure_model["checks"]}
        core_artifact_ids = {artifact["id"] for artifact in core_model["artifacts"]}
        core_service_ids = {service["id"] for service in core_model["services"]}

        for artifact_id in {"rch-bin", "rchd-bin", "rch-worker-bin", "rch-workers-config", "sbh-bin", "sbh-config"}:
            self.assertIn(artifact_id, pressure_artifact_ids)
            artifact = next(item for item in pressure_model["artifacts"] if item["id"] == artifact_id)
            self.assertFalse(artifact["required"])

        self.assertIn("rchd", pressure_service_ids)
        self.assertIn("sbh-daemon", pressure_service_ids)
        self.assertIn("rch-binary", pressure_check_ids)
        self.assertIn("sbh-binary", pressure_check_ids)
        self.assertNotIn("rch-bin", core_artifact_ids)
        self.assertNotIn("sbh-bin", core_artifact_ids)
        self.assertNotIn("rchd", core_service_ids)
        self.assertNotIn("sbh-daemon", core_service_ids)

    def test_missing_optional_service_artifact_reports_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_root = root / "logs"
            log_root.mkdir()
            model = {
                "root_dir": str(root),
                "logs": [{"id": "runtime", "host_path": str(log_root)}],
                "artifacts": [
                    {
                        "id": "rch-bin",
                        "path": "/home/sandbox/.local/bin/rch",
                        "host_path": str(root / "missing-rch"),
                        "required": False,
                    }
                ],
            }
            service = {
                "id": "rchd",
                "kind": "daemon",
                "artifact": "rch-bin",
                "required": False,
                "command": "/home/sandbox/.local/bin/rch daemon start",
                "healthcheck": {"type": "process_running", "pattern": "rchd"},
                "log": "runtime",
            }

            with mock.patch(
                "runtime_manager.runtime_ops.service_healthcheck_state",
                return_value={"state": "down", "pattern": "rchd"},
            ):
                manageable, reason = service_supports_lifecycle(service, model)
                status = {"id": service["id"]} | probe_service(model, service)

        self.assertFalse(manageable)
        self.assertEqual(reason, "optional artifact 'rch-bin' not configured")
        self.assertEqual(status["state"], "not-configured")
        self.assertEqual(status["manager_reason"], "optional artifact 'rch-bin' not configured")
        self.assertEqual(_runtime_blocked_services([status]), [])


if __name__ == "__main__":
    unittest.main()
