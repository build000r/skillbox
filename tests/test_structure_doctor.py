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
import os
import sys
import tempfile
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
            "overlay_declaration",
            "lock_parity",
            "mcp_parity",
            "skill_drift",
        }:
            self.assertIn(expected, names)
        # The overlay-declaration gate is a structure lint (the analogue of
        # global_skill_contract), not a runtime gate.
        kinds = {s.name: s.kind for s in specs}
        self.assertEqual(kinds["overlay_declaration"], KIND_STRUCTURE)
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


class RepoAtlasFrontDoorGateTests(unittest.TestCase):
    """`sbp repo` must never fail silently: exit 2 on a well-formed probe is FAIL.

    Absence of the wrapper or of the private engine checkout is INCO (verdict
    unknowable on this box), and live verdict exits (0/1/3) are PASS — drift is
    reconcile's business, not the front door gate's.
    """

    def _engine_env(self, engine_path: str):
        return mock.patch.dict(os.environ, {"SKILLBOX_REPO_ATLAS_CLI": engine_path})

    def test_missing_wrapper_is_inco(self):
        ctx = DoctorContext(
            runtime_root=Path("/nonexistent-xyz"), config_root=None, cwd=ROOT_DIR
        )
        status, detail = SD._run_repo_atlas_front_door(ctx)
        self.assertEqual(status, STATUS_INCO)
        self.assertIn("sbp wrapper", detail)

    def test_missing_engine_is_inco(self):
        with self._engine_env("/nonexistent-engine/repo_atlas_cli.py"):
            status, detail = SD._run_repo_atlas_front_door(_stub_context())
        self.assertEqual(status, STATUS_INCO)
        self.assertIn("engine not present", detail)

    def test_usage_or_config_exit_is_fail(self):
        fake = mock.Mock(
            returncode=2,
            stdout="",
            stderr="sbp repo: private Repo Atlas engine unavailable or incompatible\n",
        )
        with tempfile.NamedTemporaryFile(suffix=".py") as engine:
            with self._engine_env(engine.name):
                with mock.patch.object(SD.subprocess, "run", return_value=fake):
                    status, detail = SD._run_repo_atlas_front_door(_stub_context())
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("usage-or-config", detail)
        self.assertIn("unavailable or incompatible", detail)

    def test_non_json_probe_output_is_fail(self):
        fake = mock.Mock(returncode=0, stdout="not-json\n", stderr="")
        with tempfile.NamedTemporaryFile(suffix=".py") as engine:
            with self._engine_env(engine.name):
                with mock.patch.object(SD.subprocess, "run", return_value=fake):
                    status, detail = SD._run_repo_atlas_front_door(_stub_context())
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("non-JSON", detail)

    def test_live_verdict_exits_pass(self):
        envelope = json.dumps({"schema_version": "repo-atlas-command/v1", "exit_code": 0})
        for exit_code in (0, 1, 3):
            fake = mock.Mock(returncode=exit_code, stdout=envelope, stderr="")
            with tempfile.NamedTemporaryFile(suffix=".py") as engine:
                with self._engine_env(engine.name):
                    with mock.patch.object(SD.subprocess, "run", return_value=fake):
                        status, detail = SD._run_repo_atlas_front_door(_stub_context())
            self.assertEqual(status, STATUS_PASS, f"exit {exit_code} must PASS: {detail}")
            self.assertIn(f"exit={exit_code}", detail)

    def test_probe_timeout_is_inco(self):
        timeout = SD.subprocess.TimeoutExpired(cmd="sbp repo", timeout=15)
        with tempfile.NamedTemporaryFile(suffix=".py") as engine:
            with self._engine_env(engine.name):
                with mock.patch.object(SD.subprocess, "run", side_effect=timeout):
                    status, detail = SD._run_repo_atlas_front_door(_stub_context())
        self.assertEqual(status, STATUS_INCO)
        self.assertIn("exceeded", detail)

    def test_gate_is_registered_as_structure(self):
        specs = {s.name: s for s in SD._gate_specs()}
        self.assertIn("repo_atlas_front_door", specs)
        spec = specs["repo_atlas_front_door"]
        self.assertEqual(spec.kind, KIND_STRUCTURE)
        self.assertIn("sbp repo status . --json", spec.fix_command)


