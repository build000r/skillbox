"""Phase-level latency diagnosis for the Oracle warm-submit SLO.

The 2026-08-06 benchmark is the spec, and its two headline numbers are encoded
as fixtures: warm browser-to-submit p95 9614.340436ms against a 4000ms target,
with a minimum of 4206.743224ms. The minimum is the interesting one — it means
every observed run missed — and the first test class exists to prove the
instrument says so rather than reporting a bare p95 an operator has to
interpret.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import oracle_metrics as METRICS  # noqa: E402
from runtime_manager.oracle_latency import (  # noqa: E402
    COLD_CLI_TO_SUBMIT,
    NOTE_NO_BUDGETS,
    NOTE_NO_SAMPLES,
    NOTE_PHASES_MISSING,
    NOTE_PHASES_PARTIAL,
    ORACLE_LATENCY_DIAGNOSIS_SCHEMA,
    P95_PPM,
    REFUSAL_CODES,
    SHAPE_FLOOR_EXCEEDED,
    SHAPE_MEDIAN_EXCEEDED,
    SHAPE_TAIL_ONLY,
    SHAPE_WITHIN_SLO,
    SPAN_BROWSER_TO_SUBMIT,
    VERDICT_FAIL,
    VERDICT_PASS,
    WARM_BROWSER_TO_SUBMIT,
    OracleLatencyError,
    SpanContract,
    SpanObservation,
    assert_diagnosis_safe,
    diagnose_span,
    nearest_rank,
    observations_from_samples,
)

LATENCY_SOURCE = ENV_MANAGER_DIR / "runtime_manager" / "oracle_latency.py"

#: Straight from the bead's live measurement.
REPORTED_WARM_P95_MS = 9614.340436
REPORTED_WARM_MIN_MS = 4206.743224
REPORTED_WARM_COUNT = 20
REPORTED_COLD_P95_MS = 7273.8893

PHASES = WARM_BROWSER_TO_SUBMIT.phases


def reported_warm_spans() -> list[float]:
    """20 warm spans whose min and nearest-rank p95 are the reported values.

    Only the two reported order statistics are pinned; the interior values are
    filler between them, which is all the benchmark artifact tells us.
    """
    spans = [REPORTED_WARM_MIN_MS]
    spans += [5000.0 + index * 100.0 for index in range(17)]  # indices 1..17
    spans.append(REPORTED_WARM_P95_MS)  # index 18 — the nearest-rank p95 of 20
    spans.append(12000.0)  # index 19 — the tail
    return sorted(spans)


def warm(span_ms: float, **overrides: object) -> SpanObservation:
    values: dict[str, object] = {"span_ms": span_ms, "warm": True}
    values.update(overrides)
    return SpanObservation(**values)  # type: ignore[arg-type]


class ReportedRegressionTests(unittest.TestCase):
    """The instrument must reproduce the reported verdict, exactly."""

    def diagnosis(self):
        return diagnose_span(
            [warm(value) for value in reported_warm_spans()], WARM_BROWSER_TO_SUBMIT
        )

    def test_the_reported_percentiles_are_reproduced(self) -> None:
        verdict = self.diagnosis()
        self.assertEqual(REPORTED_WARM_COUNT, verdict.count)
        self.assertEqual(REPORTED_WARM_P95_MS, verdict.p95_ms)
        self.assertEqual(REPORTED_WARM_MIN_MS, verdict.min_ms)
        self.assertEqual(4000.0, verdict.target_ms)

    def test_the_slo_is_reported_as_missed(self) -> None:
        verdict = self.diagnosis()
        self.assertEqual(VERDICT_FAIL, verdict.verdict)
        self.assertFalse(verdict.passed)
        self.assertAlmostEqual(5614.340436, verdict.excess_at_p95_ms, places=6)

    def test_the_miss_is_diagnosed_as_a_floor_not_a_tail(self) -> None:
        # The whole point: min > target means every run missed, so the cost is
        # unconditional. A tail diagnosis would send tuning after contention
        # that is not there.
        verdict = self.diagnosis()
        self.assertEqual(SHAPE_FLOOR_EXCEEDED, verdict.shape)
        self.assertTrue(verdict.every_run_missed)

    def test_missing_phase_instrumentation_is_stated_not_guessed(self) -> None:
        verdict = self.diagnosis()
        self.assertIn(NOTE_PHASES_MISSING, verdict.notes)
        self.assertEqual((), verdict.phases)

    def test_the_cold_span_still_passes(self) -> None:
        spans = [3000.0] * 18 + [REPORTED_COLD_P95_MS, 8000.0]
        verdict = diagnose_span(
            [SpanObservation(span_ms=value, warm=False) for value in spans],
            COLD_CLI_TO_SUBMIT,
        )
        self.assertEqual(REPORTED_COLD_P95_MS, verdict.p95_ms)
        self.assertEqual(VERDICT_PASS, verdict.verdict)
        self.assertEqual(SHAPE_WITHIN_SLO, verdict.shape)
        self.assertEqual(0.0, verdict.excess_at_p95_ms)


class PercentileTests(unittest.TestCase):
    """Nearest-rank, and the same nearest-rank the benchmark used."""

    def test_p95_of_twenty_is_the_nineteenth_smallest(self) -> None:
        values = [float(index) for index in range(1, 21)]
        self.assertEqual(19.0, nearest_rank(values, P95_PPM))

    def test_the_result_is_always_a_real_observation(self) -> None:
        values = [1.0, 2.0, 100.0]
        self.assertIn(nearest_rank(values, P95_PPM), values)

    def test_it_agrees_with_the_metrics_module_on_integers(self) -> None:
        # A drift guard across the two modules: a rerun of the host benchmark
        # and this diagnosis must never disagree about which run p95 names.
        for size in (1, 2, 5, 19, 20, 21, 100):
            values = list(range(1, size + 1))
            self.assertEqual(
                float(METRICS._percentile(sorted(values), P95_PPM)),
                nearest_rank([float(value) for value in values], P95_PPM),
                size,
            )

    def test_a_single_sample_is_its_own_p95(self) -> None:
        self.assertEqual(7.5, nearest_rank([7.5], P95_PPM))

    def test_an_empty_series_refuses(self) -> None:
        with self.assertRaises(OracleLatencyError) as caught:
            nearest_rank([], P95_PPM)
        self.assertEqual("sample_invalid", caught.exception.code)


class ShapeTests(unittest.TestCase):
    """Four shapes, because they call for different work."""

    def diagnose(self, spans: list[float]):
        return diagnose_span([warm(value) for value in spans], WARM_BROWSER_TO_SUBMIT)

    def test_within_slo(self) -> None:
        self.assertEqual(SHAPE_WITHIN_SLO, self.diagnose([100.0] * 20).shape)

    def test_floor_exceeded_when_even_the_fastest_run_misses(self) -> None:
        self.assertEqual(SHAPE_FLOOR_EXCEEDED, self.diagnose([4500.0] * 20).shape)

    def test_median_exceeded_when_a_fast_case_still_exists(self) -> None:
        spans = [1000.0] + [4500.0] * 19
        verdict = self.diagnose(spans)
        self.assertEqual(SHAPE_MEDIAN_EXCEEDED, verdict.shape)
        self.assertFalse(verdict.every_run_missed)

    def test_tail_only_when_the_median_is_healthy(self) -> None:
        spans = [1000.0] * 18 + [9000.0, 9500.0]
        verdict = self.diagnose(spans)
        self.assertEqual(SHAPE_TAIL_ONLY, verdict.shape)
        self.assertEqual(VERDICT_FAIL, verdict.verdict)

    def test_the_boundary_is_inclusive(self) -> None:
        self.assertEqual(SHAPE_WITHIN_SLO, self.diagnose([4000.0] * 20).shape)
        self.assertEqual(SHAPE_FLOOR_EXCEEDED, self.diagnose([4000.1] * 20).shape)


class AttributionTests(unittest.TestCase):
    """Where the time went, ranked, so tuning starts in the right place."""

    def instrumented(self, browser: float, stage: float, submit: float, count: int = 20):
        phase_ms = dict(zip(PHASES, (browser, stage, submit)))
        return [
            warm(browser + stage + submit, phase_ms=phase_ms) for _ in range(count)
        ]

    def test_phases_are_ranked_by_p95(self) -> None:
        verdict = diagnose_span(
            self.instrumented(3800.0, 120.0, 300.0), WARM_BROWSER_TO_SUBMIT
        )
        self.assertEqual(
            ["browser_acquire", "submit", "attachment_stage"],
            [finding.phase for finding in verdict.phases],
        )
        self.assertEqual(3800.0, verdict.phases[0].p95_ms)

    def test_share_points_at_the_dominant_phase(self) -> None:
        verdict = diagnose_span(
            self.instrumented(3800.0, 100.0, 100.0), WARM_BROWSER_TO_SUBMIT
        )
        dominant = verdict.phases[0]
        self.assertEqual("browser_acquire", dominant.phase)
        self.assertGreater(dominant.share_ppm, 900_000)

    def test_budgets_are_optional_and_their_absence_is_stated(self) -> None:
        verdict = diagnose_span(
            self.instrumented(3800.0, 100.0, 100.0), WARM_BROWSER_TO_SUBMIT
        )
        self.assertIn(NOTE_NO_BUDGETS, verdict.notes)
        for finding in verdict.phases:
            self.assertIsNone(finding.budget_ms)
            self.assertIsNone(finding.overrun_ms)

    def test_declared_budgets_produce_overruns(self) -> None:
        contract = SpanContract(
            name=SPAN_BROWSER_TO_SUBMIT,
            phases=PHASES,
            target_ms=4000.0,
            warm=True,
            phase_budgets_ms={"browser_acquire": 500.0, "submit": 200.0},
        )
        verdict = diagnose_span(self.instrumented(3800.0, 100.0, 100.0), contract)
        by_phase = {finding.phase: finding for finding in verdict.phases}
        self.assertEqual(3300.0, by_phase["browser_acquire"].overrun_ms)
        self.assertEqual(0.0, by_phase["submit"].overrun_ms)
        self.assertIsNone(by_phase["attachment_stage"].overrun_ms)
        self.assertNotIn(NOTE_NO_BUDGETS, verdict.notes)

    def test_partial_instrumentation_is_flagged(self) -> None:
        rows = self.instrumented(1000.0, 100.0, 100.0, count=10)
        rows += [warm(1200.0) for _ in range(10)]
        verdict = diagnose_span(rows, WARM_BROWSER_TO_SUBMIT)
        self.assertIn(NOTE_PHASES_PARTIAL, verdict.notes)
        self.assertEqual(10, verdict.phases[0].count)


class SelectionTests(unittest.TestCase):
    """The SLO is stated for warm, successful runs."""

    def test_cold_runs_do_not_dilute_a_warm_span(self) -> None:
        rows = [warm(5000.0) for _ in range(10)]
        rows += [SpanObservation(span_ms=10.0, warm=False) for _ in range(10)]
        verdict = diagnose_span(rows, WARM_BROWSER_TO_SUBMIT)
        self.assertEqual(10, verdict.count)
        self.assertEqual(5000.0, verdict.p95_ms)

    def test_failed_runs_are_excluded(self) -> None:
        # A run that aborted early would flatter the numbers.
        rows = [warm(5000.0) for _ in range(10)]
        rows += [warm(10.0, state=METRICS.STATE_FAILED) for _ in range(10)]
        verdict = diagnose_span(rows, WARM_BROWSER_TO_SUBMIT)
        self.assertEqual(10, verdict.count)
        self.assertEqual(5000.0, verdict.min_ms)

    def test_no_matching_samples_fails_rather_than_passing_vacuously(self) -> None:
        verdict = diagnose_span(
            [SpanObservation(span_ms=10.0, warm=False)], WARM_BROWSER_TO_SUBMIT
        )
        self.assertEqual(0, verdict.count)
        self.assertEqual(VERDICT_FAIL, verdict.verdict)
        self.assertIn(NOTE_NO_SAMPLES, verdict.notes)
        self.assertFalse(verdict.passed)


class SampleProjectionTests(unittest.TestCase):
    """Metrics samples project onto a span without either module knowing."""

    class FakeSample:
        def __init__(self, durations, warm=True, state=METRICS.STATE_COMPLETED):
            self.durations_ms = durations
            self.warm = warm
            self.state = state

    def test_a_metrics_sample_projects_onto_the_span(self) -> None:
        sample = self.FakeSample(
            {"browser_acquire": 3000, "attachment_stage": 100, "submit": 200, "total": 9000}
        )
        projected = observations_from_samples([sample], WARM_BROWSER_TO_SUBMIT)
        self.assertEqual(1, len(projected))
        self.assertEqual(3300.0, projected[0].span_ms)
        self.assertTrue(projected[0].warm)

    def test_a_partial_breakdown_refuses_rather_than_undercounting(self) -> None:
        sample = self.FakeSample({"browser_acquire": 3000})
        with self.assertRaises(OracleLatencyError) as caught:
            observations_from_samples([sample], WARM_BROWSER_TO_SUBMIT)
        self.assertEqual("sample_invalid", caught.exception.code)

    def test_a_sample_without_durations_refuses(self) -> None:
        with self.assertRaises(OracleLatencyError):
            observations_from_samples([object()], WARM_BROWSER_TO_SUBMIT)


class ContractValidationTests(unittest.TestCase):
    """A contract that cannot be met must not be constructible."""

    def base(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "name": SPAN_BROWSER_TO_SUBMIT,
            "phases": PHASES,
            "target_ms": 4000.0,
            "warm": True,
        }
        values.update(overrides)
        return values

    def assert_refused(self, **overrides: object) -> None:
        with self.assertRaises(OracleLatencyError) as caught:
            SpanContract(**self.base(**overrides))  # type: ignore[arg-type]
        self.assertEqual("contract_invalid", caught.exception.code)

    def test_span_name_and_phases_are_from_the_shared_vocabulary(self) -> None:
        self.assert_refused(name="made_up_span")
        self.assert_refused(phases=("not_a_phase",))
        self.assert_refused(phases=())
        self.assert_refused(phases=("submit", "submit"))

    def test_targets_must_be_positive_and_finite(self) -> None:
        for target in (0.0, -1.0, float("inf"), float("nan"), True, "4000"):
            self.assert_refused(target_ms=target)

    def test_budgets_must_fit_inside_the_target(self) -> None:
        # Budgets summing past the target would let every run look compliant
        # against a target it can never meet.
        self.assert_refused(
            phase_budgets_ms={"browser_acquire": 3000.0, "submit": 2000.0}
        )

    def test_budgets_must_name_a_phase_in_the_span(self) -> None:
        self.assert_refused(phase_budgets_ms={"generation": 100.0})

    def test_the_shipped_contracts_match_the_benchmark(self) -> None:
        self.assertEqual(4000.0, WARM_BROWSER_TO_SUBMIT.target_ms)
        self.assertTrue(WARM_BROWSER_TO_SUBMIT.warm)
        self.assertEqual(12000.0, COLD_CLI_TO_SUBMIT.target_ms)
        self.assertFalse(COLD_CLI_TO_SUBMIT.warm)
        self.assertTrue(set(WARM_BROWSER_TO_SUBMIT.phases) <= set(COLD_CLI_TO_SUBMIT.phases))


class EmissionSafetyTests(unittest.TestCase):
    """Diagnosing a credential-bearing path must not become a way to log one."""

    def test_the_diagnosis_payload_is_emission_safe(self) -> None:
        verdict = diagnose_span(
            [
                warm(3300.0, phase_ms=dict(zip(PHASES, (3000.0, 100.0, 200.0))))
                for _ in range(20)
            ],
            WARM_BROWSER_TO_SUBMIT,
        )
        payload = verdict.to_payload()
        self.assertEqual(ORACLE_LATENCY_DIAGNOSIS_SCHEMA, payload["schema"])
        assert_diagnosis_safe(payload)

    def test_free_text_cannot_be_emitted(self) -> None:
        for leak in (
            {"span": "https://chatgpt.com/c/abc"},
            {"notes": ["/Users/b/.config/secret"]},
            {"prompt": "why is my p99 bimodal"},
            {"phases": [{"phase": "sk-live-deadbeef"}]},
        ):
            with self.assertRaises(OracleLatencyError) as caught:
                assert_diagnosis_safe(leak)
            self.assertEqual("diagnosis_unsafe", caught.exception.code)

    def test_non_finite_numbers_are_refused(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(OracleLatencyError):
                assert_diagnosis_safe({"p95_ms": value})

    def test_a_bool_outside_a_bool_position_is_refused(self) -> None:
        assert_diagnosis_safe({"passed": True})
        with self.assertRaises(OracleLatencyError):
            assert_diagnosis_safe({"count": True})

    def test_a_deep_document_is_refused(self) -> None:
        document: object = "within_slo"
        for _ in range(12):
            document = {"phases": [document]}
        with self.assertRaises(OracleLatencyError):
            assert_diagnosis_safe(document)


class ContractTests(unittest.TestCase):
    """Invariants, including that this module diagnoses and does not tune."""

    def test_every_refusal_code_in_the_source_is_declared(self) -> None:
        source = LATENCY_SOURCE.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\("([a-z_]+)"\)', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - REFUSAL_CODES)

    def test_the_module_cannot_touch_the_path_it_measures(self) -> None:
        # The bead scopes tuning to a separately authorized change; a diagnosis
        # that edits the path it measures is not a diagnosis.
        source = LATENCY_SOURCE.read_text(encoding="utf-8")
        for banned in (
            "time.sleep",
            "subprocess",
            "socket",
            "urllib",
            "os.environ",
            "open(",
        ):
            self.assertNotIn(banned, source, banned)

    def test_span_phases_come_from_the_metrics_vocabulary(self) -> None:
        for contract in (WARM_BROWSER_TO_SUBMIT, COLD_CLI_TO_SUBMIT):
            for phase in contract.phases:
                self.assertIn(phase, METRICS.LATENCY_PHASES, phase)


if __name__ == "__main__":
    unittest.main()
