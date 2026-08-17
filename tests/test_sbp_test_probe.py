"""Bounded probe-mode tests (skillbox-sbp-test-probe-mode-sz4d).

Probe mode is the only part of `sbp test` allowed to execute a repository's own
commands, so the tests are organised around the four ways it could betray that
privilege:

* it could **run when it should have refused** -- every one of the five
  authorities is withheld in turn and must produce a *named* refusal, and the
  workspace checks must reject a non-disposable, unadmitted, mismatched or
  inside-the-consumer-tree directory;
* it could **mutate the tree it was reporting on** -- the consumer fixture is
  content-hashed before and after a full probe run, canary included;
* it could **launder an unknown into a proof** -- a `likely` finding may only
  reach `proven` when its exact requirement token was established by a probe of
  exactly the right kind that actually ran;
* it could **lie about what happened** -- a refused probe must never render or
  count as a failure, and a receipt must be byte-identical across runs.

Nothing here launches a process. Every probe goes through a bounded fake runner
and a fake clock, so budget exhaustion and concurrency are deterministic rather
than timing-dependent.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import sbp_test as ST  # noqa: E402
from runtime_manager import sbp_test_findings as R  # noqa: E402
from runtime_manager import sbp_test_probe as P  # noqa: E402

FIXTURES = ROOT_DIR / "tests" / "fixtures" / "sbp_test" / "probe"
CONSUMER = FIXTURES / "consumer"

ARCHIVE = "a" * 64


def _tree_digest(root: Path) -> dict[str, str]:
    """Path -> content hash. Catches an in-place rewrite of the same size."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class FakeClock:
    """A clock the test advances by hand, so budgets are exact, not flaky."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class FakeRunner:
    """A bounded fake executor. Records every request; launches nothing."""

    def __init__(
        self,
        *,
        states: dict[str, str] | None = None,
        peak: int | None = None,
        per_unit_records: tuple[str, ...] = (),
        cancelled: tuple[str, ...] | None = None,
        residual: tuple[str, ...] = (),
        cost_s: float = 0.0,
        clock: FakeClock | None = None,
        flapping: bool = False,
    ) -> None:
        self.states = states or {}
        self.peak = peak
        self.per_unit_records = per_unit_records
        self.cancelled = cancelled
        self.residual = residual
        self.cost_s = cost_s
        self.clock = clock
        self.flapping = flapping
        self.runs: list[P.ProbeRun] = []

    def __call__(self, run: P.ProbeRun) -> P.ProbeObservation:
        self.runs.append(run)
        if self.clock is not None:
            self.clock.now += self.cost_s
        states = {}
        for index, unit in enumerate(run.units):
            if unit.id in self.states:
                states[unit.id] = self.states[unit.id]
            elif unit.synthetic:
                states[unit.id] = "failed"
            elif self.flapping and len(self.runs) % 2 == 0 and index == 0:
                # Same units, different answer on alternate attempts.
                states[unit.id] = "failed"
            else:
                states[unit.id] = "completed"
        cancelled = self.cancelled
        if cancelled is None:
            cancelled = tuple(
                sorted(unit.id for unit in run.units if not unit.synthetic)
                if any(unit.synthetic for unit in run.units)
                else ()
            )
        return P.ProbeObservation(
            unit_states=states,
            peak_concurrency=self.peak if self.peak is not None else run.max_parallel,
            per_unit_records=self.per_unit_records,
            cancelled_units=tuple(cancelled),
            residual_paths=self.residual,
        )


class ProbeFixture(unittest.TestCase):
    """A consumer copy plus an admitted disposable workspace, never overlapping."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.consumer = base / "consumer"
        shutil.copytree(CONSUMER, self.consumer)
        # Sibling, never a child: the whole point is that the two trees are
        # disjoint, and a workspace under the consumer must be refused.
        self.workspace = base / "capsule"
        self.workspace.mkdir()
        P.write_workspace_marker(self.workspace, ARCHIVE)

    def authority(self, **overrides: object) -> P.ProbeAuthority:
        defaults: dict[str, object] = {
            "workspace": self.workspace,
            "wall_clock_budget_s": 30.0,
            "max_parallel": 4,
            "allow_services": False,
            "allow_network": False,
            "repeats": 3,
            "seed": 7,
        }
        defaults.update(overrides)
        return P.ProbeAuthority(**defaults)  # type: ignore[arg-type]

    def units(self) -> tuple[P.ProbeUnit, ...]:
        return (
            P.ProbeUnit("unit-alpha", ("python3", "-m", "unittest", "tests.test_alpha")),
            P.ProbeUnit("unit-beta", ("python3", "-m", "unittest", "tests.test_beta")),
        )

    def run_probes(self, runner: FakeRunner, **overrides: object) -> dict:
        clock = overrides.pop("clock", None)
        return P.run_probes(
            self.units(),
            consumer_root=self.consumer,
            authority=self.authority(**overrides),
            runner=runner,
            clock=clock or FakeClock(),
        )


# --------------------------------------------------------------------------- #
# Authority: every one of the five is required, explicitly
# --------------------------------------------------------------------------- #


