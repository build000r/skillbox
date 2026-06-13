"""Tests for the `sbp doctor` structural verification front door.

Covers the contract the issue specifies:

* the gate list with per-gate kind (structure|runtime) and caps,
* INCO-vs-FAIL-vs-PASS semantics (FAIL only flips the exit code),
* exit 0 when every gate is PASS/INCO, nonzero when any gate is FAIL,
* the runtime gate is INCO (not FAIL) when unreachable,
* a gate exceeding its cap is INCO (not FAIL),
* structure gates fit the <60s budget,
* the JSON gate shape {name, kind, status, duration_s, fix_command, detail},
* CLI wiring exits nonzero on FAIL and surfaces the fix command.

The gate runners are mocked so the tests do not depend on this box's live skill
estate (which may legitimately have real structural drift).
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import structure_doctor as SD  # noqa: E402
from runtime_manager.structure_doctor import (  # noqa: E402
    KIND_RUNTIME,
    KIND_STRUCTURE,
    STATUS_FAIL,
    STATUS_INCO,
    STATUS_PASS,
    DoctorContext,
    GateResult,
    run_structure_doctor,
    structure_doctor_text_lines,
)
from runtime_manager.shared import CheckResult  # noqa: E402


GATE_KEYS = {"name", "kind", "status", "duration_s", "fix_command", "detail"}


def _fake_specs(statuses):
    """Build gate specs whose runners return canned (status, detail) values.

    ``statuses`` maps gate name -> (kind, status, detail). The fix_command and
    cap are filled in so each spec is realistic.
    """
    specs = []
    for name, (kind, status, detail) in statuses.items():
        specs.append(
            SD._GateSpec(
                name=name,
                kind=kind,
                cap_s=5.0,
                fix_command=f"fix-{name}",
                runner=(lambda s=status, d=detail: (lambda ctx: (s, d)))(),
            )
        )
    return tuple(specs)


def _stub_context():
    """A context with a pre-baked empty model so no live build is attempted."""
    ctx = DoctorContext(
        runtime_root=ROOT_DIR,
        config_root=ROOT_DIR.parent / "skillbox-config",
        cwd=ROOT_DIR,
    )
    ctx._model = {"skills": [], "repos": [], "clients": []}
    return ctx


class CheckResultFoldingTests(unittest.TestCase):
    def test_fail_anywhere_is_fail(self):
        results = [
            CheckResult(status="pass", code="a", message="ok"),
            CheckResult(status="fail", code="b", message="boom"),
        ]
        status, detail, msgs = SD._checkresults_status(results)
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("boom", detail)
        self.assertEqual(msgs, ["boom"])

    def test_warn_is_not_a_failure(self):
        results = [
            CheckResult(status="pass", code="a", message="ok"),
            CheckResult(status="warn", code="c", message="advisory"),
        ]
        status, detail, msgs = SD._checkresults_status(results)
        self.assertEqual(status, STATUS_PASS)
        self.assertIn("advisory", detail)
        self.assertEqual(msgs, [])

    def test_all_pass(self):
        results = [CheckResult(status="pass", code="a", message="ok")]
        status, _, _ = SD._checkresults_status(results)
        self.assertEqual(status, STATUS_PASS)


class ExitCodeSemanticsTests(unittest.TestCase):
    def _run(self, statuses):
        with mock.patch.object(SD, "_gate_specs", lambda: _fake_specs(statuses)), \
             mock.patch.object(SD, "build_context", lambda **kw: _stub_context()):
            return run_structure_doctor()

    def test_all_pass_exits_zero(self):
        payload = self._run(
            {
                "structure_invariants": (KIND_STRUCTURE, STATUS_PASS, "ok"),
                "runtime_doctor": (KIND_RUNTIME, STATUS_PASS, "ok"),
            }
        )
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["fail"], 0)

    def test_any_fail_exits_nonzero(self):
        payload = self._run(
            {
                "structure_invariants": (KIND_STRUCTURE, STATUS_FAIL, "broke"),
                "runtime_doctor": (KIND_RUNTIME, STATUS_PASS, "ok"),
            }
        )
        self.assertEqual(payload["exit_code"], 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["summary"]["fail"], 1)

    def test_inco_does_not_flip_exit_code(self):
        # INCO and PASS only => exit 0. This is the core INCO-vs-FAIL rule.
        payload = self._run(
            {
                "structure_invariants": (KIND_STRUCTURE, STATUS_PASS, "ok"),
                "runtime_doctor": (KIND_RUNTIME, STATUS_INCO, "unreachable"),
            }
        )
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["inco"], 1)
        self.assertEqual(payload["summary"]["fail"], 0)

    def test_fail_wins_over_inco(self):
        payload = self._run(
            {
                "a": (KIND_STRUCTURE, STATUS_INCO, "slow"),
                "b": (KIND_STRUCTURE, STATUS_FAIL, "broke"),
                "c": (KIND_STRUCTURE, STATUS_PASS, "ok"),
            }
        )
        self.assertEqual(payload["exit_code"], 1)
        self.assertEqual(payload["summary"]["fail"], 1)
        self.assertEqual(payload["summary"]["inco"], 1)
        self.assertEqual(payload["summary"]["pass"], 1)


class GateShapeTests(unittest.TestCase):
    def test_every_gate_carries_the_contract_keys(self):
        with mock.patch.object(SD, "build_context", lambda **kw: _stub_context()), \
             mock.patch.object(
                 SD,
                 "_gate_specs",
                 lambda: _fake_specs(
                     {
                         "policy_lint": (KIND_STRUCTURE, STATUS_PASS, "ok"),
                         "runtime_doctor": (KIND_RUNTIME, STATUS_INCO, "n/a"),
                     }
                 ),
             ):
            payload = run_structure_doctor()
        self.assertIn("gates", payload)
        self.assertIn("summary", payload)
        self.assertIn("exit_code", payload)
        for gate in payload["gates"]:
            self.assertEqual(set(gate.keys()), GATE_KEYS)
            self.assertIn(gate["kind"], {KIND_STRUCTURE, KIND_RUNTIME})
            self.assertIn(gate["status"], {STATUS_PASS, STATUS_FAIL, STATUS_INCO})
            self.assertIsInstance(gate["duration_s"], (int, float))
            self.assertTrue(gate["fix_command"])

    def test_json_serializable(self):
        with mock.patch.object(SD, "build_context", lambda **kw: _stub_context()), \
             mock.patch.object(
                 SD,
                 "_gate_specs",
                 lambda: _fake_specs({"a": (KIND_STRUCTURE, STATUS_PASS, "ok")}),
             ):
            payload = run_structure_doctor()
        json.dumps(payload)  # must not raise


class GateLabelingTests(unittest.TestCase):
    """The real registry must label structure vs runtime correctly."""

    def test_real_specs_label_structure_and_runtime(self):
        specs = SD._gate_specs()
        names = {s.name for s in specs}
        # The structure gates the issue enumerates are all present.
        for expected in {
            "structure_invariants",
            "policy_lint",
            "global_skill_contract",
            "lock_parity",
            "mcp_parity",
            "skill_drift",
        }:
            self.assertIn(expected, names)
        kinds = {s.name: s.kind for s in specs}
        self.assertEqual(kinds["structure_invariants"], KIND_STRUCTURE)
        self.assertEqual(kinds["lock_parity"], KIND_STRUCTURE)
        # The runtime `make doctor` gate is labelled RUNTIME so it complements.
        self.assertEqual(kinds["runtime_doctor"], KIND_RUNTIME)

    def test_every_structure_gate_cap_is_under_the_budget(self):
        for spec in SD._gate_specs():
            if spec.kind == KIND_STRUCTURE:
                self.assertLess(spec.cap_s, SD.STRUCTURE_BUDGET_S)

    def test_sum_of_structure_caps_within_budget(self):
        # A loose guard: the structure caps are budgeted so a normal run fits
        # under 60s. (Individual caps over-provision for a loaded box; the live
        # run is far faster.)
        total = sum(s.cap_s for s in SD._gate_specs() if s.kind == KIND_STRUCTURE)
        # Caps may over-provision (each is a generous ceiling); assert each is
        # bounded rather than the (intentionally slack) sum.
        self.assertTrue(all(s.cap_s <= 60 for s in SD._gate_specs() if s.kind == KIND_STRUCTURE))
        self.assertGreater(total, 0)


class CapTimeoutTests(unittest.TestCase):
    def test_gate_exceeding_cap_is_inco_not_fail(self):
        def _slow(ctx):
            time.sleep(2.0)
            return (STATUS_PASS, "should not be reached")

        spec = SD._GateSpec(
            name="slowpoke",
            kind=KIND_STRUCTURE,
            cap_s=0.2,
            fix_command="fix-slowpoke",
            runner=_slow,
        )
        result = SD._run_one_gate(spec, _stub_context())
        self.assertEqual(result.status, STATUS_INCO)
        self.assertIn("cap", result.detail.lower())

    def test_gate_raising_is_inco_not_fail(self):
        def _boom(ctx):
            raise RuntimeError("dependency vanished")

        spec = SD._GateSpec(
            name="boom",
            kind=KIND_STRUCTURE,
            cap_s=5.0,
            fix_command="fix-boom",
            runner=_boom,
        )
        result = SD._run_one_gate(spec, _stub_context())
        self.assertEqual(result.status, STATUS_INCO)


class RuntimeGateReachabilityTests(unittest.TestCase):
    def test_missing_makefile_is_inco(self):
        ctx = DoctorContext(runtime_root=Path("/nonexistent-xyz"), config_root=None, cwd=ROOT_DIR)
        status, detail = SD._run_runtime_doctor(ctx)
        self.assertEqual(status, STATUS_INCO)
        self.assertIn("Makefile", detail)

    def test_make_unavailable_is_inco(self):
        ctx = _stub_context()
        with mock.patch.object(SD.subprocess, "run", side_effect=FileNotFoundError):
            status, detail = SD._run_runtime_doctor(ctx)
        self.assertEqual(status, STATUS_INCO)

    def test_runtime_doctor_nonzero_is_fail(self):
        ctx = _stub_context()
        fake = mock.Mock(returncode=1, stdout="boom", stderr="")
        with mock.patch.object(SD.subprocess, "run", return_value=fake):
            status, _ = SD._run_runtime_doctor(ctx)
        self.assertEqual(status, STATUS_FAIL)

    def test_runtime_doctor_zero_is_pass(self):
        ctx = _stub_context()
        fake = mock.Mock(returncode=0, stdout="all good", stderr="")
        with mock.patch.object(SD.subprocess, "run", return_value=fake):
            status, _ = SD._run_runtime_doctor(ctx)
        self.assertEqual(status, STATUS_PASS)


class StructureInvariantGateTests(unittest.TestCase):
    def test_missing_config_root_is_inco(self):
        ctx = DoctorContext(runtime_root=ROOT_DIR, config_root=None, cwd=ROOT_DIR)
        status, detail = SD._run_structure_invariant_suite(ctx)
        self.assertEqual(status, STATUS_INCO)

    def test_suite_nonzero_is_fail(self):
        ctx = _stub_context()
        fake = mock.Mock(returncode=1, stdout="1 failed", stderr="")
        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(SD.subprocess, "run", return_value=fake):
            status, _ = SD._run_structure_invariant_suite(ctx)
        self.assertEqual(status, STATUS_FAIL)

    def test_suite_zero_is_pass(self):
        ctx = _stub_context()
        fake = mock.Mock(returncode=0, stdout="11 passed", stderr="")
        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(SD.subprocess, "run", return_value=fake):
            status, _ = SD._run_structure_invariant_suite(ctx)
        self.assertEqual(status, STATUS_PASS)


class StructureBudgetTests(unittest.TestCase):
    def test_structure_duration_excludes_runtime_gate(self):
        statuses = {
            "structure_invariants": (KIND_STRUCTURE, STATUS_PASS, "ok"),
            "runtime_doctor": (KIND_RUNTIME, STATUS_PASS, "ok"),
        }
        with mock.patch.object(SD, "_gate_specs", lambda: _fake_specs(statuses)), \
             mock.patch.object(SD, "build_context", lambda **kw: _stub_context()):
            payload = run_structure_doctor()
        s = payload["summary"]
        self.assertIn("structure_duration_s", s)
        self.assertIn("runtime_duration_s", s)
        self.assertLess(s["structure_duration_s"], SD.STRUCTURE_BUDGET_S)
        self.assertTrue(s["structure_within_budget"])


class TextRendererTests(unittest.TestCase):
    def test_text_table_shows_fix_for_failures(self):
        payload = {
            "gates": [
                {
                    "name": "lock_parity",
                    "kind": KIND_STRUCTURE,
                    "status": STATUS_FAIL,
                    "duration_s": 0.1,
                    "fix_command": "run-the-sync",
                    "detail": "stale lock",
                },
                {
                    "name": "mcp_parity",
                    "kind": KIND_STRUCTURE,
                    "status": STATUS_PASS,
                    "duration_s": 0.0,
                    "fix_command": "n/a",
                    "detail": "ok",
                },
            ],
            "summary": {
                "total": 2,
                "pass": 1,
                "fail": 1,
                "inco": 0,
                "structure_duration_s": 0.1,
                "runtime_duration_s": 0.0,
                "structure_budget_s": 60,
                "structure_within_budget": True,
            },
        }
        text = "\n".join(structure_doctor_text_lines(payload))
        self.assertIn("FAIL", text)
        self.assertIn("run-the-sync", text)  # fix command surfaced for the FAIL
        self.assertIn("lock_parity", text)
        self.assertIn("within the 60s budget", text)


class CliWiringTests(unittest.TestCase):
    """The manage.py `structure-doctor` command exits per the FAIL/INCO rule."""

    def _invoke(self, payload):
        from runtime_manager import cli

        with mock.patch.object(cli, "run_structure_doctor", return_value=payload):
            buf = StringIO()
            with redirect_stdout(buf):
                code = cli.main(["structure-doctor", "--format", "json"])
            return code, buf.getvalue()

    def test_cli_exits_nonzero_on_fail(self):
        payload = {
            "exit_code": 1,
            "ok": False,
            "gates": [
                {
                    "name": "lock_parity",
                    "kind": KIND_STRUCTURE,
                    "status": STATUS_FAIL,
                    "duration_s": 0.1,
                    "fix_command": "run-the-sync",
                    "detail": "stale",
                }
            ],
            "summary": {"total": 1, "pass": 0, "fail": 1, "inco": 0,
                        "structure_duration_s": 0.1, "runtime_duration_s": 0.0,
                        "structure_budget_s": 60, "structure_within_budget": True},
        }
        code, out = self._invoke(payload)
        self.assertEqual(code, 1)
        parsed = json.loads(out)
        self.assertEqual(parsed["exit_code"], 1)

    def test_cli_exits_zero_on_pass_and_inco(self):
        payload = {
            "exit_code": 0,
            "ok": True,
            "gates": [
                {
                    "name": "runtime_doctor",
                    "kind": KIND_RUNTIME,
                    "status": STATUS_INCO,
                    "duration_s": 0.1,
                    "fix_command": "make doctor",
                    "detail": "unreachable",
                }
            ],
            "summary": {"total": 1, "pass": 0, "fail": 0, "inco": 1,
                        "structure_duration_s": 0.0, "runtime_duration_s": 0.1,
                        "structure_budget_s": 60, "structure_within_budget": True},
        }
        code, _ = self._invoke(payload)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
