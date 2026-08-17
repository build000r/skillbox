"""Contract tests for ``runtime_manager.oracle_metrics``.

Run the way the brief specifies::

    PYTHONPATH=.env-manager python3 -m unittest tests.test_oracle_metrics

The load-bearing family here is :class:`RedactionTests`.  The metrics contract
claims a leak is structurally impossible, not merely scrubbed, so those tests
attack it from both ends: every input path is fed prompt text, private URLs,
account identity, and credentials and must fail closed; and every emitted
document is walked so that any string outside the declared vocabulary — plus an
opaque run ID, a SHA-256 digest, and a rendered timestamp — is a failure.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.oracle_metrics import (
    DEFAULT_WINDOW_CAPACITY,
    ERROR_CLASSES,
    ERROR_NONE,
    LATENCY_PHASES,
    MAX_ATTEMPTS,
    MAX_DURATION_MS,
    MAX_EPOCH_MS,
    MAX_INFLIGHT,
    MAX_QUEUE_DEPTH,
    MIN_EPOCH_MS,
    ORACLE_METRICS_SAMPLE_SCHEMA,
    ORACLE_METRICS_SNAPSHOT_SCHEMA,
    PHASE_BROWSER_ACQUIRE,
    PHASE_GENERATION,
    PHASE_QUEUE_WAIT,
    PHASE_SUBMIT,
    PHASE_TOTAL,
    RUN_STAGES,
    RUN_STATES,
    STAGE_ADMITTED,
    STAGE_DELIVERED,
    STAGE_GENERATING,
    STAGE_QUEUED,
    STAGE_SUBMITTED,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_DENIED,
    STATE_FAILED,
    STATE_TIMED_OUT,
    SUPPORTED_MODES,
    OracleMetricsError,
    OracleMetricsRegistry,
    OracleRunSample,
    PhaseTimer,
    assert_emission_safe,
    canonical_json_bytes,
    iso_millis,
    new_run_id,
)


NOW_MS = 1_800_000_000_000  # 2027-01-15T08:00:00.000Z
DIGEST = "a" * 64

# Everything the Oracle lane touches that must never reach a metrics document.
SENSITIVE_LITERALS = (
    "summarize the acquisition memo for Q3",
    "https://chatgpt.com/c/6f2a1b90-private-thread",
    "https://oracle.internal.example/deep-research/tenant-42",
    "operator@example.com",
    "rob.baratta",
    "/Users/b/.oracle/profile/Default/Cookies",
    "sk-live-9f8e7d6c5b4a",
    "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "__Secure-next-auth.session-token=abc123",
    "tskey-auth-k1234567CNTRL-abcdef",
)


def sample(**overrides: object) -> OracleRunSample:
    values: dict[str, object] = {
        "run_id": "0" * 32,
        "mode": "standard",
        "state": STATE_COMPLETED,
        "stage_reached": STAGE_DELIVERED,
        "error_class": ERROR_NONE,
        "warm": True,
        "attempts": 1,
        "queue_depth": 0,
        "inflight": 1,
        "result_bytes": 4096,
        "result_digest": DIGEST,
        "observed_at_ms": NOW_MS,
        "durations_ms": {PHASE_QUEUE_WAIT: 10, PHASE_SUBMIT: 500, PHASE_TOTAL: 9000},
    }
    values.update(overrides)
    return OracleRunSample(**values)  # type: ignore[arg-type]


def failed_sample(**overrides: object) -> OracleRunSample:
    values: dict[str, object] = {
        "state": STATE_FAILED,
        "stage_reached": STAGE_SUBMITTED,
        "error_class": "browser_crashed",
        "result_bytes": 0,
        "result_digest": None,
    }
    values.update(overrides)
    return sample(**values)


def walk_strings(document: object, path: str = "$") -> list[tuple[str, str]]:
    """Every string in the document, paired with where it was found."""
    found: list[tuple[str, str]] = []
    if isinstance(document, str):
        found.append((path, document))
    elif isinstance(document, dict):
        for key, value in document.items():
            found.append((f"{path}.<key>", key))
            found.extend(walk_strings(value, f"{path}.{key}"))
    elif isinstance(document, list):
        for index, item in enumerate(document):
            found.extend(walk_strings(item, f"{path}[{index}]"))
    return found


class VocabularyTests(unittest.TestCase):
    """The closed vocabulary has to actually be closed."""

    def test_states_stages_and_error_classes_are_unique(self) -> None:
        for names in (RUN_STATES, RUN_STAGES, ERROR_CLASSES, LATENCY_PHASES):
            with self.subTest(names=names):
                self.assertEqual(len(names), len(set(names)))

    def test_every_state_accepts_at_least_one_error_class(self) -> None:
        for state in RUN_STATES:
            with self.subTest(state=state):
                accepted = [
                    name
                    for name in ERROR_CLASSES
                    if self._accepts(state, name)
                ]
                self.assertTrue(accepted)

    def test_error_classes_partition_across_states(self) -> None:
        """No error class is legal under two different terminal states."""
        for name in ERROR_CLASSES:
            with self.subTest(error_class=name):
                states = [s for s in RUN_STATES if self._accepts(s, name)]
                self.assertEqual(len(states), 1, states)

    def test_only_completed_may_carry_the_none_error_class(self) -> None:
        self.assertTrue(self._accepts(STATE_COMPLETED, ERROR_NONE))
        for state in RUN_STATES:
            if state == STATE_COMPLETED:
                continue
            with self.subTest(state=state):
                self.assertFalse(self._accepts(state, ERROR_NONE))

    def _accepts(self, state: str, error_class: str) -> bool:
        stages = {
            STATE_COMPLETED: STAGE_DELIVERED,
            STATE_DENIED: STAGE_QUEUED,
            STATE_TIMED_OUT: STAGE_SUBMITTED,
        }
        completed = state == STATE_COMPLETED
        try:
            sample(
                state=state,
                stage_reached=stages.get(state, STAGE_ADMITTED),
                error_class=error_class,
                result_bytes=4096 if completed else 0,
                result_digest=DIGEST if completed else None,
            )
        except OracleMetricsError:
            return False
        return True


class SampleShapeTests(unittest.TestCase):
    def test_a_well_formed_sample_is_accepted(self) -> None:
        record = sample()
        self.assertEqual(record.state, STATE_COMPLETED)
        self.assertEqual(record.durations_ms[PHASE_TOTAL], 9000)

    def test_durations_are_frozen_against_post_construction_mutation(self) -> None:
        record = sample()
        with self.assertRaises(TypeError):
            record.durations_ms[PHASE_TOTAL] = 1  # type: ignore[index]

    def test_unknown_mode_state_stage_or_error_class_fails_closed(self) -> None:
        cases = (
            {"mode": "turbo"},
            {"state": "running"},
            {"stage_reached": "warming"},
            {"error_class": "kaboom"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(OracleMetricsError):
                    sample(**overrides)

    def test_state_and_error_class_must_agree(self) -> None:
        with self.assertRaises(OracleMetricsError):
            sample(state=STATE_COMPLETED, error_class="browser_crashed")
        with self.assertRaises(OracleMetricsError):
            failed_sample(error_class=ERROR_NONE)
        with self.assertRaises(OracleMetricsError):
            failed_sample(state=STATE_DENIED, error_class="browser_crashed")

    def test_state_and_stage_must_agree(self) -> None:
        with self.assertRaises(OracleMetricsError):
            sample(stage_reached=STAGE_SUBMITTED)
        with self.assertRaises(OracleMetricsError):
            failed_sample(
                state=STATE_DENIED,
                error_class="policy_denied",
                stage_reached=STAGE_GENERATING,
            )
        with self.assertRaises(OracleMetricsError):
            failed_sample(
                state=STATE_CANCELLED,
                error_class="client_cancelled",
                stage_reached=STAGE_DELIVERED,
            )

    def test_bools_are_not_accepted_where_integers_are_required(self) -> None:
        for field_name in ("attempts", "queue_depth", "inflight", "result_bytes"):
            with self.subTest(field=field_name):
                with self.assertRaises(OracleMetricsError):
                    sample(**{field_name: True})

    def test_warm_must_be_a_real_bool(self) -> None:
        for value in (1, 0, "true", None):
            with self.subTest(value=value):
                with self.assertRaises(OracleMetricsError):
                    sample(warm=value)

    def test_integer_bounds_fail_closed(self) -> None:
        cases = (
            {"attempts": 0},
            {"attempts": MAX_ATTEMPTS + 1},
            {"queue_depth": -1},
            {"queue_depth": MAX_QUEUE_DEPTH + 1},
            {"inflight": MAX_INFLIGHT + 1},
            {"observed_at_ms": MIN_EPOCH_MS - 1},
            {"observed_at_ms": MAX_EPOCH_MS + 1},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(OracleMetricsError):
                    sample(**overrides)

    def test_run_id_must_be_opaque_lowercase_hex(self) -> None:
        for value in ("", "0" * 31, "0" * 33, "Z" * 32, "A" * 32, 0, None):
            with self.subTest(value=value):
                with self.assertRaises(OracleMetricsError):
                    sample(run_id=value)

    def test_durations_reject_unknown_phases_and_missing_total(self) -> None:
        with self.assertRaises(OracleMetricsError):
            sample(durations_ms={PHASE_TOTAL: 1, "warmup": 2})
        with self.assertRaises(OracleMetricsError):
            sample(durations_ms={PHASE_SUBMIT: 2})
        with self.assertRaises(OracleMetricsError):
            sample(durations_ms={})

    def test_no_phase_may_exceed_the_total(self) -> None:
        with self.assertRaises(OracleMetricsError):
            sample(durations_ms={PHASE_SUBMIT: 10, PHASE_TOTAL: 9})
        sample(durations_ms={PHASE_SUBMIT: 9, PHASE_TOTAL: 9})

    def test_duration_bounds_fail_closed(self) -> None:
        with self.assertRaises(OracleMetricsError):
            sample(durations_ms={PHASE_TOTAL: -1})
        with self.assertRaises(OracleMetricsError):
            sample(durations_ms={PHASE_TOTAL: MAX_DURATION_MS + 1})

    def test_from_mapping_requires_the_exact_key_set_and_schema(self) -> None:
        document = sample().as_document()
        self.assertEqual(OracleRunSample.from_mapping(document), sample())
        with self.assertRaises(OracleMetricsError):
            OracleRunSample.from_mapping({**document, "extra": 1})
        short = dict(document)
        del short["mode"]
        with self.assertRaises(OracleMetricsError):
            OracleRunSample.from_mapping(short)
        with self.assertRaises(OracleMetricsError):
            OracleRunSample.from_mapping({**document, "schema": "other.v1"})


class RunBoundEvidenceTests(unittest.TestCase):
    """The brief's risk gate: no completed receipt without real evidence."""

    def test_completed_requires_a_nonempty_result_and_its_digest(self) -> None:
        with self.assertRaises(OracleMetricsError):
            sample(result_bytes=0)
        with self.assertRaises(OracleMetricsError):
            sample(result_digest=None)
        with self.assertRaises(OracleMetricsError):
            sample(result_digest="deadbeef")
        with self.assertRaises(OracleMetricsError):
            sample(result_digest="A" * 64)

    def test_a_completed_run_must_have_reached_delivered(self) -> None:
        for stage in RUN_STAGES:
            if stage == STAGE_DELIVERED:
                continue
            with self.subTest(stage=stage):
                with self.assertRaises(OracleMetricsError):
                    sample(stage_reached=stage)

    def test_unfinished_runs_may_not_claim_result_bytes_or_a_digest(self) -> None:
        with self.assertRaises(OracleMetricsError):
            failed_sample(result_bytes=10)
        with self.assertRaises(OracleMetricsError):
            failed_sample(result_digest=DIGEST)