class AuthorityRefusalTests(ProbeFixture):
    def test_each_missing_authority_refuses_by_name(self) -> None:
        cases = {
            "workspace": "probe_workspace_missing",
            "wall_clock_budget_s": "probe_budget_missing",
            "max_parallel": "probe_parallelism_missing",
            "allow_services": "probe_service_permission_missing",
            "allow_network": "probe_network_permission_missing",
        }
        for field, expected in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(P.ProbeRefusal) as caught:
                    self.run_probes(FakeRunner(), **{field: None})
                self.assertEqual(caught.exception.code, expected)
                # An agent must be able to repair its own invocation.
                self.assertTrue(caught.exception.next_actions)
                self.assertTrue(caught.exception.needs_input)

    def test_denied_permission_is_explicit_not_absent(self) -> None:
        """`False` is a decision and must be accepted; only `None` refuses."""
        receipt = self.run_probes(FakeRunner(), allow_services=False, allow_network=False)
        self.assertFalse(receipt["authority"]["services_permitted"])
        self.assertFalse(receipt["authority"]["network_permitted"])

    def test_permissions_are_independent(self) -> None:
        receipt = self.run_probes(FakeRunner(), allow_services=True, allow_network=False)
        self.assertTrue(receipt["authority"]["services_permitted"])
        self.assertFalse(receipt["authority"]["network_permitted"])

    def test_out_of_range_authorities_refuse(self) -> None:
        cases = [
            ({"wall_clock_budget_s": 0.0}, "probe_budget_invalid"),
            ({"wall_clock_budget_s": -1.0}, "probe_budget_invalid"),
            ({"wall_clock_budget_s": P.MAX_WALL_CLOCK_BUDGET_S + 1}, "probe_budget_invalid"),
            ({"max_parallel": 0}, "probe_parallelism_invalid"),
            ({"max_parallel": P.MAX_PROBE_PARALLELISM + 1}, "probe_parallelism_invalid"),
            ({"max_parallel": True}, "probe_parallelism_invalid"),
            ({"repeats": 1}, "probe_repeats_invalid"),
            ({"repeats": P.MAX_REPEATS + 1}, "probe_repeats_invalid"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(P.ProbeRefusal) as caught:
                    self.run_probes(FakeRunner(), **overrides)
                self.assertEqual(caught.exception.code, expected)

    def test_no_runner_refuses_rather_than_pretending(self) -> None:
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.run_probes(
                self.units(),
                consumer_root=self.consumer,
                authority=self.authority(),
                runner=None,
            )
        self.assertEqual(caught.exception.code, "probe_runner_missing")

    def test_no_units_refuses(self) -> None:
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.run_probes(
                (),
                consumer_root=self.consumer,
                authority=self.authority(),
                runner=FakeRunner(),
            )
        self.assertEqual(caught.exception.code, "probe_units_missing")

    def test_every_refusal_code_is_registered(self) -> None:
        self.assertTrue(P.NEEDS_INPUT_CODES <= P.REFUSAL_CODES)
        self.assertIn("probe_budget_exhausted", P.REFUSAL_CODES)
        # Budget exhaustion is NOT needs-input: the caller gave us authority and
        # we spent it. It is a refusal of that probe, not of the invocation.
        self.assertNotIn("probe_budget_exhausted", P.NEEDS_INPUT_CODES)


# --------------------------------------------------------------------------- #
# Capsule workspace admission
# --------------------------------------------------------------------------- #


class WorkspaceAdmissionTests(ProbeFixture):
    def test_admits_a_marked_disposable_sibling(self) -> None:
        admitted = P.admit_workspace(
            self.workspace, consumer_root=self.consumer, archive_sha256=ARCHIVE
        )
        self.assertEqual(admitted.archive_sha256, ARCHIVE)
        self.assertEqual(admitted.scratch.name, P.PROBE_SCRATCH_DIRNAME)

    def test_refuses_a_workspace_inside_the_consumer_tree(self) -> None:
        inside = self.consumer / "capsule"
        inside.mkdir()
        P.write_workspace_marker(inside, ARCHIVE)
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.admit_workspace(inside, consumer_root=self.consumer)
        self.assertEqual(caught.exception.code, "probe_workspace_inside_consumer_tree")

    def test_refuses_the_consumer_root_itself(self) -> None:
        P.write_workspace_marker(self.consumer, ARCHIVE)
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.admit_workspace(self.consumer, consumer_root=self.consumer)
        self.assertEqual(caught.exception.code, "probe_workspace_inside_consumer_tree")

    def test_refuses_an_unmarked_directory(self) -> None:
        bare = self.workspace.parent / "bare"
        bare.mkdir()
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.admit_workspace(bare, consumer_root=self.consumer)
        self.assertEqual(caught.exception.code, "probe_workspace_not_admitted")

    def test_refuses_a_workspace_not_marked_disposable(self) -> None:
        P.write_workspace_marker(self.workspace, ARCHIVE, disposable=False)
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.admit_workspace(self.workspace, consumer_root=self.consumer)
        self.assertEqual(caught.exception.code, "probe_workspace_not_disposable")

    def test_refuses_a_capsule_mismatch(self) -> None:
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.admit_workspace(
                self.workspace, consumer_root=self.consumer, archive_sha256="b" * 64
            )
        self.assertEqual(caught.exception.code, "probe_capsule_mismatch")

    def test_refuses_an_unverifiable_archive(self) -> None:
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.admit_workspace(
                self.workspace,
                consumer_root=self.consumer,
                verify_archive=lambda digest: False,
            )
        self.assertEqual(caught.exception.code, "probe_capsule_archive_unverified")

    def test_verified_archive_is_admitted(self) -> None:
        seen: list[str] = []

        def verify(digest: str) -> bool:
            seen.append(digest)
            return True

        P.admit_workspace(
            self.workspace, consumer_root=self.consumer, verify_archive=verify
        )
        self.assertEqual(seen, [ARCHIVE])

    def test_refuses_a_corrupt_marker(self) -> None:
        (self.workspace / P.WORKSPACE_MARKER).write_text("{ not json", encoding="utf-8")
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.admit_workspace(self.workspace, consumer_root=self.consumer)
        self.assertEqual(caught.exception.code, "probe_workspace_not_admitted")

    def test_workspace_payload_carries_no_absolute_path(self) -> None:
        admitted = P.admit_workspace(self.workspace, consumer_root=self.consumer)
        rendered = json.dumps(admitted.to_payload())
        self.assertNotIn(str(self.workspace), rendered)
        self.assertNotIn(tempfile.gettempdir(), rendered)


# --------------------------------------------------------------------------- #
# The probes themselves
# --------------------------------------------------------------------------- #


class ProbeExecutionTests(ProbeFixture):
    def test_all_kinds_are_attempted_in_a_healthy_run(self) -> None:
        receipt = self.run_probes(FakeRunner())
        kinds = [probe["kind"] for probe in receipt["probes"]]
        self.assertEqual(kinds, list(P.PROBE_KINDS))
        self.assertEqual(receipt["counts"]["refused"], 0)

    def test_repeated_serial_runs_agree(self) -> None:
        runner = FakeRunner()
        receipt = self.run_probes(runner, repeats=4)
        serial = _probe(receipt, P.PROBE_SERIAL_REPEAT)
        self.assertEqual(serial["state"], P.STATE_RAN)
        self.assertEqual(serial["attempts"], 4)
        self.assertTrue(serial["observations"]["agreed"])
        serial_runs = [r for r in runner.runs if r.probe_kind == P.PROBE_SERIAL_REPEAT]
        self.assertEqual([r.max_parallel for r in serial_runs], [1, 1, 1, 1])

    def test_disagreeing_serial_runs_are_a_failure_not_a_refusal(self) -> None:
        receipt = self.run_probes(FakeRunner(flapping=True))
        serial = _probe(receipt, P.PROBE_SERIAL_REPEAT)
        self.assertEqual(serial["state"], P.STATE_FAILED)
        self.assertFalse(serial["observations"]["agreed"])
        self.assertIsNone(serial["refusal_code"])
        self.assertIn("non-deterministic", serial["detail"])

    def test_two_way_and_n_way_concurrency_use_the_requested_widths(self) -> None:
        runner = FakeRunner()
        self.run_probes(runner, max_parallel=6)
        widths = {
            run.probe_kind: run.max_parallel
            for run in runner.runs
            if run.probe_kind in (P.PROBE_CONCURRENCY_TWO, P.PROBE_CONCURRENCY_N)
        }
        self.assertEqual(widths[P.PROBE_CONCURRENCY_TWO], 2)
        self.assertEqual(widths[P.PROBE_CONCURRENCY_N], 6)

    def test_randomized_order_is_seeded_and_reproducible(self) -> None:
        first = self.run_probes(FakeRunner(), seed=11)
        second = self.run_probes(FakeRunner(), seed=11)
        orders = _probe(first, P.PROBE_RANDOMIZED_ORDER)["observations"]["orders"]
        self.assertEqual(orders, _probe(second, P.PROBE_RANDOMIZED_ORDER)["observations"]["orders"])
        # Every attempt is a permutation of the declared units -- a probe may
        # reorder the suite, never edit it.
        for order in orders:
            self.assertEqual(sorted(order), ["unit-alpha", "unit-beta"])

    def test_randomized_order_disagreement_is_evidence(self) -> None:
        receipt = self.run_probes(FakeRunner(flapping=True))
        randomized = _probe(receipt, P.PROBE_RANDOMIZED_ORDER)
        self.assertEqual(randomized["state"], P.STATE_FAILED)
        self.assertIn("order dependent", randomized["detail"])


# --------------------------------------------------------------------------- #
# The synthetic canary
# --------------------------------------------------------------------------- #


class CanaryTests(ProbeFixture):
    def test_canary_proves_sibling_cancellation_and_cleanup(self) -> None:
        receipt = self.run_probes(FakeRunner())
        canary = _probe(receipt, P.PROBE_SYNTHETIC_CANARY)
        self.assertEqual(canary["state"], P.STATE_RAN)
        self.assertTrue(canary["observations"]["canary_failed"])
        self.assertEqual(canary["observations"]["cancelled_units"], ["unit-alpha", "unit-beta"])
        self.assertEqual(canary["observations"]["residual_paths"], [])
        self.assertTrue(canary["observations"]["canary_unit_id"].startswith(P.CANARY_UNIT_PREFIX))

    def test_uncancelled_siblings_are_a_failure(self) -> None:
        receipt = self.run_probes(FakeRunner(cancelled=()))
        canary = _probe(receipt, P.PROBE_SYNTHETIC_CANARY)
        self.assertEqual(canary["state"], P.STATE_FAILED)
        self.assertIn("no sibling was cancelled", canary["detail"])

    def test_residual_paths_after_the_canary_are_a_failure(self) -> None:
        receipt = self.run_probes(FakeRunner(residual=("leftover.sock",)))
        canary = _probe(receipt, P.PROBE_SYNTHETIC_CANARY)
        self.assertEqual(canary["state"], P.STATE_FAILED)
        self.assertIn("survived cleanup", canary["detail"])

    def test_a_canary_that_passed_proves_nothing(self) -> None:
        receipt = self.run_probes(
            FakeRunner(states={f"{P.CANARY_UNIT_PREFIX}fail": "completed"})
        )
        canary = _probe(receipt, P.PROBE_SYNTHETIC_CANARY)
        self.assertEqual(canary["state"], P.STATE_FAILED)
        self.assertIn("did not fail", canary["detail"])

    def test_canary_never_reaches_outside_the_workspace(self) -> None:
        admitted = P.admit_workspace(self.workspace, consumer_root=self.consumer)
        outside = P.ProbeUnit(
            f"{P.CANARY_UNIT_PREFIX}evil",
            (str(self.consumer / "tests" / "test_alpha.py"),),
            synthetic=True,
        )
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.assert_canary_safe(outside, admitted)
        self.assertEqual(caught.exception.code, "probe_canary_unsafe")

    def test_an_unmarked_unit_cannot_masquerade_as_the_canary(self) -> None:
        admitted = P.admit_workspace(self.workspace, consumer_root=self.consumer)
        with self.assertRaises(P.ProbeRefusal):
            P.assert_canary_safe(P.ProbeUnit("plain-unit", ()), admitted)

    def test_a_consumer_unit_may_not_claim_the_reserved_prefix(self) -> None:
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.run_probes(
                (P.ProbeUnit(f"{P.CANARY_UNIT_PREFIX}sneaky", ()),),
                consumer_root=self.consumer,
                authority=self.authority(),
                runner=FakeRunner(),
            )
        self.assertEqual(caught.exception.code, "probe_canary_unsafe")

    def test_canary_argv_writes_nothing(self) -> None:
        """The canary is a bare non-zero exit -- no script, no file, no service."""
        admitted = P.admit_workspace(self.workspace, consumer_root=self.consumer)
        canary = P.build_canary(admitted)
        self.assertEqual(canary.argv, ("python3", "-c", "raise SystemExit(1)"))
        self.assertTrue(canary.synthetic)


# --------------------------------------------------------------------------- #
# Zero consumer mutation
# --------------------------------------------------------------------------- #


class ConsumerImmutabilityTests(ProbeFixture):
    def test_a_full_probe_run_changes_no_consumer_byte(self) -> None:
        before = _tree_digest(self.consumer)
        self.run_probes(FakeRunner())
        self.assertEqual(before, _tree_digest(self.consumer))

    def test_a_failing_probe_run_changes_no_consumer_byte(self) -> None:
        before = _tree_digest(self.consumer)
        self.run_probes(FakeRunner(flapping=True, cancelled=(), residual=("x",)))
        self.assertEqual(before, _tree_digest(self.consumer))

    def test_scratch_is_created_inside_the_capsule_only(self) -> None:
        runner = FakeRunner()
        self.run_probes(runner)
        for run in runner.runs:
            self.assertTrue(str(run.scratch).startswith(str(self.workspace.resolve())))
        self.assertFalse((self.consumer / P.PROBE_SCRATCH_DIRNAME).exists())

    def test_receipt_records_the_no_mutation_guard(self) -> None:
        receipt = self.run_probes(FakeRunner())
        self.assertFalse(receipt["consumer_mutation"]["attempted"])
        self.assertIn("harness-owned", receipt["consumer_mutation"]["guard"])


# --------------------------------------------------------------------------- #
# Cleanup and leak verification
# --------------------------------------------------------------------------- #


class CleanupTests(ProbeFixture):
    def test_scratch_is_removed_and_verified(self) -> None:
        receipt = self.run_probes(FakeRunner())
        cleanup = _probe(receipt, P.PROBE_CLEANUP_LEAK)
        self.assertEqual(cleanup["state"], P.STATE_RAN)
        self.assertTrue(cleanup["observations"]["scratch_existed"])
        self.assertTrue(cleanup["observations"]["scratch_removed"])
        self.assertEqual(cleanup["observations"]["leaked_paths"], [])
        self.assertFalse((self.workspace / P.PROBE_SCRATCH_DIRNAME).exists())

    def test_the_workspace_marker_survives_cleanup(self) -> None:
        """Cleanup removes scratch, not the admission itself."""
        self.run_probes(FakeRunner())
        self.assertTrue((self.workspace / P.WORKSPACE_MARKER).is_file())

    def test_cleanup_runs_even_after_the_budget_is_exhausted(self) -> None:
        clock = FakeClock()
        receipt = self.run_probes(
            FakeRunner(cost_s=10.0, clock=clock),
            wall_clock_budget_s=5.0,
            clock=clock,
        )
        self.assertTrue(receipt["budget_exhausted"])
        cleanup = _probe(receipt, P.PROBE_CLEANUP_LEAK)
        self.assertEqual(cleanup["state"], P.STATE_RAN)
        self.assertFalse((self.workspace / P.PROBE_SCRATCH_DIRNAME).exists())


# --------------------------------------------------------------------------- #
# Budget exhaustion
# --------------------------------------------------------------------------- #


class BudgetTests(ProbeFixture):
    def test_exhaustion_refuses_the_remaining_probes(self) -> None:
        clock = FakeClock()
        receipt = self.run_probes(
            FakeRunner(cost_s=4.0, clock=clock),
            wall_clock_budget_s=10.0,
            clock=clock,
        )
        self.assertTrue(receipt["budget_exhausted"])
        refused = [p for p in receipt["probes"] if p["state"] == P.STATE_REFUSED]
        self.assertTrue(refused)
        for probe in refused:
            self.assertEqual(probe["refusal_code"], "probe_budget_exhausted")
            self.assertEqual(probe["attempts"], 0)

    def test_a_refused_probe_is_never_counted_as_a_failure(self) -> None:
        clock = FakeClock()
        receipt = self.run_probes(
            FakeRunner(cost_s=100.0, clock=clock),
            wall_clock_budget_s=1.0,
            clock=clock,
        )
        counts = receipt["counts"]
        self.assertGreater(counts["refused"], 0)
        self.assertEqual(counts["failed"], 0)

    def test_a_generous_budget_refuses_nothing(self) -> None:
        clock = FakeClock()
        receipt = self.run_probes(
            FakeRunner(cost_s=0.1, clock=clock),
            wall_clock_budget_s=600.0,
            clock=clock,
        )
        self.assertFalse(receipt["budget_exhausted"])
        self.assertEqual(receipt["counts"]["refused"], 0)

    def test_exhaustion_upgrades_nothing(self) -> None:
        clock = FakeClock()
        receipt = self.run_probes(
            FakeRunner(cost_s=100.0, clock=clock, per_unit_records=()),
            wall_clock_budget_s=1.0,
            clock=clock,
        )
        report = _report_with_likely("RECEIPT_NOT_COMPOSABLE")
        _upgraded, upgrades = P.upgrade_report(report, receipt)
        self.assertEqual(upgrades, [])


# --------------------------------------------------------------------------- #
# Deterministic receipts
# --------------------------------------------------------------------------- #


class ReceiptTests(ProbeFixture):
    def test_two_identical_runs_digest_identically(self) -> None:
        first = self.run_probes(FakeRunner())
        second = self.run_probes(FakeRunner())
        self.assertEqual(P.receipt_digest(first), P.receipt_digest(second))
        self.assertEqual(P.receipt_json(first), P.receipt_json(second))

    def test_a_different_seed_changes_the_receipt(self) -> None:
        first = self.run_probes(FakeRunner(), seed=1)
        second = self.run_probes(FakeRunner(), seed=2)
        self.assertNotEqual(P.receipt_digest(first), P.receipt_digest(second))

    def test_receipt_is_versioned(self) -> None:
        receipt = self.run_probes(FakeRunner())
        self.assertEqual(receipt["schema"], "probe-receipt/v1")
        self.assertEqual(receipt["schema_version"], P.PROBE_SCHEMA_VERSION)

    def test_receipt_carries_no_wall_clock_or_absolute_path(self) -> None:
        rendered = P.receipt_json(self.run_probes(FakeRunner()))
        self.assertNotIn(str(self.workspace), rendered)
        self.assertNotIn(str(self.consumer), rendered)
        self.assertNotIn("elapsed", rendered)

    def test_receipt_states_the_permission_matrix(self) -> None:
        receipt = self.run_probes(
            FakeRunner(), allow_services=True, allow_network=False, max_parallel=3
        )
        authority = receipt["authority"]
        self.assertEqual(
            {
                authority["services_permitted"],
                authority["network_permitted"],
            },
            {True, False},
        )
        self.assertEqual(authority["max_parallel"], 3)
        self.assertEqual(authority["capsule"]["archive_sha256"], ARCHIVE)
        self.assertTrue(authority["capsule"]["disposable"])

    def test_receipt_publishes_what_can_never_be_proven(self) -> None:
        receipt = self.run_probes(FakeRunner())
        self.assertIn("CROSS_MACHINE_PARTITION_MISSING", receipt["never_provable_by_probe"])
        self.assertNotIn("RECEIPT_NOT_COMPOSABLE", receipt["never_provable_by_probe"])


# --------------------------------------------------------------------------- #
# likely -> proven, only on the exact requirement
# --------------------------------------------------------------------------- #


def _probe(receipt: dict, kind: str) -> dict:
    for entry in receipt["probes"]:
        if entry["kind"] == kind:
            return entry
    raise AssertionError(f"receipt has no {kind} probe")


def _report_with_likely(code: str) -> dict:
    finding = R.Finding(
        code,
        "likely",
        evidence=(R.Evidence("absent", "Makefile#targets", "static read"),),
        reason="static evidence is strong but not proof",
    )
    return R.build_report(R.Subject(label="fixture", capsule_digest=ARCHIVE), [finding])


class UpgradeTests(ProbeFixture):
    def test_exact_requirement_upgrades_likely_to_proven(self) -> None:
        receipt = self.run_probes(FakeRunner(per_unit_records=()))
        report = _report_with_likely("RECEIPT_NOT_COMPOSABLE")
        upgraded, upgrades = P.upgrade_report(report, receipt)
        self.assertEqual([u["finding_code"] for u in upgrades], ["RECEIPT_NOT_COMPOSABLE"])
        statuses = {f["finding_code"]: f["status"] for f in upgraded["findings"]}
        self.assertEqual(statuses["RECEIPT_NOT_COMPOSABLE"], "proven")

    def test_the_upgrade_carries_probe_evidence(self) -> None:
        receipt = self.run_probes(FakeRunner(per_unit_records=()))
        upgraded, _ = P.upgrade_report(_report_with_likely("RECEIPT_NOT_COMPOSABLE"), receipt)
        finding = next(
            f for f in upgraded["findings"] if f["finding_code"] == "RECEIPT_NOT_COMPOSABLE"
        )
        kinds = {item["kind"] for item in finding["evidence"]}
        self.assertIn("probe", kinds)
        # The static evidence is kept, not replaced: the probe confirms the read,
        # it does not erase how the finding was first located.
        self.assertIn("absent", kinds)

    def test_unmet_requirement_does_not_upgrade(self) -> None:
        receipt = self.run_probes(FakeRunner(per_unit_records=("unit-alpha.xml",)))
        upgraded, upgrades = P.upgrade_report(
            _report_with_likely("RECEIPT_NOT_COMPOSABLE"), receipt
        )
        self.assertEqual(upgrades, [])
        statuses = {f["finding_code"]: f["status"] for f in upgraded["findings"]}
        self.assertEqual(statuses["RECEIPT_NOT_COMPOSABLE"], "likely")

    def test_a_code_with_no_requirement_never_upgrades(self) -> None:
        receipt = self.run_probes(FakeRunner(per_unit_records=()))
        upgraded, upgrades = P.upgrade_report(
            _report_with_likely("CROSS_MACHINE_PARTITION_MISSING"), receipt
        )
        self.assertEqual(upgrades, [])
        statuses = {f["finding_code"]: f["status"] for f in upgraded["findings"]}
        self.assertEqual(statuses["CROSS_MACHINE_PARTITION_MISSING"], "likely")

    def test_unknown_is_never_upgraded_to_proven(self) -> None:
        receipt = self.run_probes(FakeRunner(per_unit_records=()))
        report = R.build_report(
            R.Subject(label="fixture"),
            [R.Finding("RECEIPT_NOT_COMPOSABLE", "unknown", reason="not readable")],
        )
        upgraded, upgrades = P.upgrade_report(report, receipt)
        self.assertEqual(upgrades, [])
        statuses = {f["finding_code"]: f["status"] for f in upgraded["findings"]}
        self.assertEqual(statuses["RECEIPT_NOT_COMPOSABLE"], "unknown")

    def test_serialization_under_fanout_upgrades_the_lock_seam(self) -> None:
        receipt = self.run_probes(FakeRunner(peak=1), max_parallel=8)
        _upgraded, upgrades = P.upgrade_report(
            _report_with_likely("EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING"), receipt
        )
        self.assertEqual(
            [u["probe_kind"] for u in upgrades], [P.PROBE_CONCURRENCY_N]
        )

    def test_a_real_fanout_does_not_upgrade_the_lock_seam(self) -> None:
        receipt = self.run_probes(FakeRunner(peak=8), max_parallel=8)
        _upgraded, upgrades = P.upgrade_report(
            _report_with_likely("EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING"), receipt
        )
        self.assertEqual(upgrades, [])

    def test_a_concurrent_collision_upgrades_the_static_endpoint(self) -> None:
        receipt = self.run_probes(FakeRunner(peak=2, states={"unit-beta": "failed"}))
        _upgraded, upgrades = P.upgrade_report(
            _report_with_likely("SERVICE_ENDPOINT_STATIC"), receipt
        )
        self.assertEqual([u["probe_kind"] for u in upgrades], [P.PROBE_CONCURRENCY_TWO])

    def test_upgrade_recomputes_gates_rather_than_patching_status(self) -> None:
        receipt = self.run_probes(FakeRunner(peak=1), max_parallel=8)
        report = _report_with_likely("EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING")
        self.assertTrue(report["gates"]["parallel"]["admitted"])
        upgraded, _ = P.upgrade_report(report, receipt)
        # `likely` never gates; `proven` on a parallel-blocking code must.
        self.assertFalse(upgraded["gates"]["parallel"]["admitted"])
        self.assertIn(
            "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING",
            upgraded["gates"]["parallel"]["blocked_by"],
        )
        self.assertEqual(upgraded[R.V1_READINESS_KEY], "blocked")

    def test_cleared_findings_survive_the_rebuild(self) -> None:
        receipt = self.run_probes(FakeRunner(per_unit_records=()))
        report = R.build_report(
            R.Subject(label="fixture"),
            [
                R.Finding(
                    "RECEIPT_NOT_COMPOSABLE",
                    "likely",
                    evidence=(R.Evidence("absent", "Makefile#targets", "static"),),
                    reason="static only",
                )
            ],
            [R.Cleared("PATH_FRAGILE", (R.Evidence("absent", "Makefile#targets", "clean"),))],
        )
        upgraded, _ = P.upgrade_report(report, receipt)
        self.assertEqual(
            [c["finding_code"] for c in upgraded["cleared"]], ["PATH_FRAGILE"]
        )

    def test_every_requirement_names_a_registered_code_and_real_probe(self) -> None:
        for code, requirement in P.PROOF_REQUIREMENTS.items():
            with self.subTest(code=code):
                self.assertIn(code, R.CODES)
                self.assertIn(requirement.probe_kind, P.PROBE_KINDS)
                self.assertTrue(requirement.statement.strip())

    def test_provable_and_never_provable_partition_the_registry(self) -> None:
        self.assertEqual(
            set(P.PROOF_REQUIREMENTS) | set(P.NEVER_PROVABLE_BY_PROBE), set(R.CODES)
        )
        self.assertFalse(set(P.PROOF_REQUIREMENTS) & set(P.NEVER_PROVABLE_BY_PROBE))


# --------------------------------------------------------------------------- #
# Front-door integration
# --------------------------------------------------------------------------- #


class FrontDoorProbeTests(ProbeFixture):
    def test_static_score_is_the_default_and_says_so(self) -> None:
        payload = ST.score_payload(self.consumer)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["probed"])
        self.assertTrue(payload["analysis_only"])
        self.assertNotIn("probe_receipt", payload)
        self.assertFalse(payload["report"]["provenance"]["executed_anything"])

    def test_probe_without_authority_refuses_and_keeps_the_static_report(self) -> None:
        payload = ST.score_payload(
            self.consumer, probe=P.ProbeAuthority(), probe_runner=FakeRunner()
        )
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["probed"])
        self.assertEqual(payload["probe_state"], "refused")
        self.assertEqual(ST.score_exit_class(payload), "needs_input")
        # The analysis is still worth reading.
        self.assertIn("report", payload)

    def test_probe_run_marks_the_report_as_executed(self) -> None:
        payload = ST.score_payload(
            self.consumer,
            probe=self.authority(),
            probe_runner=FakeRunner(),
        )
        self.assertTrue(payload["probed"])
        self.assertFalse(payload["analysis_only"])
        self.assertTrue(payload["report"]["provenance"]["executed_anything"])
        self.assertEqual(
            payload["report"]["provenance"]["probe"]["receipt_digest"],
            payload["probe_receipt_digest"],
        )
        self.assertEqual(ST.score_exit_class(payload), "ok")

    def test_a_failed_probe_is_evidence_and_still_exits_ok(self) -> None:
        payload = ST.score_payload(
            self.consumer,
            probe=self.authority(),
            probe_runner=FakeRunner(flapping=True),
        )
        self.assertTrue(payload["probed"])
        self.assertGreater(payload["probe_counts"]["failed"], 0)
        self.assertEqual(ST.score_exit_class(payload), "ok")

    def test_probe_units_come_from_the_declared_manifest(self) -> None:
        runner = FakeRunner()
        ST.score_payload(self.consumer, probe=self.authority(), probe_runner=runner)
        declared = {
            unit.id for run in runner.runs for unit in run.units if not unit.synthetic
        }
        self.assertEqual(declared, {"unit-alpha", "unit-beta"})

    def test_probing_the_repo_from_inside_itself_refuses(self) -> None:
        inside = self.consumer / "capsule"
        inside.mkdir()
        P.write_workspace_marker(inside, ARCHIVE)
        payload = ST.score_payload(
            self.consumer,
            probe=self.authority(workspace=inside),
            probe_runner=FakeRunner(),
        )
        self.assertEqual(payload["error_code"], "probe_workspace_inside_consumer_tree")
        self.assertEqual(ST.score_exit_class(payload), "needs_input")

    def test_front_door_probe_leaves_the_consumer_tree_untouched(self) -> None:
        before = _tree_digest(self.consumer)
        ST.score_payload(self.consumer, probe=self.authority(), probe_runner=FakeRunner())
        self.assertEqual(before, _tree_digest(self.consumer))

    def test_probe_authorities_are_named_once(self) -> None:
        self.assertEqual(len(ST.PROBE_AUTHORITIES), 5)
        self.assertIn("capsule_workspace", ST.PROBE_AUTHORITIES)


