from __future__ import annotations

import json
import os
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
    def test_tool_wrappers_all_use_adapter_specs(self) -> None:
        seen: list[tuple[str, str, list[str]]] = []

        def fake_run_adapter(
            spec: ADAPT.AdapterSpec,
            *,
            context: object | None = None,
            **_kwargs: object,
        ) -> ADAPT.AdapterResult:
            args = spec.args_builder(context if isinstance(context, dict) else {})
            seen.append((spec.name, spec.binary, args))
            return ADAPT.AdapterResult(
                status="ok",
                payload={},
                raw_excerpt="",
                elapsed_ms=1,
                source_command=[spec.binary, *args],
                timeout_seconds=spec.timeout_default,
                timeout_source="default",
                warnings=[],
                source=spec.name,
            )

        root = Path("/repo")
        with mock.patch.object(ADAPT, "run_adapter", side_effect=fake_run_adapter):
            ADAPT.br_ready_adapter(root)
            ADAPT.bv_triage_adapter(root)
            ADAPT.sbp_skills_adapter(root)
            ADAPT.ntm_activity_adapter("sess", root_dir=root)

        self.assertEqual(
            seen,
            [
                ("br", "br", ["ready", "--json"]),
                ("bv", "bv", ["--robot-triage", "--format", "json"]),
                ("sbp", "sbp", ["skills", "--issues-only", "--format", "json"]),
                ("ntm", "ntm", ["activity", "sess", "--json"]),
            ],
        )

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
        self.assertEqual(result["timeout_seconds"], ADAPT.DEFAULT_TIMEOUTS["br"])
        self.assertEqual(result["source_command"], ["br", "ready", "--json"])
        self.assertIn("elapsed_ms", result)

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
        self.assertEqual(result["status"], "nonzero_exit")
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
        self.assertEqual(json_result["status"], "parse_error")
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
        self.assertEqual(toon_result["status"], "parse_error")
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
        self.assertEqual(result["timeout_seconds"], ADAPT.DEFAULT_TIMEOUTS["sbp"])

    def test_env_tunable_timeout_extends_sleeping_fake_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = Path(tmpdir) / "bv"
            bin_path.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import time\n"
                "time.sleep(0.12)\n"
                "print(json.dumps({'ok': True}))\n",
                encoding="utf-8",
            )
            bin_path.chmod(0o755)
            spec = ADAPT.AdapterSpec(
                name="bv",
                binary=str(bin_path),
                args_builder=lambda _context: [],
                timeout_default=0.05,
                parse=lambda stdout: json.loads(stdout),
            )

            result = ADAPT.run_adapter(spec, env={"SKILLBOX_ADAPTER_TIMEOUT_BV": "0.3"}).to_payload()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["payload"], {"ok": True})
        self.assertEqual(result["timeout_seconds"], 0.3)
        self.assertEqual(result["timeout_source"], "SKILLBOX_ADAPTER_TIMEOUT_BV")

    def test_global_timeout_multiplier_is_reported_and_capped(self) -> None:
        calls: list[float] = []

        def fake_run(_command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(float(kwargs["timeout"]))
            return _completed("{}")

        spec = ADAPT.AdapterSpec(
            name="br",
            binary="br",
            args_builder=lambda _context: ["ready", "--json"],
            timeout_default=20.0,
            parse=lambda stdout: json.loads(stdout),
        )

        result = ADAPT.run_adapter(
            spec,
            env={"SKILLBOX_ADAPTER_TIMEOUT": "3"},
            subprocess_run=fake_run,
        ).to_payload()

        self.assertEqual(calls, [ADAPT.MAX_ADAPTER_TIMEOUT_SECONDS])
        self.assertEqual(result["timeout_seconds"], ADAPT.MAX_ADAPTER_TIMEOUT_SECONDS)
        self.assertEqual(result["timeout_source"], "SKILLBOX_ADAPTER_TIMEOUT")
        self.assertEqual(result["warnings"][0]["code"], "ADAPTER_TIMEOUT_CAPPED")

    def test_timeout_missing_binary_nonzero_and_parse_error_have_distinct_statuses(self) -> None:
        def timeout_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(["br"], timeout=1.5)

        timeout_result = ADAPT.run_command_adapter("br", ["br"], subprocess_run=timeout_run)
        self.assertEqual(timeout_result["status"], "timeout")

        def missing_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("missing")

        missing_result = ADAPT.run_command_adapter("br", ["br"], subprocess_run=missing_run)
        self.assertEqual(missing_result["status"], "unavailable")

        def nonzero_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return _completed("{}", returncode=12)

        nonzero_result = ADAPT.run_command_adapter("br", ["br"], subprocess_run=nonzero_run)
        self.assertEqual(nonzero_result["status"], "nonzero_exit")

        def bad_json(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return _completed("{")

        parse_result = ADAPT.run_command_adapter("br", ["br"], subprocess_run=bad_json)
        self.assertEqual(parse_result["status"], "parse_error")

    def test_runner_fault_injection_never_raises(self) -> None:
        specs = [
            ADAPT.AdapterSpec(
                name="br",
                binary="br",
                args_builder=lambda _context: (_ for _ in ()).throw(RuntimeError("args broke")),
                timeout_default=0.1,
                parse=lambda stdout: json.loads(stdout),
            ),
            ADAPT.AdapterSpec(
                name="bv",
                binary="bv",
                args_builder=lambda _context: [],
                timeout_default=0.1,
                parse=lambda _stdout: (_ for _ in ()).throw(RuntimeError("parse broke")),
            ),
        ]

        args_result = ADAPT.run_adapter(specs[0], subprocess_run=lambda *_a, **_k: _completed("{}")).to_payload()
        parse_result = ADAPT.run_adapter(specs[1], subprocess_run=lambda *_a, **_k: _completed("{}")).to_payload()

        def boom(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise RuntimeError("subprocess broke")

        subprocess_result = ADAPT.run_command_adapter("sbp", ["sbp"], subprocess_run=boom)

        self.assertEqual(args_result["status"], "unavailable")
        self.assertEqual(parse_result["status"], "parse_error")
        self.assertEqual(subprocess_result["status"], "unavailable")

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

    def test_collect_evidence_exposes_elapsed_and_applied_timeout(self) -> None:
        def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return _completed("{}")

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                **os.environ,
                "SKILLBOX_ADAPTER_TIMEOUT_BR": "0.2",
                "SKILLBOX_ADAPTER_TIMEOUT_BV": "0.2",
                "SKILLBOX_ADAPTER_TIMEOUT_SBP": "0.2",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(ADAPT.subprocess, "run", side_effect=fake_run):
                    payload = ADAPT.collect_agent_adapter_evidence(Path(tmpdir))

        br_ready = payload["adapters"]["br_ready"]
        self.assertEqual(br_ready["status"], "ok")
        self.assertEqual(br_ready["source_command"], ["br", "ready", "--json"])
        self.assertEqual(br_ready["timeout_seconds"], 0.2)
        self.assertIn("elapsed_ms", br_ready)


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