class RedactionTests(unittest.TestCase):
    """Proof that no prompt, URL, identity, or secret can be emitted."""

    maxDiff = None

    def test_sensitive_values_are_rejected_by_every_string_field(self) -> None:
        for literal in SENSITIVE_LITERALS:
            for field_name in ("run_id", "mode", "state", "stage_reached",
                               "error_class", "result_digest"):
                with self.subTest(literal=literal, field=field_name):
                    with self.assertRaises(OracleMetricsError):
                        sample(**{field_name: literal})

    def test_sensitive_values_cannot_ride_in_as_a_duration_phase(self) -> None:
        for literal in SENSITIVE_LITERALS:
            with self.subTest(literal=literal):
                with self.assertRaises(OracleMetricsError):
                    sample(durations_ms={PHASE_TOTAL: 1, literal: 1})
                with self.assertRaises(OracleMetricsError):
                    sample(durations_ms={PHASE_TOTAL: literal})

    def test_the_sample_contract_has_no_free_text_field(self) -> None:
        """There is nowhere for a message, URL, path, or caller ID to live."""
        self.assertEqual(
            sorted(sample().as_document()),
            [
                "attempts",
                "durations_ms",
                "error_class",
                "inflight",
                "mode",
                "observed_at_ms",
                "queue_depth",
                "result_bytes",
                "result_digest",
                "run_id",
                "schema",
                "stage_reached",
                "state",
                "warm",
            ],
        )

    def test_no_emitted_string_falls_outside_the_declared_vocabulary(self) -> None:
        registry = OracleMetricsRegistry()
        registry.record(sample())
        registry.record(failed_sample(run_id="1" * 32))
        registry.observe_queue(
            queue_depth=3, inflight=2, capacity=4, observed_at_ms=NOW_MS
        )
        documents = (
            sample().as_document(),
            registry.snapshot(generated_at_ms=NOW_MS),
        )
        allowed = set(
            RUN_STATES
            + RUN_STAGES
            + ERROR_CLASSES
            + LATENCY_PHASES
            + SUPPORTED_MODES
            + (ORACLE_METRICS_SAMPLE_SCHEMA, ORACLE_METRICS_SNAPSHOT_SCHEMA)
        )
        for document in documents:
            for path, value in walk_strings(document):
                with self.subTest(path=path, value=value):
                    if value in allowed:
                        continue
                    if path.endswith("<key>"):
                        continue  # structural keys, pinned by the shape tests
                    self.assertRegex(
                        value,
                        r"^(?:[0-9a-f]{32}|[0-9a-f]{64}|"
                        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
                        r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z)$",
                    )

    def test_rendered_bytes_contain_none_of_the_sensitive_literals(self) -> None:
        registry = OracleMetricsRegistry()
        registry.record(sample())
        registry.record(failed_sample(run_id="1" * 32))
        registry.observe_queue(
            queue_depth=1, inflight=1, capacity=2, observed_at_ms=NOW_MS
        )
        payload = registry.render(generated_at_ms=NOW_MS).decode("ascii")
        for literal in SENSITIVE_LITERALS:
            with self.subTest(literal=literal):
                self.assertNotIn(literal, payload)
        for fragment in ("http", "@", "/Users/", "Bearer", "cookie", "token"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, payload)

    def test_assert_emission_safe_rejects_injected_keys_and_values(self) -> None:
        base = sample().as_document()
        for literal in SENSITIVE_LITERALS:
            with self.subTest(literal=literal):
                with self.assertRaises(OracleMetricsError):
                    assert_emission_safe({**base, literal: 1})
                with self.assertRaises(OracleMetricsError):
                    assert_emission_safe({**base, "mode": literal})
                with self.assertRaises(OracleMetricsError):
                    assert_emission_safe({**base, "state": [literal]})

    def test_assert_emission_safe_rejects_unsupported_types(self) -> None:
        for value in (1.5, b"bytes", object(), {1: 2}, {"warm": 1.0}):
            with self.subTest(value=value):
                with self.assertRaises(OracleMetricsError):
                    assert_emission_safe(value)

    def test_assert_emission_safe_allows_bools_only_in_bool_positions(self) -> None:
        assert_emission_safe({"warm": True})
        with self.assertRaises(OracleMetricsError):
            assert_emission_safe({"attempts": True})
        with self.assertRaises(OracleMetricsError):
            assert_emission_safe(True)

    def test_assert_emission_safe_bounds_nesting_depth(self) -> None:
        document: object = 1
        for _ in range(12):
            document = {"runs": document}
        with self.assertRaises(OracleMetricsError):
            assert_emission_safe(document)

    def test_a_mutated_frozen_sample_cannot_smuggle_text_into_a_registry(self) -> None:
        record = sample()
        object.__setattr__(record, "run_id", SENSITIVE_LITERALS[0])
        registry = OracleMetricsRegistry()
        with self.assertRaises(OracleMetricsError):
            registry.record(record)
        self.assertEqual(registry.sample_count, 0)

    def test_a_subclassed_sample_is_refused_outright(self) -> None:
        class Sneaky(OracleRunSample):
            pass

        registry = OracleMetricsRegistry()
        with self.assertRaises(OracleMetricsError):
            registry.record(Sneaky(**sample().__dict__))

    def test_canonical_json_bytes_refuses_an_unsafe_document(self) -> None:
        with self.assertRaises(OracleMetricsError):
            canonical_json_bytes({"mode": SENSITIVE_LITERALS[1]})


class PhaseTimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ticks: list[int] = []

    def clock(self) -> int:
        return self.ticks.pop(0)

    def test_durations_are_monotonic_milliseconds(self) -> None:
        self.ticks = [0, 1_500_000_000, 1_500_000_000, 4_000_000_000]
        timer = PhaseTimer(monotonic_ns=self.clock)
        timer.start(PHASE_QUEUE_WAIT)
        self.assertEqual(timer.stop(PHASE_QUEUE_WAIT), 1500)
        timer.start(PHASE_TOTAL)
        self.assertEqual(timer.stop(PHASE_TOTAL), 2500)

    def test_durations_are_ordered_by_the_declared_phase_order(self) -> None:
        self.ticks = [0, 1_000_000, 0, 2_000_000, 0, 3_000_000]
        timer = PhaseTimer(monotonic_ns=self.clock)
        for phase in (PHASE_TOTAL, PHASE_GENERATION, PHASE_QUEUE_WAIT):
            timer.start(phase)
            timer.stop(phase)
        self.assertEqual(
            list(timer.durations()),
            [PHASE_QUEUE_WAIT, PHASE_GENERATION, PHASE_TOTAL],
        )

    def test_unknown_phase_double_start_and_orphan_stop_fail_closed(self) -> None:
        self.ticks = [0]
        timer = PhaseTimer(monotonic_ns=self.clock)
        with self.assertRaises(OracleMetricsError):
            timer.start("warmup")
        with self.assertRaises(OracleMetricsError):
            timer.stop(PHASE_SUBMIT)
        timer.start(PHASE_SUBMIT)
        with self.assertRaises(OracleMetricsError):
            timer.start(PHASE_SUBMIT)

    def test_an_open_phase_makes_durations_fail_closed(self) -> None:
        self.ticks = [0]
        timer = PhaseTimer(monotonic_ns=self.clock)
        timer.start(PHASE_TOTAL)
        with self.assertRaises(OracleMetricsError):
            timer.durations()

    def test_a_backwards_clock_fails_closed(self) -> None:
        self.ticks = [5_000_000, 1_000_000]
        timer = PhaseTimer(monotonic_ns=self.clock)
        timer.start(PHASE_TOTAL)
        with self.assertRaises(OracleMetricsError):
            timer.stop(PHASE_TOTAL)

    def test_a_non_integer_clock_fails_closed(self) -> None:
        timer = PhaseTimer(monotonic_ns=lambda: 1.5)
        with self.assertRaises(OracleMetricsError):
            timer.start(PHASE_TOTAL)
        with self.assertRaises(OracleMetricsError):
            PhaseTimer(monotonic_ns="not callable")

    def test_timer_output_is_accepted_as_sample_durations(self) -> None:
        self.ticks = [0, 3_000_000, 0, 9_000_000]
        timer = PhaseTimer(monotonic_ns=self.clock)
        timer.start(PHASE_SUBMIT)
        timer.stop(PHASE_SUBMIT)
        timer.start(PHASE_TOTAL)
        timer.stop(PHASE_TOTAL)
        record = sample(durations_ms=timer.durations())
        self.assertEqual(record.durations_ms[PHASE_SUBMIT], 3)
        self.assertEqual(record.durations_ms[PHASE_TOTAL], 9)