# --------------------------------------------------------------------------- #
# The local-executor adapter
# --------------------------------------------------------------------------- #


class ExecutorAdapterTests(ProbeFixture):
    """The seam is wired to the real executor, and projects plans it accepts."""

    def _plan(self, units, *, width, kind="serial_repeat"):
        captured: dict = {}

        def fake_execute(plan_content, **kwargs):
            captured["plan"] = plan_content
            captured["kwargs"] = kwargs
            return _FakeOutcome(plan_content)

        admitted = P.admit_workspace(self.workspace, consumer_root=self.consumer)
        run = P.ProbeRun(
            probe_kind=kind,
            attempt=1,
            units=tuple(units),
            max_parallel=width,
            workspace=admitted.root,
            scratch=admitted.scratch / kind,
            allow_services=False,
            allow_network=False,
            deadline_s=30.0,
        )
        run.scratch.mkdir(parents=True, exist_ok=True)
        observation = P.local_executor_runner(execute=fake_execute)(run)
        return captured, observation

    def test_width_one_puts_each_unit_in_its_own_wave_in_order(self) -> None:
        """Otherwise `schedule_batches` re-sorts a shuffle away silently."""
        units = (P.ProbeUnit("unit-beta", ("x",)), P.ProbeUnit("unit-alpha", ("y",)))
        captured, _ = self._plan(units, width=1)
        self.assertEqual(captured["plan"]["waves"], [["unit-beta"], ["unit-alpha"]])

    def test_wide_runs_use_a_single_wave(self) -> None:
        captured, _ = self._plan(self.units(), width=4)
        self.assertEqual(captured["plan"]["waves"], [["unit-alpha", "unit-beta"]])

    def test_canary_gets_its_own_wave_and_siblings_depend_on_it(self) -> None:
        admitted = P.admit_workspace(self.workspace, consumer_root=self.consumer)
        canary = P.build_canary(admitted)
        captured, _ = self._plan((*self.units(), canary), width=4)
        self.assertEqual(
            captured["plan"]["waves"], [[canary.id], ["unit-alpha", "unit-beta"]]
        )
        edges = captured["plan"]["edges"]
        self.assertEqual(
            sorted((e["source"], e["target"], e["kind"]) for e in edges),
            [
                ("unit-alpha", canary.id, "depends_on"),
                ("unit-beta", canary.id, "depends_on"),
            ],
        )

    def test_the_executor_runs_against_the_capsule_never_the_consumer(self) -> None:
        captured, _ = self._plan(self.units(), width=2)
        self.assertEqual(captured["kwargs"]["repo"], self.workspace.resolve())
        self.assertNotEqual(captured["kwargs"]["repo"], self.consumer)
        for unit in captured["plan"]["units"]:
            self.assertIsNone(unit["cwd"])

    def test_the_probe_environment_is_narrowed_and_states_its_permissions(self) -> None:
        captured, _ = self._plan(self.units(), width=2)
        env = captured["kwargs"]["base_env"]
        self.assertEqual(env[P.ENV_SERVICES_ALLOWED], "0")
        self.assertEqual(env[P.ENV_NETWORK_ALLOWED], "0")
        # A probe that inherited the whole shell would carry stray tokens into a
        # run whose whole premise is that it is disposable.
        self.assertTrue(set(env) <= set(P._PROBE_ENV_KEEP) | {
            P.ENV_SERVICES_ALLOWED, P.ENV_NETWORK_ALLOWED, P.ENV_WORKSPACE
        })

    def test_units_declared_with_a_timeout_ceiling(self) -> None:
        captured, _ = self._plan(self.units(), width=2)
        for unit in captured["plan"]["units"]:
            self.assertGreaterEqual(unit["timeout_s"], 1)
            self.assertTrue(unit["runnable"])