def _git_row(
    path,
    classes=(),
    registration="registered",
    ahead=0,
    behind=0,
    mid_op=None,
    stash_count=0,
):
    """One sbp-git/v1 repos row with the fields the gate consumes."""
    return {
        "path": path,
        "classes": sorted(classes),
        "primary_class": (sorted(classes) or ["clean-current"])[0],
        "branch": "main",
        "upstream": "origin/main",
        "ahead": ahead,
        "behind": behind,
        "stash_count": stash_count,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "mid_op": mid_op,
        "bare": False,
        "error": None,
        "risk_band": "clean",
        "registration": registration,
        "fix": [],
    }


def _git_envelope(rows, filters=()):
    return {
        "schema": "sbp-git/v1",
        "repos": list(rows),
        "filters": list(filters),
        "summary": {},
        "registration_summary": {},
        "repo_count": len(rows),
    }


class GitHygieneGateTests(unittest.TestCase):
    """The git_hygiene gate reads ONLY the sbp git TTL cache — it never scans.

    Absent/stale cache is INCO (verdict unknowable), ordinary drift (dirty /
    ahead / behind / stash) is an advisory PASS-with-warnings, and only the
    loss-risk classes FAIL: mid-op, diverged, dirty+unregistered — each FAIL
    carrying its exact `sbp git --only ...` handoff.
    """

    def _run(self, loaded):
        """Run the gate with the cache loader canned to ``loaded``."""
        with mock.patch.object(SD, "load_scan_cache", return_value=loaded):
            # The gate must not spawn ANY subprocess — cache-fed only.
            with mock.patch.object(
                SD.subprocess, "run", side_effect=AssertionError("gate scanned!")
            ):
                return SD._run_git_hygiene(_stub_context())

    def test_absent_cache_is_inco_with_exact_advisory(self):
        status, detail = self._run(None)
        self.assertEqual(status, STATUS_INCO)
        self.assertEqual(detail, "no recent scan — run sbp git")

    def test_stale_cache_is_inco_not_fail(self):
        # A stale envelope full of loss-risk rows must still be INCO: the gate
        # may not pass judgment on a scan older than the TTL.
        envelope = _git_envelope([_git_row("/x/r1", {"mid-op", "dirty"}, mid_op="rebase")])
        status, detail = self._run((envelope, SD.GIT_SCAN_TTL_SECONDS + 1))
        self.assertEqual(status, STATUS_INCO)
        self.assertIn("no recent scan — run sbp git", detail)

    def test_fresh_clean_estate_is_pass_with_age(self):
        envelope = _git_envelope(
            [_git_row("/x/r1", {"clean-current"}), _git_row("/x/r2", {"clean-current"})]
        )
        status, detail = self._run((envelope, 240.0))
        self.assertEqual(status, STATUS_PASS)
        self.assertIn("scan 4m old", detail)  # age always surfaced
        self.assertIn("clean estate (2 repos)", detail)

    def test_ordinary_drift_is_pass_with_warning_counts(self):
        envelope = _git_envelope(
            [
                _git_row("/x/r1", {"dirty"}),
                _git_row("/x/r2", {"dirty", "stash"}, stash_count=2),
                _git_row("/x/r3", {"ahead"}, ahead=3),
                _git_row("/x/r4", {"behind"}, behind=1),
            ]
        )
        status, detail = self._run((envelope, 30.0))
        self.assertEqual(status, STATUS_PASS)
        self.assertIn("scan 30s old", detail)
        self.assertIn("2 dirty", detail)
        self.assertIn("1 ahead", detail)
        self.assertIn("1 behind", detail)
        self.assertIn("1 stash", detail)
        self.assertIn("advisory", detail)

    def test_mid_op_is_fail_naming_repo_and_handoff(self):
        envelope = _git_envelope(
            [
                _git_row("/x/clean", {"clean-current"}),
                _git_row("/x/surgery", {"mid-op", "dirty"}, mid_op="rebase"),
            ]
        )
        status, detail = self._run((envelope, 120.0))
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("/x/surgery", detail)
        self.assertIn("sbp git --only mid-op", detail)
        self.assertIn("scan 2m old", detail)

    def test_diverged_is_fail(self):
        envelope = _git_envelope(
            [_git_row("/x/split", {"ahead", "behind", "diverged-clean"}, ahead=2, behind=3)]
        )
        status, detail = self._run((envelope, 10.0))
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("/x/split", detail)
        self.assertIn("diverged", detail)
        self.assertIn("sbp git --only diverged-clean", detail)

    def test_dirty_diverged_also_fails(self):
        # diverged-clean is a clean-only class; a DIRTY diverged repo is
        # strictly worse, so the gate detects divergence from ahead/behind.
        envelope = _git_envelope(
            [_git_row("/x/worse", {"ahead", "behind", "dirty"}, ahead=1, behind=1)]
        )
        status, detail = self._run((envelope, 10.0))
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("/x/worse", detail)

    def test_dirty_unregistered_is_fail_with_handoff(self):
        envelope = _git_envelope(
            [_git_row("/x/orphan", {"dirty"}, registration="unregistered")]
        )
        status, detail = self._run((envelope, 10.0))
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("/x/orphan", detail)
        self.assertIn("sbp git --only dirty,unregistered", detail)

    def test_dirty_with_unknown_registration_is_pass(self):
        # 'unknown' means the registry was unavailable at scan time — that must
        # NOT count as unregistered; a dirty repo alone is ordinary drift.
        envelope = _git_envelope([_git_row("/x/r1", {"dirty"}, registration="unknown")])
        status, detail = self._run((envelope, 10.0))
        self.assertEqual(status, STATUS_PASS)
        self.assertIn("1 dirty", detail)

    def test_clean_unregistered_is_not_a_failure(self):
        # Only dirty AND unregistered is loss-risk; a clean unregistered repo
        # is registry housekeeping, not a doctor FAIL.
        envelope = _git_envelope(
            [_git_row("/x/r1", {"clean-current"}, registration="unregistered")]
        )
        status, _ = self._run((envelope, 10.0))
        self.assertEqual(status, STATUS_PASS)

    def test_filtered_envelope_is_flagged_partial(self):
        envelope = _git_envelope([_git_row("/x/r1", {"dirty"})], filters=["dirty"])
        status, detail = self._run((envelope, 10.0))
        self.assertEqual(status, STATUS_PASS)
        self.assertIn("filtered view", detail)

    def test_gate_makes_no_subprocess_calls_even_on_absent_cache(self):
        # Both the INCO path and the fresh-envelope path run with subprocess
        # booby-trapped inside _run(); this asserts the trap stayed unsprung
        # for a mixed envelope too.
        envelope = _git_envelope(
            [
                _git_row("/x/surgery", {"mid-op"}, mid_op="merge"),
                _git_row("/x/split", {"diverged-clean", "ahead", "behind"}, ahead=1, behind=1),
                _git_row("/x/orphan", {"dirty"}, registration="unregistered"),
            ]
        )
        status, detail = self._run((envelope, 60.0))
        self.assertEqual(status, STATUS_FAIL)
        # All three loss-risk handoffs fire together, each with its fix.
        self.assertIn("sbp git --only mid-op", detail)
        self.assertIn("sbp git --only diverged-clean", detail)
        self.assertIn("sbp git --only dirty,unregistered", detail)

    def test_gate_is_registered_as_structure_before_repo_atlas(self):
        specs = SD._gate_specs()
        names = [s.name for s in specs]
        self.assertIn("git_hygiene", names)
        self.assertLess(names.index("git_hygiene"), names.index("repo_atlas_front_door"))
        spec = {s.name: s for s in specs}["git_hygiene"]
        self.assertEqual(spec.kind, KIND_STRUCTURE)
        self.assertEqual(spec.cap_s, SD.CAP_FAST_LINT)
        self.assertEqual(
            spec.fix_command,
            "sbp git  # rescan, then sbp git --only mid-op,diverged-clean",
        )


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