class RegistryTests(unittest.TestCase):
    maxDiff = None

    def test_capacity_bounds_fail_closed(self) -> None:
        for value in (0, -1, True, "8"):
            with self.subTest(value=value):
                with self.assertRaises(OracleMetricsError):
                    OracleMetricsRegistry(capacity=value)  # type: ignore[arg-type]

    def test_the_window_is_a_bounded_ring_buffer(self) -> None:
        registry = OracleMetricsRegistry(capacity=3)
        self.assertEqual(registry.capacity, 3)
        for index in range(10):
            registry.record(sample(observed_at_ms=NOW_MS + index))
        self.assertEqual(registry.sample_count, 3)
        snapshot = registry.snapshot(generated_at_ms=NOW_MS + 100)
        self.assertEqual(snapshot["window"], {
            "samples": 3,
            "capacity": 3,
            "oldest_at": iso_millis(NOW_MS + 7),
            "newest_at": iso_millis(NOW_MS + 9),
        })

    def test_default_capacity_is_the_documented_one(self) -> None:
        self.assertEqual(OracleMetricsRegistry().capacity, DEFAULT_WINDOW_CAPACITY)

    def test_counts_and_reliability_are_derived_from_the_window(self) -> None:
        registry = OracleMetricsRegistry()
        for index in range(3):
            registry.record(sample(run_id=f"{index:032x}"))
        registry.record(failed_sample(run_id="a" * 32, attempts=3, warm=False))
        registry.record(
            failed_sample(
                run_id="b" * 32,
                mode="deep-research",
                state=STATE_TIMED_OUT,
                error_class="response_timeout",
                stage_reached=STAGE_GENERATING,
            )
        )
        snapshot = registry.snapshot(generated_at_ms=NOW_MS)
        self.assertEqual(snapshot["runs"]["total"], 5)
        self.assertEqual(snapshot["runs"]["warm"], 4)
        self.assertEqual(snapshot["runs"]["cold"], 1)
        self.assertEqual(snapshot["runs"]["by_state"][STATE_COMPLETED], 3)
        self.assertEqual(snapshot["runs"]["by_state"][STATE_FAILED], 1)
        self.assertEqual(snapshot["runs"]["by_state"][STATE_TIMED_OUT], 1)
        self.assertEqual(snapshot["runs"]["by_mode"], {"standard": 4, "deep-research": 1})
        self.assertEqual(snapshot["runs"]["by_stage_reached"][STAGE_DELIVERED], 3)
        reliability = snapshot["reliability"]
        self.assertEqual(reliability["success_rate_ppm"], 600_000)
        self.assertEqual(reliability["attempts_total"], 7)
        self.assertEqual(reliability["retried_runs"], 1)
        self.assertEqual(reliability["by_error_class"]["browser_crashed"], 1)
        self.assertEqual(reliability["by_error_class"]["response_timeout"], 1)
        self.assertEqual(reliability["by_error_class"][ERROR_NONE], 3)

    def test_every_enumerated_bucket_is_present_and_zero_filled(self) -> None:
        snapshot = OracleMetricsRegistry().snapshot(generated_at_ms=NOW_MS)
        self.assertEqual(set(snapshot["runs"]["by_state"]), set(RUN_STATES))
        self.assertEqual(set(snapshot["runs"]["by_stage_reached"]), set(RUN_STAGES))
        self.assertEqual(set(snapshot["runs"]["by_mode"]), set(SUPPORTED_MODES))
        self.assertEqual(
            set(snapshot["reliability"]["by_error_class"]), set(ERROR_CLASSES)
        )
        self.assertEqual(set(snapshot["latency_ms"]), set(LATENCY_PHASES))
        self.assertEqual(snapshot["reliability"]["success_rate_ppm"], 0)
        self.assertEqual(snapshot["window"]["oldest_at"], None)
        self.assertEqual(snapshot["latency_ms"][PHASE_TOTAL], {
            "count": 0,
            "min": None,
            "p50": None,
            "p95": None,
            "max": None,
        })

    def test_success_rate_is_floored_parts_per_million(self) -> None:
        registry = OracleMetricsRegistry()
        registry.record(sample())
        registry.record(failed_sample(run_id="1" * 32))
        registry.record(failed_sample(run_id="2" * 32))
        snapshot = registry.snapshot(generated_at_ms=NOW_MS)
        self.assertEqual(snapshot["reliability"]["success_rate_ppm"], 333_333)

    def test_percentiles_use_nearest_rank_over_the_observed_samples(self) -> None:
        registry = OracleMetricsRegistry()
        for index in range(1, 21):
            registry.record(
                sample(
                    run_id=f"{index:032x}",
                    durations_ms={PHASE_TOTAL: index * 100},
                )
            )
        bucket = registry.snapshot(generated_at_ms=NOW_MS)["latency_ms"][PHASE_TOTAL]
        self.assertEqual(bucket["count"], 20)
        self.assertEqual(bucket["min"], 100)
        self.assertEqual(bucket["p50"], 1000)
        self.assertEqual(bucket["p95"], 1900)
        self.assertEqual(bucket["max"], 2000)

    def test_a_phase_only_some_runs_report_is_counted_only_for_those_runs(self) -> None:
        registry = OracleMetricsRegistry()
        registry.record(sample())
        registry.record(
            sample(
                run_id="1" * 32,
                durations_ms={PHASE_BROWSER_ACQUIRE: 700, PHASE_TOTAL: 9000},
            )
        )
        latency = registry.snapshot(generated_at_ms=NOW_MS)["latency_ms"]
        self.assertEqual(latency[PHASE_BROWSER_ACQUIRE]["count"], 1)
        self.assertEqual(latency[PHASE_BROWSER_ACQUIRE]["p95"], 700)
        self.assertEqual(latency[PHASE_TOTAL]["count"], 2)

    def test_queue_gauges_track_the_latest_reading_and_a_high_watermark(self) -> None:
        registry = OracleMetricsRegistry()
        registry.observe_queue(
            queue_depth=7, inflight=2, capacity=4, observed_at_ms=NOW_MS
        )
        registry.observe_queue(
            queue_depth=1, inflight=1, capacity=4, observed_at_ms=NOW_MS + 5_000
        )
        self.assertEqual(
            registry.snapshot(generated_at_ms=NOW_MS + 6_000)["gauges"],
            {
                "queue_depth": 1,
                "queue_depth_max": 7,
                "inflight": 1,
                "capacity": 4,
                "observed_at": iso_millis(NOW_MS + 5_000),
            },
        )

    def test_impossible_gauge_readings_fail_closed(self) -> None:
        registry = OracleMetricsRegistry()
        cases = (
            {"queue_depth": -1, "inflight": 0, "capacity": 1},
            {"queue_depth": MAX_QUEUE_DEPTH + 1, "inflight": 0, "capacity": 1},
            {"queue_depth": 0, "inflight": 3, "capacity": 2},
            {"queue_depth": 0, "inflight": True, "capacity": 2},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(OracleMetricsError):
                    registry.observe_queue(observed_at_ms=NOW_MS, **overrides)

    def test_record_returns_a_revalidated_sample_and_rejects_foreign_input(self) -> None:
        registry = OracleMetricsRegistry()
        stored = registry.record(sample())
        self.assertEqual(stored, sample())
        for value in (sample().as_document(), None, "sample", 1):
            with self.subTest(value=value):
                with self.assertRaises(OracleMetricsError):
                    registry.record(value)

    def test_render_is_canonical_ascii_json_with_one_trailing_newline(self) -> None:
        registry = OracleMetricsRegistry()
        registry.record(sample())
        payload = registry.render(generated_at_ms=NOW_MS)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(payload.count(b"\n"), 1)
        text = payload.decode("ascii")
        document = json.loads(text)
        self.assertEqual(document["schema"], ORACLE_METRICS_SNAPSHOT_SCHEMA)
        self.assertEqual(
            text[:-1],
            json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ),
        )

    def test_rendering_the_same_window_twice_is_byte_identical(self) -> None:
        registry = OracleMetricsRegistry()
        registry.record(sample())
        registry.record(failed_sample(run_id="1" * 32))
        self.assertEqual(
            registry.render(generated_at_ms=NOW_MS),
            registry.render(generated_at_ms=NOW_MS),
        )

    def test_the_snapshot_key_set_is_closed(self) -> None:
        snapshot = OracleMetricsRegistry().snapshot(generated_at_ms=NOW_MS)
        self.assertEqual(
            sorted(snapshot),
            ["gauges", "generated_at", "latency_ms", "reliability", "runs", "schema", "window"],
        )
        self.assertEqual(
            sorted(snapshot["reliability"]),
            ["attempts_total", "by_error_class", "retried_runs", "success_rate_ppm"],
        )

    def test_a_snapshot_clock_outside_the_supported_range_fails_closed(self) -> None:
        registry = OracleMetricsRegistry()
        for value in (MIN_EPOCH_MS - 1, MAX_EPOCH_MS + 1, True, 1.0):
            with self.subTest(value=value):
                with self.assertRaises(OracleMetricsError):
                    registry.snapshot(generated_at_ms=value)  # type: ignore[arg-type]


class HelperTests(unittest.TestCase):
    def test_new_run_id_is_opaque_hex_and_not_repeated(self) -> None:
        ids = {new_run_id() for _ in range(64)}
        self.assertEqual(len(ids), 64)
        for value in ids:
            with self.subTest(value=value):
                self.assertRegex(value, r"^[0-9a-f]{32}$")

    def test_iso_millis_renders_a_fixed_width_utc_timestamp(self) -> None:
        self.assertEqual(iso_millis(NOW_MS), "2027-01-15T08:00:00.000Z")
        self.assertEqual(iso_millis(NOW_MS + 1), "2027-01-15T08:00:00.001Z")
        self.assertEqual(iso_millis(MIN_EPOCH_MS), "2020-01-01T00:00:00.000Z")

    def test_iso_millis_fails_closed_outside_the_supported_range(self) -> None:
        for value in (MIN_EPOCH_MS - 1, MAX_EPOCH_MS + 1, 1.0, True, "now"):
            with self.subTest(value=value):
                with self.assertRaises(OracleMetricsError):
                    iso_millis(value)


if __name__ == "__main__":
    unittest.main()