class _FakeOutcome:
    """Minimal stand-in for `RunOutcome` -- only `to_payload` is consumed."""

    def __init__(self, plan_content: dict) -> None:
        self._plan = plan_content

    def to_payload(self) -> dict:
        units = [u["id"] for u in self._plan["units"]]
        return {
            "results": [{"unit_id": uid, "state": "completed"} for uid in units],
            "schedule": {
                "batches": [
                    {"units": list(wave)} for wave in self._plan["waves"]
                ]
            },
        }


class ServicePermissionEnforcementTests(ProbeFixture):
    def test_a_unit_needing_a_service_is_refused_when_services_are_denied(self) -> None:
        """The gate refuses to launch; it does not hope the unit behaves."""
        with self.assertRaises(P.ProbeRefusal) as caught:
            P.run_probes(
                (P.ProbeUnit("unit-db", ("x",), services=("postgres",)),),
                consumer_root=self.consumer,
                authority=self.authority(allow_services=False),
                runner=FakeRunner(),
            )
        self.assertEqual(caught.exception.code, "probe_services_denied_but_required")
        self.assertIn("unit-db", caught.exception.message)

    def test_the_same_unit_is_admitted_when_services_are_allowed(self) -> None:
        receipt = P.run_probes(
            (P.ProbeUnit("unit-db", ("x",), services=("postgres",)),),
            consumer_root=self.consumer,
            authority=self.authority(allow_services=True),
            runner=FakeRunner(),
            clock=FakeClock(),
        )
        self.assertTrue(receipt["authority"]["services_permitted"])

    def test_the_receipt_never_overclaims_network_enforcement(self) -> None:
        """Denial we cannot enforce must not render like denial we can."""
        receipt = self.run_probes(FakeRunner())
        enforcement = receipt["authority"]["enforcement"]
        self.assertEqual(enforcement["services"], "refused_before_launch")
        self.assertEqual(enforcement["network"], "declared_only")


