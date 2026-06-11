from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import agent_adapters as ADAPT  # noqa: E402


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["tool"], returncode, stdout=stdout, stderr=stderr)


class CommandAdapterTests(unittest.TestCase):
    def test_successful_json_command_is_parsed_with_timeout_and_cwd(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append({"command": command, **kwargs})
            return _completed('{"items": [1]}')

        with tempfile.TemporaryDirectory() as tmpdir:
            result = ADAPT.run_command_adapter(
                "br",
                ["br", "ready", "--json"],
                cwd=Path(tmpdir),
                subprocess_run=fake_run,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["payload"], {"items": [1]})
        self.assertEqual(calls[0]["timeout"], ADAPT.DEFAULT_TIMEOUTS["br"])
        self.assertEqual(calls[0]["command"], ["br", "ready", "--json"])

    def test_missing_command_degrades_without_raising(self) -> None:
        def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("missing br")

        result = ADAPT.run_command_adapter("br", ["br", "ready", "--json"], subprocess_run=fake_run)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["warnings"][0]["code"], "UNAVAILABLE_DEPENDENCY")

    def test_nonzero_exit_redacts_stderr(self) -> None:
        stderr = "Authorization: Bearer token-secret password=secret123 api_key=api-secret"

        def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return _completed('{"error": "bad"}', returncode=2, stderr=stderr)

        result = ADAPT.run_command_adapter("bv", ["bv", "--robot-triage"], subprocess_run=fake_run)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["warnings"][0]["code"], "ADAPTER_NONZERO_EXIT")
        self.assertNotIn("token-secret", result["stderr"])
        self.assertNotIn("secret123", result["stderr"])
        self.assertNotIn("api-secret", result["stderr"])
        self.assertIn(ADAPT.REDACTION_MARKER, result["stderr"])

    def test_malformed_json_and_toon_are_structured_warnings(self) -> None:
        def bad_json(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return _completed("{not json")

        json_result = ADAPT.run_command_adapter("bv", ["bv"], subprocess_run=bad_json)
        self.assertFalse(json_result["ok"])
        self.assertEqual(json_result["warnings"][0]["code"], "MALFORMED_JSON")
        self.assertIn("{not json", json_result["stdout_preview"])

        def toon(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return _completed("key: value\n")

        toon_result = ADAPT.run_command_adapter(
            "bv",
            ["bv", "--robot-triage", "--format", "toon"],
            expected_format="toon",
            subprocess_run=toon,
        )
        self.assertFalse(toon_result["ok"])
        self.assertEqual(toon_result["warnings"][0]["code"], "MALFORMED_TOON")

    def test_timeout_is_structured_and_redacted(self) -> None:
        def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(
                ["sbp"],
                timeout=2.5,
                output="partial",
                stderr="TOKEN=secret",
            )

        result = ADAPT.run_command_adapter("sbp", ["sbp", "skills"], subprocess_run=fake_run)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["warnings"][0]["code"], "ADAPTER_TIMEOUT")
        self.assertEqual(result["stdout_preview"], "partial")
        self.assertNotIn("secret", result["stderr"])

    def test_wrappers_use_expected_commands(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return _completed("{}")

        root = Path("/repo")
        ADAPT.br_ready_adapter(root, timeout_seconds=0.1, subprocess_run=fake_run)
        ADAPT.br_list_adapter(root, status="closed", timeout_seconds=0.1, subprocess_run=fake_run)
        ADAPT.br_show_adapter(root, "bd-123", timeout_seconds=0.1, subprocess_run=fake_run)
        ADAPT.bv_triage_adapter(root, timeout_seconds=0.1, subprocess_run=fake_run)
        ADAPT.sbp_skills_adapter(root, timeout_seconds=0.1, subprocess_run=fake_run)
        ADAPT.ntm_activity_adapter("sess", root_dir=root, timeout_seconds=0.1, subprocess_run=fake_run)

        self.assertEqual(
            commands,
            [
                ["br", "ready", "--json"],
                ["br", "list", "--status=closed", "--json"],
                ["br", "show", "bd-123", "--json"],
                ["bv", "--robot-triage", "--format", "json"],
                ["sbp", "skills", "--issues-only", "--format", "json"],
                ["ntm", "activity", "sess", "--json"],
            ],
        )


class FileAndInProcessAdapterTests(unittest.TestCase):
    def test_pulse_state_missing_unreadable_stale_and_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing = ADAPT.pulse_state_adapter(root, now=1000)
            self.assertEqual(missing["status"], "unavailable")
            self.assertEqual(missing["warnings"][0]["code"], "PULSE_STATE_MISSING")

            state_dir = root / ".skillbox-state" / "logs" / "runtime"
            state_dir.mkdir(parents=True)
            state_path = state_dir / "pulse.state.json"
            state_path.write_text("{bad", encoding="utf-8")
            unreadable = ADAPT.pulse_state_adapter(root, now=1000)
            self.assertEqual(unreadable["status"], "degraded")
            self.assertEqual(unreadable["warnings"][0]["code"], "PULSE_STATE_UNREADABLE")

            state_path.write_text(json.dumps({"updated_at": 800, "cycle_count": 3}), encoding="utf-8")
            stale = ADAPT.pulse_state_adapter(root, now=1000, max_age_seconds=120)
            self.assertEqual(stale["status"], "degraded")
            self.assertEqual(stale["warnings"][0]["code"], "STALE_PULSE_STATE")
            self.assertEqual(stale["age_seconds"], 200)

            state_path.write_text(json.dumps({"updated_at": 990, "cycle_count": 4}), encoding="utf-8")
            fresh = ADAPT.pulse_state_adapter(root, now=1000, max_age_seconds=120)
            self.assertTrue(fresh["ok"])
            self.assertEqual(fresh["status"], "ok")
            self.assertEqual(fresh["payload"]["cycle_count"], 4)

    def test_runtime_evidence_adapter_reports_success_and_failure(self) -> None:
        with mock.patch.object(ADAPT, "collect_runtime_evidence", return_value={"overall": "green"}):
            ok = ADAPT.runtime_evidence_adapter(Path("/repo"), {"services": []})
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["payload"], {"overall": "green"})

        with mock.patch.object(ADAPT, "collect_runtime_evidence", side_effect=RuntimeError("bad model")):
            degraded = ADAPT.runtime_evidence_adapter(Path("/repo"), {"services": []})
        self.assertFalse(degraded["ok"])
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(degraded["warnings"][0]["code"], "EVIDENCE_COLLECTION_FAILED")


if __name__ == "__main__":
    unittest.main()
