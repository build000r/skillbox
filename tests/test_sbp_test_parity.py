"""Skillbox as the zeroth consumer: manifest/gate parity and the N=5 ledger.

Two rival proof formats out of one codebase is a schism waiting to happen, so
the tests here are about withholding confidence rather than demonstrating it:
the ledger must refuse to hand authority to `sbp test` until five consecutive
canonical runs have actually agreed, and "agreed" has to mean something strict
enough that it cannot be satisfied by accident.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import sbp_test_manifest as M  # noqa: E402
from runtime_manager import sbp_test_parity as PAR  # noqa: E402
from runtime_manager import sbp_test_receipts as R  # noqa: E402

SELF_TEST_SH = ROOT_DIR / "scripts" / "self-test.sh"
PARITY_SOURCE = ENV_MANAGER_DIR / "runtime_manager" / "sbp_test_parity.py"


def gate_receipt(lanes: list[dict], *, status: str = "pass", canonical: bool = True) -> dict:
    return {
        "schema": PAR.SELF_TEST_SCHEMA,
        "commit": "a" * 40,
        "canonical": canonical,
        "status": status,
        "lanes": lanes,
    }


def lane(unit_id: str, status: str = "pass", duration_s: int = 1) -> dict:
    return {
        "id": unit_id,
        "status": status,
        "exit_code": 0 if status == "pass" else 1,
        "duration_s": duration_s,
    }


def sbp_receipt(units: dict[str, R.Verdict], *, green: bool) -> dict:
    return {
        "schema": R.RECEIPT_SCHEMA,
        "green": green,
        "units": {uid: verdict.to_payload() for uid, verdict in units.items()},
    }


PASSED = R.Verdict(R.TEST_PASSED, R.EXEC_COMPLETED, R.PROOF_COMPLETE)
FAILED = R.Verdict(R.TEST_FAILED, R.EXEC_COMPLETED, R.PROOF_COMPLETE)
TIMED_OUT = R.Verdict(R.TEST_NOT_RUN, R.EXEC_TIMEOUT, R.PROOF_INDETERMINATE)


class ParityTestCase(unittest.TestCase):
    def assert_refused(self, code: str, action: object) -> PAR.ParityRefusal:
        with self.assertRaises(PAR.ParityRefusal) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception


class RepoManifestMirrorsTheGateTests(ParityTestCase):
    """The manifest must describe the lanes the canonical gate actually runs."""

    def gate_lane_ids(self) -> set[str]:
        """Lane ids parsed out of self-test.sh, matrix loop expanded."""
        source = SELF_TEST_SH.read_text(encoding="utf-8")
        literal = set(re.findall(r'run_lane "([a-z0-9][a-z0-9.\-]*)"', source))
        versions = re.search(r'PYTHON_VERSIONS=\(([^)]*)\)', source)
        coverage = re.search(r'COVERAGE_PYTHON="([0-9.]+)"', source)
        self.assertIsNotNone(versions, "could not read PYTHON_VERSIONS")
        self.assertIsNotNone(coverage, "could not read COVERAGE_PYTHON")
        expanded = set()
        for raw in re.findall(r'"([0-9.]+)"', versions.group(1)):
            expanded.add(
                f"test-{raw}-coverage" if raw == coverage.group(1) else f"test-{raw}"
            )
        return literal | expanded

    def manifest(self):
        manifest, findings = M.load_manifest(ROOT_DIR)
        self.assertEqual([], findings, "the repo manifest must lint clean")
        self.assertIsNotNone(manifest)
        return manifest

    def test_the_repo_manifest_lints_clean(self) -> None:
        self.manifest()

    def test_the_gate_group_matches_the_self_test_lanes_exactly(self) -> None:
        # The zeroth-consumer claim in one assertion. If a lane is added to the
        # gate and not to the manifest (or vice versa), parity would silently
        # compare two different things.
        manifest = self.manifest()
        self.assertIn("gate", manifest.groups)
        self.assertEqual(self.gate_lane_ids(), set(manifest.groups["gate"]))

    def test_the_pinned_matrix_is_three_parallel_units(self) -> None:
        manifest = self.manifest()
        matrix = sorted(uid for uid in manifest.units if uid.startswith("test-3."))
        self.assertEqual(3, len(matrix), matrix)
        for unit_id in matrix:
            unit = manifest.units[unit_id]
            # No resource_group and not exclusive: the whole point of expressing
            # the matrix as units instead of a shell loop is that they may run
            # at the same time.
            self.assertIsNone(unit.resource_group, unit_id)
            self.assertEqual("shared", unit.exclusivity, unit_id)

    def test_the_default_group_needs_no_daemon(self) -> None:
        manifest = self.manifest()
        for unit_id in manifest.groups[M.DEFAULT_GROUP]:
            caps = list(manifest.units[unit_id].requires.get("caps") or [])
            self.assertNotIn("docker", caps, unit_id)

    def test_compose_declares_its_docker_requirement(self) -> None:
        # So a machine without Docker reports "cannot host this" rather than
        # quietly counting the check as done.
        manifest = self.manifest()
        self.assertIn("docker", manifest.units["compose"].requires.get("caps") or [])

    def test_lanes_declare_no_invented_dependencies(self) -> None:
        # self-test.sh runs lanes in a fixed order, but that is sequencing, not
        # dependency; recording it as one would serialize independent units.
        manifest = self.manifest()
        for unit_id in manifest.groups["gate"]:
            self.assertEqual((), manifest.units[unit_id].depends_on, unit_id)


class LaneTranslationTests(ParityTestCase):
    """Both formats are read through one vocabulary."""

    def test_a_failing_lane_is_a_failed_test_not_broken_infrastructure(self) -> None:
        verdict = PAR.lane_verdict(lane("lint", "fail"))
        self.assertEqual(R.TEST_FAILED, verdict.test_outcome)
        self.assertEqual(R.EXEC_COMPLETED, verdict.execution_outcome)
        self.assertEqual(R.PROOF_COMPLETE, verdict.proof)

    def test_a_passing_lane_is_green(self) -> None:
        self.assertTrue(PAR.lane_verdict(lane("lint")).green)

    def test_an_unknown_lane_status_is_never_a_pass(self) -> None:
        verdict = PAR.lane_verdict({"id": "x", "status": "who-knows"})
        self.assertFalse(verdict.green)
        self.assertEqual(R.EXEC_ADMISSION_UNKNOWN, verdict.execution_outcome)

    def test_a_wrong_schema_is_refused(self) -> None:
        self.assert_refused(
            "parity_input_invalid",
            lambda: PAR.normalize_self_test({"schema": "other/1", "lanes": []}),
        )

    def test_a_receipt_without_lanes_is_refused(self) -> None:
        self.assert_refused(
            "parity_input_invalid",
            lambda: PAR.normalize_self_test({"schema": PAR.SELF_TEST_SCHEMA}),
        )

    def test_the_lane_mapping_is_identity_today(self) -> None:
        self.assertEqual("lint", PAR.unit_for_lane("lint"))


class ComparisonTests(ParityTestCase):
    """What counts as agreement, and what deliberately does not."""

    def test_identical_outcomes_agree(self) -> None:
        observation = PAR.compare(
            gate_receipt([lane("lint"), lane("render")]),
            sbp_receipt({"lint": PASSED, "render": PASSED}, green=True),
        )
        self.assertTrue(observation.agrees)
        self.assertEqual((), observation.disagreements)

    def test_a_per_lane_disagreement_breaks_parity(self) -> None:
        observation = PAR.compare(
            gate_receipt([lane("lint"), lane("render")], status="pass"),
            sbp_receipt({"lint": FAILED, "render": PASSED}, green=False),
        )
        self.assertFalse(observation.agrees)
        self.assertEqual(("lint",), observation.disagreements)

    def test_agreeing_on_red_while_disagreeing_on_which_lane_is_not_parity(self) -> None:
        # Both say "red", for different reasons. A verdict-only comparison would
        # call this agreement and be wrong.
        observation = PAR.compare(
            gate_receipt([lane("lint", "fail"), lane("render")], status="fail"),
            sbp_receipt({"lint": PASSED, "render": FAILED}, green=False),
        )
        self.assertTrue(observation.verdict_agrees)
        self.assertFalse(observation.agrees)
        self.assertEqual(("lint", "render"), observation.disagreements)

    def test_a_lane_missing_from_one_side_is_a_disagreement(self) -> None:
        observation = PAR.compare(
            gate_receipt([lane("lint"), lane("render")]),
            sbp_receipt({"lint": PASSED}, green=True),
        )
        self.assertFalse(observation.agrees)
        self.assertEqual(("render",), observation.disagreements)

    def test_an_extra_lane_on_the_new_side_is_also_a_disagreement(self) -> None:
        observation = PAR.compare(
            gate_receipt([lane("lint")]),
            sbp_receipt({"lint": PASSED, "extra": PASSED}, green=True),
        )
        self.assertEqual(("extra",), observation.disagreements)

    def test_an_overall_verdict_mismatch_breaks_parity(self) -> None:
        observation = PAR.compare(
            gate_receipt([lane("lint")], status="fail"),
            sbp_receipt({"lint": PASSED}, green=True),
        )
        self.assertFalse(observation.verdict_agrees)
        self.assertFalse(observation.agrees)

    def test_a_non_canonical_run_never_counts(self) -> None:
        observation = PAR.compare(
            gate_receipt([lane("lint")], canonical=False),
            sbp_receipt({"lint": PASSED}, green=True),
        )
        self.assertFalse(observation.counts)
        self.assertFalse(observation.agrees, "a lane subset is not evidence")


class LedgerTests(ParityTestCase):
    """self-test.sh keeps authority until five canonical runs agree."""

    def agreeing(self, canonical: bool = True) -> PAR.ParityObservation:
        return PAR.compare(
            gate_receipt([lane("lint")], canonical=canonical),
            sbp_receipt({"lint": PASSED}, green=True),
        )

    def disagreeing(self) -> PAR.ParityObservation:
        return PAR.compare(
            gate_receipt([lane("lint")], status="fail"),
            sbp_receipt({"lint": PASSED}, green=True),
        )

    def test_authority_stays_with_the_gate_until_five(self) -> None:
        ledger = PAR.ParityLedger()
        for expected in range(1, PAR.MIN_CONSECUTIVE_AGREEMENTS):
            ledger.record(self.agreeing())
            self.assertEqual(expected, ledger.consecutive_agreements())
            self.assertFalse(ledger.may_switch())
            self.assertEqual(PAR.AUTHORITY_SELF_TEST, ledger.authority())
        ledger.record(self.agreeing())
        self.assertEqual(5, ledger.consecutive_agreements())
        self.assertTrue(ledger.may_switch())
        self.assertEqual(PAR.AUTHORITY_SBP_TEST, ledger.authority())

    def test_one_disagreement_resets_the_streak_to_zero(self) -> None:
        # Streaks do not partially survive: four agreements and a disagreement
        # is worth nothing, not four fifths of a migration.
        ledger = PAR.ParityLedger()
        for _ in range(4):
            ledger.record(self.agreeing())
        ledger.record(self.disagreeing())
        self.assertEqual(0, ledger.consecutive_agreements())
        self.assertEqual(PAR.AUTHORITY_SELF_TEST, ledger.authority())

    def test_a_non_canonical_run_neither_extends_nor_breaks_a_streak(self) -> None:
        ledger = PAR.ParityLedger()
        ledger.record(self.agreeing())
        ledger.record(self.agreeing(canonical=False))
        ledger.record(self.agreeing())
        self.assertEqual(2, ledger.consecutive_agreements())

    def test_an_empty_ledger_never_switches(self) -> None:
        ledger = PAR.ParityLedger()
        self.assertEqual(0, ledger.consecutive_agreements())
        self.assertEqual(PAR.AUTHORITY_SELF_TEST, ledger.authority())

    def test_the_threshold_may_be_raised_but_never_lowered(self) -> None:
        self.assertEqual(5, PAR.require_threshold(5))
        self.assertEqual(9, PAR.require_threshold(9))
        for value in (4, 0, -1, True, "5", 5.0):
            self.assert_refused(
                "threshold_too_low", lambda value=value: PAR.require_threshold(value)
            )

    def test_a_raised_threshold_governs_the_switch(self) -> None:
        ledger = PAR.ParityLedger(threshold=7)
        for _ in range(6):
            ledger.record(self.agreeing())
        self.assertFalse(ledger.may_switch())
        ledger.record(self.agreeing())
        self.assertTrue(ledger.may_switch())

    def test_a_lowered_threshold_is_refused_at_construction(self) -> None:
        self.assert_refused("threshold_too_low", lambda: PAR.ParityLedger(threshold=1))

    def test_the_payload_reports_what_remains(self) -> None:
        ledger = PAR.ParityLedger()
        ledger.record(self.agreeing())
        payload = ledger.to_payload()
        self.assertEqual(1, payload["consecutive_agreements"])
        self.assertEqual(4, payload["remaining"])
        self.assertEqual(PAR.AUTHORITY_SELF_TEST, payload["authority"])
        json.dumps(payload)


class TimingTelemetryTests(ParityTestCase):
    """Serial gate vs wave-concurrent executor — the evidence P4 needs."""

    def test_serial_and_wall_clock_are_reported_separately(self) -> None:
        report = PAR.timing_report(
            gate_receipt([lane("a", duration_s=10), lane("b", duration_s=20)]),
            {"a": 10.0, "b": 20.0},
            sbp_wall_clock_s=21.0,
        )
        self.assertEqual(30.0, report["self_test_serial_total_s"])
        self.assertEqual(21.0, report["sbp_test_wall_clock_s"])
        self.assertAlmostEqual(30 / 21, report["speedup"], places=3)

    def test_per_lane_durations_come_from_both_sides(self) -> None:
        report = PAR.timing_report(
            gate_receipt([lane("a", duration_s=10)]), {"a": 4.0}, sbp_wall_clock_s=4.0
        )
        row = report["lanes"][0]
        self.assertEqual(10.0, row["self_test_s"])
        self.assertEqual(4.0, row["sbp_test_s"])

    def test_a_lane_measured_on_only_one_side_reports_none(self) -> None:
        report = PAR.timing_report(
            gate_receipt([lane("a", duration_s=10)]), {"b": 1.0}, sbp_wall_clock_s=1.0
        )
        rows = {row["unit_id"]: row for row in report["lanes"]}
        self.assertIsNone(rows["a"]["sbp_test_s"])
        self.assertIsNone(rows["b"]["self_test_s"])

    def test_a_zero_wall_clock_reports_no_speedup_rather_than_dividing(self) -> None:
        report = PAR.timing_report(gate_receipt([]), {}, sbp_wall_clock_s=0.0)
        self.assertIsNone(report["speedup"])


class ContractTests(ParityTestCase):
    """Invariants that keep the migration honest."""

    def test_every_refusal_code_in_the_source_is_declared(self) -> None:
        source = PARITY_SOURCE.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\(\s*"([a-z_]+)"', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - PAR.REFUSAL_CODES)

    def test_the_module_runs_nothing(self) -> None:
        source = PARITY_SOURCE.read_text(encoding="utf-8")
        for banned in ("subprocess", "socket", "os.system", "popen"):
            self.assertNotIn(banned, source, banned)

    def test_the_threshold_constant_is_five(self) -> None:
        self.assertEqual(5, PAR.MIN_CONSECUTIVE_AGREEMENTS)

    def test_the_manifest_documents_that_the_gate_stays_authoritative(self) -> None:
        text = (ROOT_DIR / ".skillbox" / "test.yaml").read_text(encoding="utf-8")
        self.assertIn("self-test.sh", text)
        self.assertIn("FIVE", text)


if __name__ == "__main__":
    unittest.main()