class RealExecutorEvidenceTests(ProbeFixture):
    """Against the REAL executor: exit codes are read, not assumed.

    The fake runner can only prove the orchestration. These launch actual
    processes (two `python3 -c` one-liners, no service, no network) so a
    regression that stopped reading exit codes -- and therefore reported every
    suite as healthy -- could not pass.
    """

    def _real(self, units, **overrides):
        return P.run_probes(
            units,
            consumer_root=self.consumer,
            authority=self.authority(repeats=2, **overrides),
            runner=P.local_executor_runner(),
        )

    def test_a_genuinely_failing_unit_is_reported_failed(self) -> None:
        receipt = self._real(
            (
                P.ProbeUnit("unit-ok", ("python3", "-c", "print('ok')")),
                P.ProbeUnit("unit-bad", ("python3", "-c", "raise SystemExit(3)")),
            )
        )
        two = _probe(receipt, P.PROBE_CONCURRENCY_TWO)
        self.assertEqual(two["state"], P.STATE_FAILED)
        self.assertEqual(two["observations"]["failed_units"], ["unit-bad"])
        # Evidence, not a refusal: we learned something about the suite.
        self.assertIsNone(two["refusal_code"])
        self.assertEqual(receipt["counts"]["refused"], 0)

    def test_a_run_that_never_completed_proves_nothing(self) -> None:
        """The completion guard: no upgrade off a suite that did not finish."""
        receipt = self._real(
            (P.ProbeUnit("unit-bad", ("python3", "-c", "raise SystemExit(3)")),)
        )
        serial = _probe(receipt, P.PROBE_SERIAL_REPEAT)
        self.assertFalse(serial["observations"]["all_units_completed"])
        self.assertEqual(serial["established"], [])
        _upgraded, upgrades = P.upgrade_report(
            _report_with_likely("RECEIPT_NOT_COMPOSABLE"), receipt
        )
        self.assertEqual(upgrades, [])

    def test_a_passing_suite_really_completes(self) -> None:
        receipt = self._real(
            (P.ProbeUnit("unit-ok", ("python3", "-c", "print('ok')")),)
        )
        serial = _probe(receipt, P.PROBE_SERIAL_REPEAT)
        self.assertTrue(serial["observations"]["all_units_completed"])
        self.assertEqual(serial["state"], P.STATE_RAN)

    def test_the_real_canary_process_fails_and_cancels_siblings(self) -> None:
        receipt = self._real(
            (P.ProbeUnit("unit-ok", ("python3", "-c", "print('ok')")),)
        )
        canary = _probe(receipt, P.PROBE_SYNTHETIC_CANARY)
        self.assertEqual(canary["observations"]["canary_state"], "failed")
        self.assertEqual(canary["observations"]["cancelled_units"], ["unit-ok"])

    def test_a_launch_failure_is_not_a_test_failure_it_is_evidence(self) -> None:
        """A command that cannot start still yields a named non-completion."""
        receipt = self._real(
            (P.ProbeUnit("unit-missing", ("definitely-not-a-real-binary-xyz",)),)
        )
        serial = _probe(receipt, P.PROBE_SERIAL_REPEAT)
        self.assertFalse(serial["observations"]["all_units_completed"])
        self.assertEqual(receipt["counts"]["refused"], 0)

    def test_the_real_executor_leaves_the_consumer_tree_alone(self) -> None:
        before = _tree_digest(self.consumer)
        self._real((P.ProbeUnit("unit-ok", ("python3", "-c", "print('ok')")),))
        self.assertEqual(before, _tree_digest(self.consumer))


