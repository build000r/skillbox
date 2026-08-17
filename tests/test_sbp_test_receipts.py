"""Three-axis receipt state machine with never-lie verdicts.

The suite is organised around the two ways a test system lies. It can call a
failing test an infrastructure problem (so people re-run instead of read), or it
can call an unknown a pass (so people ship). The matrix tests pin the first, the
reducer and aggregate tests pin the second, and the crash-injection tests make
sure a torn write cannot manufacture either.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import sbp_test_receipts as R  # noqa: E402
from runtime_manager._shared.errors import (  # noqa: E402
    EXIT_DRIFT,
    EXIT_ERROR,
    EXIT_NEEDS_INPUT,
    EXIT_OK,
)

RECEIPTS_SOURCE = ENV_MANAGER_DIR / "runtime_manager" / "sbp_test_receipts.py"

PASSED = R.Verdict(R.TEST_PASSED, R.EXEC_COMPLETED, R.PROOF_COMPLETE)
FAILED = R.Verdict(R.TEST_FAILED, R.EXEC_COMPLETED, R.PROOF_COMPLETE)
TIMED_OUT = R.Verdict(R.TEST_NOT_RUN, R.EXEC_TIMEOUT, R.PROOF_INDETERMINATE)
SKIPPED = R.Verdict(R.TEST_SKIPPED, R.EXEC_CANCELED, R.PROOF_INDETERMINATE)


class ReceiptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_root = Path(temporary.name).resolve() / "state"
        self.run_id = "run-1"

    def assert_refused(self, code: str, action: object) -> R.ReceiptRefusal:
        with self.assertRaises(R.ReceiptRefusal) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def attempt(self, unit_id: str, verdict: R.Verdict, number: int = 1, **kwargs) -> R.Attempt:
        return R.Attempt(unit_id=unit_id, attempt=number, verdict=verdict, **kwargs)

    def receipt(self, units: dict, required: tuple, **kwargs) -> R.RunReceipt:
        return R.RunReceipt(
            run_id=self.run_id,
            plan_digest="a" * 64,
            units=units,
            required=required,
            **kwargs,
        )


class ValidityMatrixTests(ReceiptTestCase):
    """Generated over all 96 nominal triples: invalid is unrepresentable."""

    def test_the_nominal_space_is_ninety_six(self) -> None:
        self.assertEqual(4, len(R.TEST_OUTCOMES))
        self.assertEqual(8, len(R.EXECUTION_OUTCOMES))
        self.assertEqual(3, len(R.PROOF_LEVELS))
        self.assertEqual(96, 4 * 8 * 3)

    def test_most_combinations_are_invalid(self) -> None:
        self.assertEqual(15, len(R.VALID_VERDICTS))
        self.assertEqual(81, len(R.invalid_verdicts()))

    def test_every_valid_triple_constructs(self) -> None:
        for test, execution, proof in sorted(R.VALID_VERDICTS):
            verdict = R.Verdict(test, execution, proof)
            self.assertEqual(test, verdict.test_outcome)

    def test_every_invalid_triple_is_refused(self) -> None:
        # The generated transition test: 81 refusals, no exceptions.
        for triple in R.invalid_verdicts():
            self.assert_refused(
                "verdict_invalid", lambda triple=triple: R.Verdict(*triple)
            )

    def test_unknown_axis_values_are_refused(self) -> None:
        for triple in (
            ("weird", R.EXEC_COMPLETED, R.PROOF_COMPLETE),
            (R.TEST_PASSED, "weird", R.PROOF_COMPLETE),
            (R.TEST_PASSED, R.EXEC_COMPLETED, "weird"),
        ):
            self.assert_refused("verdict_invalid", lambda triple=triple: R.Verdict(*triple))

    def test_healthy_is_not_in_the_vocabulary(self) -> None:
        # Review repair 2026-08-14: `completed` replaces `healthy`, because a
        # nonzero test exit is a completed execution of a failed test. The
        # assertion is about the VALUE, not the prose — the module docstring
        # names `healthy` precisely to explain why it does not exist.
        self.assertIn("completed", R.EXECUTION_OUTCOMES)
        self.assertNotIn("healthy", R.EXECUTION_OUTCOMES)
        exported = {
            getattr(R, name)
            for name in dir(R)
            if name.startswith("EXEC_") and isinstance(getattr(R, name), str)
        }
        self.assertNotIn("healthy", exported)
        for triple in sorted(R.VALID_VERDICTS):
            self.assertNotIn("healthy", triple)

    def test_completed_never_pairs_with_a_unit_that_never_ran(self) -> None:
        for test in (R.TEST_SKIPPED, R.TEST_NOT_RUN):
            for proof in R.PROOF_LEVELS:
                self.assertNotIn((test, R.EXEC_COMPLETED, proof), R.VALID_VERDICTS)

    def test_an_unknown_can_never_be_completely_proven(self) -> None:
        for execution in (
            R.EXEC_TIMEOUT,
            R.EXEC_CANCELED,
            R.EXEC_LAUNCH_FAILED,
            R.EXEC_RESULT_UNAVAILABLE,
            R.EXEC_EXECUTOR_LOST,
            R.EXEC_ADMISSION_UNKNOWN,
            R.EXEC_ARTIFACT_INCOMPLETE,
        ):
            for test in R.TEST_OUTCOMES:
                self.assertNotIn((test, execution, R.PROOF_COMPLETE), R.VALID_VERDICTS)

    def test_only_one_triple_is_green(self) -> None:
        green = [t for t in sorted(R.VALID_VERDICTS) if R.Verdict(*t).green]
        self.assertEqual([(R.TEST_PASSED, R.EXEC_COMPLETED, R.PROOF_COMPLETE)], green)

    def test_no_unproven_verdict_is_ever_green(self) -> None:
        for triple in sorted(R.VALID_VERDICTS):
            verdict = R.Verdict(*triple)
            if verdict.is_unproven:
                self.assertFalse(verdict.green, triple)

    def test_only_a_failed_test_reads_as_a_test_failure(self) -> None:
        for triple in sorted(R.VALID_VERDICTS):
            verdict = R.Verdict(*triple)
            self.assertEqual(triple[0] == R.TEST_FAILED, verdict.is_test_failure, triple)


class VocabularyRepairTests(ReceiptTestCase):
    """A nonzero test exit is a failed test, not a broken executor."""

    def test_a_nonzero_exit_is_a_completed_execution_of_a_failed_test(self) -> None:
        verdict = R.verdict_from_unit_result(
            {"state": "completed", "exit_code": 3, "log_file": "x.log"}
        )
        self.assertEqual(R.TEST_FAILED, verdict.test_outcome)
        self.assertEqual(R.EXEC_COMPLETED, verdict.execution_outcome)
        self.assertEqual(R.PROOF_COMPLETE, verdict.proof)
        self.assertTrue(verdict.is_test_failure)
        self.assertFalse(verdict.is_unproven)

    def test_a_zero_exit_is_the_only_pass(self) -> None:
        verdict = R.verdict_from_unit_result(
            {"state": "completed", "exit_code": 0, "log_file": "x.log"}
        )
        self.assertTrue(verdict.green)

    def test_a_timeout_is_never_a_test_failure(self) -> None:
        verdict = R.verdict_from_unit_result(
            {"state": "timed_out", "exit_code": None, "log_file": "x.log"}
        )
        self.assertEqual(R.TEST_NOT_RUN, verdict.test_outcome)
        self.assertEqual(R.EXEC_TIMEOUT, verdict.execution_outcome)
        self.assertFalse(verdict.is_test_failure)
        self.assertTrue(verdict.is_unproven)

    def test_a_timeout_without_a_log_is_indeterminate_not_partial(self) -> None:
        verdict = R.verdict_from_unit_result(
            {"state": "timed_out", "exit_code": None}, log_present=False
        )
        self.assertEqual(R.PROOF_INDETERMINATE, verdict.proof)

    def test_both_cancellation_spellings_are_accepted(self) -> None:
        # The executor says "cancelled"; this axis is normatively "canceled".
        for state in ("cancelled", "canceled"):
            verdict = R.verdict_from_unit_result({"state": state, "log_file": "x"})
            self.assertEqual(R.EXEC_CANCELED, verdict.execution_outcome)

    def test_a_launch_failure_is_distinguished_from_a_test_failure(self) -> None:
        verdict = R.verdict_from_unit_result(
            {"state": "failed", "exit_code": None, "cause": "could not start unit: OSError"}
        )
        self.assertEqual(R.EXEC_LAUNCH_FAILED, verdict.execution_outcome)
        self.assertFalse(verdict.is_test_failure)

    def test_missing_artifacts_downgrade_the_proof_not_the_test(self) -> None:
        verdict = R.verdict_from_unit_result(
            {"state": "completed", "exit_code": 0, "log_file": "x"},
            artifacts_complete=False,
        )
        self.assertEqual(R.TEST_PASSED, verdict.test_outcome)
        self.assertEqual(R.EXEC_ARTIFACT_INCOMPLETE, verdict.execution_outcome)
        self.assertEqual(R.PROOF_PARTIAL, verdict.proof)
        self.assertFalse(verdict.green, "a pass with missing artifacts is not green")

    def test_a_skip_is_recorded_as_a_called_off_attempt(self) -> None:
        verdict = R.verdict_from_unit_result({"state": "skipped"})
        self.assertEqual(R.TEST_SKIPPED, verdict.test_outcome)
        self.assertEqual(R.SKIP_EXECUTION_OUTCOME, verdict.execution_outcome)

    def test_an_unrecognised_executor_state_is_an_unknown_never_a_pass(self) -> None:
        verdict = R.verdict_from_unit_result({"state": "teleported"})
        self.assertEqual(R.EXEC_EXECUTOR_LOST, verdict.execution_outcome)
        self.assertFalse(verdict.green)

    def test_every_executor_state_maps_to_a_valid_verdict(self) -> None:
        from runtime_manager import sbp_test_executor as EX

        for state in EX.UNIT_STATES:
            verdict = R.verdict_from_unit_result(
                {"state": state, "exit_code": 0 if state == "completed" else None,
                 "log_file": "x"}
            )
            self.assertIn(
                (verdict.test_outcome, verdict.execution_outcome, verdict.proof),
                R.VALID_VERDICTS,
                state,
            )


class ReducerPropertyTests(ReceiptTestCase):
    """Properties that must hold for any sequence of attempts."""

    def test_no_attempts_is_an_honest_unknown(self) -> None:
        verdict = R.reduce_unit([])
        self.assertEqual(R.EXEC_ADMISSION_UNKNOWN, verdict.execution_outcome)
        self.assertFalse(verdict.green)

    def test_the_last_attempt_wins(self) -> None:
        attempts = [
            self.attempt("u", FAILED, 1),
            self.attempt("u", PASSED, 2),
        ]
        self.assertTrue(R.reduce_unit(attempts).green)

    def test_ordering_of_the_input_does_not_matter(self) -> None:
        attempts = [self.attempt("u", PASSED, 2), self.attempt("u", FAILED, 1)]
        self.assertTrue(R.reduce_unit(attempts).green)

    def test_the_reduction_is_never_green_unless_the_last_attempt_is(self) -> None:
        # Property over every ordered pair of valid verdicts.
        verdicts = [R.Verdict(*t) for t in sorted(R.VALID_VERDICTS)]
        for first in verdicts:
            for second in verdicts:
                reduced = R.reduce_unit(
                    [self.attempt("u", first, 1), self.attempt("u", second, 2)]
                )
                self.assertEqual(second.green, reduced.green, (first, second))

    def test_a_retry_never_erases_the_earlier_attempt(self) -> None:
        R.append_attempt(self.state_root, self.run_id, self.attempt("u", FAILED, 1))
        R.append_attempt(self.state_root, self.run_id, self.attempt("u", PASSED, 2))
        stored = R.read_attempts(self.state_root, self.run_id, "u")
        self.assertEqual([1, 2], [item.attempt for item in stored])
        self.assertTrue(R.reduce_unit(stored).green)
        # The flake is still visible, which is the only way it is ever noticed.
        self.assertTrue(stored[0].verdict.is_test_failure)


class AppendOnlyTests(ReceiptTestCase):
    """Attempts are append-only; a retry may not overwrite its predecessor."""

    def test_writing_the_same_attempt_twice_is_refused(self) -> None:
        R.append_attempt(self.state_root, self.run_id, self.attempt("u", PASSED, 1))
        error = self.assert_refused(
            "attempt_exists",
            lambda: R.append_attempt(
                self.state_root, self.run_id, self.attempt("u", FAILED, 1)
            ),
        )
        self.assertEqual(["u"], error.units)
        # And the original survives unchanged.
        self.assertTrue(R.read_attempts(self.state_root, self.run_id, "u")[0].verdict.green)

    def test_the_next_attempt_number_increments(self) -> None:
        self.assertEqual(1, R.next_attempt_number(self.state_root, self.run_id, "u"))
        R.append_attempt(self.state_root, self.run_id, self.attempt("u", FAILED, 1))
        self.assertEqual(2, R.next_attempt_number(self.state_root, self.run_id, "u"))

    def test_attempt_numbers_are_bounded(self) -> None:
        self.assert_refused(
            "attempt_invalid", lambda: self.attempt("u", PASSED, R.MAX_ATTEMPTS + 1)
        )
        self.assert_refused("attempt_invalid", lambda: self.attempt("u", PASSED, 0))

    def test_attempts_round_trip_through_disk(self) -> None:
        original = self.attempt(
            "u", FAILED, 1, exit_code=3, duration_s=1.25, cause="exited 3"
        )
        R.append_attempt(self.state_root, self.run_id, original)
        restored = R.read_attempts(self.state_root, self.run_id, "u")[0]
        self.assertEqual(3, restored.exit_code)
        self.assertEqual("exited 3", restored.cause)
        self.assertEqual(FAILED, restored.verdict)


class CrashInjectionTests(ReceiptTestCase):
    """A crash at either edge of an atomic write leaves the store readable."""

    def raiser(self, phase: str):
        def hook(seen: str, _path: Path) -> None:
            if seen == phase:
                raise KeyboardInterrupt(f"crash {phase} write")

        return hook

    def store_files(self) -> list[str]:
        store = R.run_store(self.state_root, self.run_id)
        if not store.exists():
            return []
        return sorted(str(p.relative_to(store)) for p in store.rglob("*") if p.is_file())

    def test_a_crash_before_the_write_leaves_nothing_behind(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            R.append_attempt(
                self.state_root,
                self.run_id,
                self.attempt("u", PASSED, 1),
                on_write=self.raiser("before"),
            )
        self.assertEqual([], self.store_files())
        self.assertEqual((), R.read_attempts(self.state_root, self.run_id, "u"))

    def test_a_crash_after_the_write_leaves_a_complete_file(self) -> None:
        # os.replace already happened, so the record is whole — never torn.
        with self.assertRaises(KeyboardInterrupt):
            R.append_attempt(
                self.state_root,
                self.run_id,
                self.attempt("u", PASSED, 1),
                on_write=self.raiser("after"),
            )
        stored = R.read_attempts(self.state_root, self.run_id, "u")
        self.assertEqual(1, len(stored))
        self.assertTrue(stored[0].verdict.green)

    def test_a_crash_never_leaves_a_temporary_file(self) -> None:
        for phase in ("before", "after"):
            with self.assertRaises(KeyboardInterrupt):
                R.append_attempt(
                    self.state_root,
                    "run-" + phase,
                    self.attempt("u", PASSED, 1),
                    on_write=self.raiser(phase),
                )
            store = R.run_store(self.state_root, "run-" + phase)
            leftovers = [p.name for p in store.rglob("*.tmp")] if store.exists() else []
            self.assertEqual([], leftovers, phase)

    def test_a_crash_writing_the_receipt_preserves_the_previous_one(self) -> None:
        first = self.receipt({"u": PASSED}, ("u",))
        R.write_receipt(self.state_root, first)
        second = self.receipt({"u": FAILED}, ("u",))
        with self.assertRaises(KeyboardInterrupt):
            R.write_receipt(self.state_root, second, on_write=self.raiser("before"))
        stored = R.read_receipt(self.state_root, self.run_id)
        self.assertTrue(stored["green"], "the previous receipt was corrupted")

    def test_a_crash_writing_the_receipt_never_corrupts_it(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            R.write_receipt(
                self.state_root,
                self.receipt({"u": PASSED}, ("u",)),
                on_write=self.raiser("after"),
            )
        stored = R.read_receipt(self.state_root, self.run_id)
        self.assertEqual(R.RECEIPT_SCHEMA, stored["schema"])


class StorageTests(ReceiptTestCase):
    """Receipts live under the state root, privately, and redacted."""

    def test_the_store_path_is_under_test_runs(self) -> None:
        store = R.run_store(self.state_root, self.run_id)
        self.assertEqual(("test-runs", self.run_id), store.parts[-2:])

    def test_files_are_private_and_directories_are_too(self) -> None:
        path = R.append_attempt(
            self.state_root, self.run_id, self.attempt("u", PASSED, 1)
        )
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(path.parent).st_mode))

    def test_secret_shaped_fields_are_redacted_on_disk(self) -> None:
        attempt = self.attempt(
            "u",
            PASSED,
            1,
            cache_key={"api_token": "sk-live-deadbeef", "argv_digest": "a" * 64},
        )
        path = R.append_attempt(self.state_root, self.run_id, attempt)
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("sk-live-deadbeef", raw)
        self.assertIn("[REDACTED]", raw)
        self.assertIn("a" * 64, raw, "non-secret material must survive")

    def test_a_malformed_run_id_is_refused(self) -> None:
        for run_id in ("", "../escape", "Run 1", "x" * 80):
            self.assert_refused(
                "store_invalid", lambda run_id=run_id: R.run_store(self.state_root, run_id)
            )


class AggregateAndExitLadderTests(ReceiptTestCase):
    """Only harvested exit-0 writes green; everything else is honest about it."""

    def test_a_fully_proven_pass_is_green_and_exits_zero(self) -> None:
        receipt = self.receipt({"u": PASSED}, ("u",))
        self.assertTrue(receipt.green)
        self.assertEqual(EXIT_OK, receipt.exit_code())
        self.assertEqual([], receipt.next_actions())

    def test_a_failed_required_unit_exits_one(self) -> None:
        receipt = self.receipt({"u": FAILED}, ("u",))
        self.assertFalse(receipt.green)
        self.assertEqual(EXIT_ERROR, receipt.exit_code())
        self.assertEqual("failed", receipt.to_payload()["verdict_class"])

    def test_an_unproven_unit_exits_needs_input_with_a_resume(self) -> None:
        receipt = self.receipt({"u": TIMED_OUT}, ("u",))
        self.assertEqual(EXIT_NEEDS_INPUT, receipt.exit_code())
        self.assertEqual(["sbp test resume"], receipt.next_actions())

    def test_indeterminate_is_nonzero_but_never_a_test_failure(self) -> None:
        receipt = self.receipt({"u": TIMED_OUT}, ("u",))
        payload = receipt.to_payload()
        self.assertNotEqual(EXIT_OK, payload["exit_code"])
        self.assertEqual("unproven", payload["verdict_class"])
        self.assertEqual([], payload["failed_units"])

    def test_a_manifest_mismatch_exits_drift(self) -> None:
        receipt = self.receipt({"u": PASSED}, ("u",), manifest_mismatch=("ghost-unit",))
        self.assertEqual(EXIT_DRIFT, receipt.exit_code())
        self.assertEqual("drifted", receipt.to_payload()["verdict_class"])

    def test_drift_outranks_a_failure_which_outranks_unproven(self) -> None:
        both = self.receipt({"a": FAILED, "b": TIMED_OUT}, ("a", "b"))
        self.assertEqual(EXIT_ERROR, both.exit_code())
        drifted = self.receipt(
            {"a": FAILED, "b": TIMED_OUT}, ("a", "b"), manifest_mismatch=("x",)
        )
        self.assertEqual(EXIT_DRIFT, drifted.exit_code())

    def test_a_missing_required_unit_is_unproven_not_green(self) -> None:
        receipt = self.receipt({"a": PASSED}, ("a", "absent"))
        self.assertFalse(receipt.green)
        self.assertEqual(("absent",), receipt.missing_required)
        self.assertEqual(EXIT_NEEDS_INPUT, receipt.exit_code())

    def test_a_skipped_required_unit_does_not_make_a_run_green(self) -> None:
        # A skip means nobody ran it; the run cannot claim it passed.
        receipt = self.receipt({"a": PASSED, "b": SKIPPED}, ("a", "b"))
        self.assertFalse(receipt.green)
        self.assertEqual(("b",), receipt.unproven_units)
        self.assertEqual(EXIT_NEEDS_INPUT, receipt.exit_code())

    def test_a_run_with_no_required_units_is_not_green(self) -> None:
        self.assertFalse(self.receipt({"a": PASSED}, ()).green)

    def test_a_non_required_failure_does_not_sink_the_run(self) -> None:
        receipt = self.receipt({"a": PASSED, "extra": FAILED}, ("a",))
        self.assertTrue(receipt.green)
        self.assertEqual(EXIT_OK, receipt.exit_code())


class FinalizationTests(ReceiptTestCase):
    """Authoritative indeterminate finalization — the missing-artifact case."""

    def test_finalizing_a_completed_unit_downgrades_its_proof(self) -> None:
        receipt = self.receipt({"u": PASSED}, ("u",))
        finalized = R.finalize_indeterminate(receipt, ["u"])
        verdict = finalized.units["u"]
        self.assertEqual(R.EXEC_ARTIFACT_INCOMPLETE, verdict.execution_outcome)
        self.assertEqual(R.PROOF_PARTIAL, verdict.proof)
        self.assertFalse(finalized.green)
        self.assertEqual(EXIT_NEEDS_INPUT, finalized.exit_code())

    def test_finalizing_an_absent_unit_records_an_indeterminate(self) -> None:
        receipt = self.receipt({}, ("u",))
        finalized = R.finalize_indeterminate(receipt, ["u"])
        verdict = finalized.units["u"]
        self.assertEqual(R.TEST_NOT_RUN, verdict.test_outcome)
        self.assertEqual(R.PROOF_INDETERMINATE, verdict.proof)

    def test_finalization_preserves_the_test_outcome_it_knew(self) -> None:
        receipt = self.receipt({"u": FAILED}, ("u",))
        finalized = R.finalize_indeterminate(receipt, ["u"])
        self.assertEqual(R.TEST_FAILED, finalized.units["u"].test_outcome)


class CacheKeyTests(ReceiptTestCase):
    """Cache-key material recorded from day one; nothing consults a cache here."""

    def material(self, **kwargs):
        options = {
            "plan_digest": "a" * 64,
            "manifest_digest": "b" * 64,
            "unit_argv": ["pytest", "-q"],
            "env": {"CI": "1", "TOKEN": "sk-live-deadbeef"},
        }
        options.update(kwargs)
        return R.cache_key_material(**options)  # type: ignore[arg-type]

    def test_env_values_are_digested_never_stored(self) -> None:
        rendered = json.dumps(self.material())
        self.assertNotIn("sk-live-deadbeef", rendered)
        self.assertIn("CI", rendered)

    def test_a_changed_env_value_moves_the_digest(self) -> None:
        first = self.material()["env_digest"]
        second = self.material(env={"CI": "2", "TOKEN": "sk-live-deadbeef"})["env_digest"]
        self.assertNotEqual(first, second)

    def test_a_changed_argv_moves_the_digest(self) -> None:
        self.assertNotEqual(
            self.material()["argv_digest"],
            self.material(unit_argv=["pytest", "-x"])["argv_digest"],
        )

    def test_the_material_is_deterministic(self) -> None:
        self.assertEqual(self.material(), self.material())

    def test_the_module_consults_no_cache(self) -> None:
        # Execution is always fresh in this leaf; cache authority is the P5 leaf.
        source = RECEIPTS_SOURCE.read_text(encoding="utf-8")
        for banned in ("cache_lookup", "cache_hit", "restore_from_cache"):
            self.assertNotIn(banned, source, banned)


class BuildReceiptTests(ReceiptTestCase):
    """End to end: attempts on disk reduce into one receipt."""

    def test_a_run_reduces_every_recorded_unit(self) -> None:
        R.append_attempt(self.state_root, self.run_id, self.attempt("a", FAILED, 1))
        R.append_attempt(self.state_root, self.run_id, self.attempt("a", PASSED, 2))
        R.append_attempt(self.state_root, self.run_id, self.attempt("b", TIMED_OUT, 1))
        receipt = R.build_receipt(
            self.state_root, self.run_id, plan_digest="a" * 64, required=("a", "b")
        )
        self.assertTrue(receipt.units["a"].green)
        self.assertEqual(("b",), receipt.unproven_units)
        self.assertEqual({"a": 2, "b": 1}, dict(receipt.attempts_by_unit))
        self.assertEqual(EXIT_NEEDS_INPUT, receipt.exit_code())

    def test_the_written_receipt_reads_back(self) -> None:
        R.append_attempt(self.state_root, self.run_id, self.attempt("a", PASSED, 1))
        receipt = R.build_receipt(
            self.state_root, self.run_id, plan_digest="a" * 64, required=("a",)
        )
        R.write_receipt(self.state_root, receipt)
        stored = R.read_receipt(self.state_root, self.run_id)
        self.assertTrue(stored["green"])
        self.assertEqual(EXIT_OK, stored["exit_code"])
        self.assertEqual("passed", stored["verdict_class"])

    def test_an_empty_run_is_not_green(self) -> None:
        receipt = R.build_receipt(
            self.state_root, self.run_id, plan_digest="a" * 64, required=("a",)
        )
        self.assertFalse(receipt.green)
        self.assertEqual(EXIT_NEEDS_INPUT, receipt.exit_code())


class ContractTests(ReceiptTestCase):
    """Invariants that keep the never-lie discipline as the module changes."""

    def test_every_refusal_code_in_the_source_is_declared(self) -> None:
        source = RECEIPTS_SOURCE.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\(\s*"([a-z_]+)"', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - R.REFUSAL_CODES)

    def test_the_schema_names_match_the_bead(self) -> None:
        self.assertEqual("test-attempt/v1", R.ATTEMPT_SCHEMA)
        self.assertEqual("test-receipt/v1", R.RECEIPT_SCHEMA)
        self.assertEqual(".skillbox-state/test-runs", R.RECEIPT_STORE_RELPATH)

    def test_the_module_runs_no_command(self) -> None:
        source = RECEIPTS_SOURCE.read_text(encoding="utf-8")
        for banned in ("subprocess", "socket", "os.system", "popen"):
            self.assertNotIn(banned, source, banned)

    def test_payloads_are_json_serializable(self) -> None:
        json.dumps(self.receipt({"u": PASSED}, ("u",)).to_payload())
        json.dumps(self.attempt("u", PASSED, 1).to_payload())


if __name__ == "__main__":
    unittest.main()
