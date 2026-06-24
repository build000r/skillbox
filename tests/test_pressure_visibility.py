from __future__ import annotations

import io
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

from runtime_manager import context_rendering as CONTEXT  # noqa: E402
from runtime_manager import runtime_ops as OPS  # noqa: E402
from runtime_manager import shared as SHARED  # noqa: E402
from runtime_manager import workflows as WORKFLOWS  # noqa: E402
from runtime_manager.text_renderers import print_status_text  # noqa: E402


PULSE_MODULE = SourceFileLoader(
    "skillbox_pulse_visibility",
    str((ENV_MANAGER_DIR / "pulse.py").resolve()),
).load_module()


def _advisory_fixture() -> dict[str, object]:
    return {
        "ok": True,
        "mode": "read_only",
        "mutates": False,
        "local_disk": {
            "free_gib": 8.0,
            "total_gib": 100.0,
            "free_percent": 8.0,
            "pressure_level": "high",
        },
        "target_worker": {
            "id": "worker-devbox",
            "state": "ready",
            "tailscale_hostname": "skillbox-worker-devbox",
            "excluded_box_ids": ["client_a", "ssh-gateway", "example-prod"],
        },
        "rch": {
            "state": "not-configured",
            "worker_state": "no-worker",
            "fail_open_expected": True,
            "hook_install_allowed": False,
        },
        "sbh": {
            "state": "not-configured",
            "daemon_state": "missing-daemon",
            "auto_delete_allowed": False,
            "ballast_mutation_allowed": False,
        },
        "protected_paths": [
            {"id": "codex-state", "path": "~/.codex", "policy": "protected_no_touch"},
            {"id": "ssh-material", "path": "~/.ssh", "policy": "protected_no_touch"},
        ],
        "warnings": ["Local disk pressure is high; inspect pressure-report first."],
        "safe_first_commands": [
            "python3 .env-manager/manage.py pressure-report --format json",
            "python3 .env-manager/manage.py rch-report --format json",
            "python3 .env-manager/manage.py sbh-report --format json",
        ],
    }


class PressureVisibilityTests(unittest.TestCase):
    def test_generated_context_includes_pressure_offload_policy(self) -> None:
        model = {
            "root_dir": str(ROOT_DIR),
            "active_clients": [],
            "active_profiles": ["core"],
            "clients": [],
            "env": {},
            "repos": [],
            "services": [],
            "tasks": [],
            "skills": [],
            "logs": [],
        }

        with mock.patch.object(CONTEXT, "runtime_pressure_advisory", return_value=_advisory_fixture()):
            content = CONTEXT.generate_context_markdown(model)

        self.assertIn("Pressure And Offload Policy", content)
        self.assertIn("worker-devbox", content)
        self.assertIn("hook-install-allowed=False", content)
        self.assertIn("auto-delete=False", content)
        self.assertIn("sbh-report --format json", content)
        self.assertIn("Do not run cleanup", content)

    def test_status_compact_text_and_next_actions_surface_pressure(self) -> None:
        payload = {
            "clients": [],
            "active_clients": [],
            "default_client": None,
            "active_profiles": [],
            "box_access": {},
            "distributors": [],
            "storage": {},
            "repos": [],
            "artifacts": [],
            "env_files": [],
            "skills": [],
            "tasks": [],
            "services": [],
            "blocked_services": [],
            "logs": [],
            "checks": [],
            "ingress": {},
            "parity_ledger": {},
            "pressure_advisory": _advisory_fixture(),
        }

        actions = SHARED.next_actions_for_status(payload)
        payload["next_actions"] = actions
        compact = OPS.compact_runtime_status(payload)

        self.assertIn("pressure-report --format json", actions)
        self.assertEqual(compact["pressure_advisory"]["target_worker"]["id"], "worker-devbox")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            print_status_text(payload)
        rendered = stdout.getvalue()
        self.assertIn("pressure/offload:", rendered)
        self.assertIn("rch: not-configured", rendered)
        self.assertIn("sbh: not-configured", rendered)

    def test_focus_and_stewardship_summaries_include_pressure(self) -> None:
        live = {"pressure_advisory": {"warnings": ["pressure"]}, "repos": [], "services": [], "checks": [], "logs": []}
        summary = WORKFLOWS._focus_summary_counts(live)  # noqa: SLF001
        self.assertEqual(summary["pressure_warnings"], 1)

        report = WORKFLOWS.render_stewardship_report_markdown(
            {
                "client_id": "skillbox",
                "generated_at": "2026-05-14T00:00:00Z",
                "active_profiles": ["core"],
                "next_recommendation": "inspect",
                "focus": {"status": "present", "path": "workspace/.focus.json"},
                "health": {"checks": {}, "services": {}, "recent_errors": {}},
                "evidence": {
                    "doctor": {"status": "pass", "counts": {"fail": 0}},
                    "sessions": {"count": 0},
                    "parity_ledger": {"deferred_count": 0},
                    "dev_prod_parity": {"status": "not_assessed", "blocking_count": 0},
                    "pressure_advisory": _advisory_fixture(),
                },
                "risks": [],
                "not_assessed": [],
                "next_actions": [],
            }
        )

        self.assertIn("Pressure: high", report)
        self.assertIn("target=worker-devbox", report)
        self.assertIn("rch=not-configured", report)
        self.assertIn("sbh=not-configured", report)

    def test_pulse_records_pressure_warning_without_mutation(self) -> None:
        state = PULSE_MODULE.PulseState()
        advisory = _advisory_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                mock.patch.object(PULSE_MODULE, "runtime_pressure_advisory", return_value=advisory),
                mock.patch.object(PULSE_MODULE, "log_runtime_event") as event,
                mock.patch.object(PULSE_MODULE, "log"),
            ):
                PULSE_MODULE._reconcile_pressure_advisory(root, state)  # noqa: SLF001

        self.assertEqual(state.pressure_warnings, advisory["warnings"])
        self.assertFalse(state.pressure_advisory["mutates"])
        self.assertEqual(event.call_args.args[0], "pulse.pressure_advisory")
        self.assertFalse(event.call_args.args[2]["mutates"])
        self.assertIn("pressure_warnings", state.to_dict())


if __name__ == "__main__":
    unittest.main()