# --------------------------------------------------------------------------- #
# Real CLI proof -- the acceptance criterion
# --------------------------------------------------------------------------- #


class RealCliProbeTests(unittest.TestCase):
    """`sbp test score --probe` really probes, through the real CLI.

    This is the bead's acceptance criterion and the thing the first pass got
    wrong: the seam existed but nothing was plugged into it, so a fully
    authorized invocation still ended at `probe_runner_missing`. The proof
    shells out to the actual CLI rather than calling the front door in-process,
    because the defect was in the wiring, not the logic.
    """

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(FIXTURES))
        from cli_proof import run_cli_probe  # noqa: PLC0415

        cls._tmp = tempfile.TemporaryDirectory()
        cls.payload = run_cli_probe(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def receipt(self) -> dict:
        return self.payload["probe_receipt"]

    def test_the_cli_no_longer_refuses_with_probe_runner_missing(self) -> None:
        self.assertNotEqual(self.payload.get("error_code"), "probe_runner_missing")
        self.assertTrue(self.payload["probed"])
        self.assertEqual(self.payload["_exit_code"], 0)

    def test_every_probe_kind_actually_ran(self) -> None:
        probes = {p["kind"]: p for p in self.receipt()["probes"]}
        self.assertEqual(sorted(probes), sorted(P.PROBE_KINDS))
        for kind, probe in sorted(probes.items()):
            with self.subTest(kind=kind):
                self.assertNotEqual(probe["state"], P.STATE_REFUSED)
                self.assertIsNone(probe["refusal_code"])
                self.assertGreaterEqual(probe["attempts"], 1)
        self.assertEqual(self.receipt()["counts"]["refused"], 0)

    def test_services_and_network_were_explicitly_denied(self) -> None:
        authority = self.receipt()["authority"]
        self.assertIs(authority["services_permitted"], False)
        self.assertIs(authority["network_permitted"], False)

    def test_units_really_executed_and_completed(self) -> None:
        serial = next(
            p for p in self.receipt()["probes"] if p["kind"] == P.PROBE_SERIAL_REPEAT
        )
        self.assertTrue(serial["observations"]["all_units_completed"])

    def test_concurrency_was_really_reached(self) -> None:
        two = next(
            p for p in self.receipt()["probes"] if p["kind"] == P.PROBE_CONCURRENCY_TWO
        )
        self.assertGreaterEqual(two["observations"]["peak_concurrency"], 2)

    def test_the_randomized_probe_really_reordered_units(self) -> None:
        randomized = next(
            p for p in self.receipt()["probes"] if p["kind"] == P.PROBE_RANDOMIZED_ORDER
        )
        orders = randomized["observations"]["orders"]
        self.assertGreater(len({tuple(order) for order in orders}), 1)

    def test_the_canary_failed_and_cancelled_its_siblings(self) -> None:
        canary = next(
            p for p in self.receipt()["probes"] if p["kind"] == P.PROBE_SYNTHETIC_CANARY
        )
        self.assertTrue(canary["observations"]["canary_failed"])
        self.assertEqual(canary["observations"]["canary_state"], "failed")
        self.assertEqual(
            canary["observations"]["cancelled_units"], ["unit-alpha", "unit-beta"]
        )
        self.assertEqual(canary["observations"]["residual_paths"], [])

    def test_scratch_was_cleaned_up(self) -> None:
        cleanup = next(
            p for p in self.receipt()["probes"] if p["kind"] == P.PROBE_CLEANUP_LEAK
        )
        self.assertTrue(cleanup["observations"]["scratch_removed"])
        self.assertEqual(cleanup["observations"]["leaked_paths"], [])
        self.assertFalse((Path(self.payload["_workspace"]) / ".sbp-probe").exists())

    def test_the_consumer_tree_was_not_mutated(self) -> None:
        self.assertEqual(self.payload["_consumer_before"], self.payload["_consumer_after"])
        self.assertTrue(self.payload["_consumer_before"])  # the check is not vacuous

    def test_a_real_run_upgraded_likely_to_proven(self) -> None:
        upgrades = self.payload["probe_upgrades"]
        self.assertEqual(
            [u["finding_code"] for u in upgrades], ["RECEIPT_NOT_COMPOSABLE"]
        )
        self.assertEqual(upgrades[0]["probe_kind"], P.PROBE_SERIAL_REPEAT)
        statuses = {
            f["finding_code"]: f["status"] for f in self.payload["report"]["findings"]
        }
        self.assertEqual(statuses["RECEIPT_NOT_COMPOSABLE"], "proven")

    def test_provenance_admits_it_executed(self) -> None:
        provenance = self.payload["report"]["provenance"]
        self.assertTrue(provenance["executed_anything"])
        self.assertEqual(
            provenance["probe"]["receipt_digest"], self.payload["probe_receipt_digest"]
        )

    def test_the_bundled_proof_script_agrees(self) -> None:
        from cli_proof import check  # noqa: PLC0415

        self.assertEqual(check(self.payload), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
