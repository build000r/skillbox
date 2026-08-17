"""Bounded-concurrency federation behind ``doctor --all``.

The federation's job is to add identity, provenance and ordering to three
authoritative surfaces — never a second opinion about their own domains. So the
tests split in two: concurrency/latency behaviour, and fidelity (a provider's
native verdict survives the projection, and a surface that reported no verdict
is not given one).
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import health_federation as FED  # noqa: E402
from runtime_manager.health_protocol import (  # noqa: E402
    SCOPE_RUNTIME,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_TIMED_OUT,
    STATUS_UNAVAILABLE,
    STATUS_WARN,
    CheckScope,
    HealthCheckResult,
    Provenance,
    ProviderDescriptor,
)


def result(provider_id: str, check_id: str, status: str = STATUS_PASS) -> HealthCheckResult:
    # A non-pass result carries an inspect action, exactly as the real providers
    # build one: a result with ACTION_NONE never competes for the primary slot.
    action = (
        FED._inspect_action(f"{provider_id}:{check_id}", f"review {check_id}")
        if status != STATUS_PASS
        else HealthCheckResult.__dataclass_fields__["next_action"].default
    )
    return HealthCheckResult(
        check_id=check_id,
        provider_id=provider_id,
        scope=CheckScope(kind=SCOPE_RUNTIME),
        status=status,
        severity="none" if status == STATUS_PASS else "critical",
        observed_at=time.time(),
        provenance=Provenance(provider_id=provider_id, source="test"),
        next_action=action,
    )


class FakeProvider:
    """A provider with a controllable delay and outcome."""

    def __init__(self, provider_id: str, delay: float = 0.0, raises: Exception | None = None,
                 results: tuple[HealthCheckResult, ...] | None = None) -> None:
        self.provider_id = provider_id
        self.delay = delay
        self.raises = raises
        self.results = results
        self.calls = 0

    def describe(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id=self.provider_id,
            title=self.provider_id,
            scope_kinds=(SCOPE_RUNTIME,),
        )

    def collect(self):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.results or (result(self.provider_id, "check"),)


class BoundedConcurrencyTests(unittest.TestCase):
    """Latency tracks the slowest provider plus overhead, not the sum."""

    DELAY = 0.4

    def test_total_latency_is_sub_additive(self) -> None:
        providers = [FakeProvider(f"p{index}", delay=self.DELAY) for index in range(3)]
        started = time.monotonic()
        report = FED.collect_health(providers, timeout_s=30, max_workers=4)
        elapsed = time.monotonic() - started

        serial = self.DELAY * len(providers)
        self.assertEqual(3, len(report.results))
        # Comfortably below the serial sum and above one provider's own cost:
        # the assertion is "concurrent", not a brittle exact timing.
        self.assertLess(elapsed, serial * 0.75, f"{elapsed:.3f}s vs serial {serial:.3f}s")
        self.assertGreaterEqual(elapsed, self.DELAY * 0.5)

    def test_the_report_records_how_it_collected(self) -> None:
        report = FED.collect_health([FakeProvider("p0")], timeout_s=11, max_workers=2)
        payload = report.to_payload()
        self.assertEqual(11, payload["timeout_s"])
        self.assertEqual(2, payload["max_workers"])
        self.assertGreaterEqual(payload["elapsed_s"], 0.0)
        self.assertEqual(["p0"], [p["provider_id"] for p in payload["providers"]])

    def test_worker_count_never_exceeds_the_provider_count(self) -> None:
        report = FED.collect_health([FakeProvider("p0")], max_workers=64)
        self.assertEqual(1, len(report.results))

    def test_invalid_bounds_are_refused(self) -> None:
        for kwargs in ({"timeout_s": 0}, {"timeout_s": -1}, {"max_workers": 0}):
            with self.assertRaises(ValueError):
                FED.collect_health([FakeProvider("p0")], **kwargs)  # type: ignore[arg-type]

    def test_no_providers_is_an_empty_report_not_a_crash(self) -> None:
        report = FED.collect_health([])
        self.assertEqual((), report.results)
        self.assertEqual((), report.descriptors)


class DeterminismTests(unittest.TestCase):
    """Output order never depends on which provider finished first."""

    def test_order_is_stable_regardless_of_completion_order(self) -> None:
        slow_first = [
            FakeProvider("alpha", delay=0.25, results=(result("alpha", "a", STATUS_FAIL),)),
            FakeProvider("beta", delay=0.0, results=(result("beta", "b", STATUS_PASS),)),
        ]
        fast_first = [
            FakeProvider("alpha", delay=0.0, results=(result("alpha", "a", STATUS_FAIL),)),
            FakeProvider("beta", delay=0.25, results=(result("beta", "b", STATUS_PASS),)),
        ]
        first = FED.federated_health_payload(FED.collect_health(slow_first, timeout_s=30))
        second = FED.federated_health_payload(FED.collect_health(fast_first, timeout_s=30))
        identity = lambda payload: [  # noqa: E731
            (c["provider_id"], c["check_id"], c["status"]) for c in payload["checks"]
        ]
        self.assertEqual(identity(first), identity(second))

    def test_descriptors_are_sorted_by_provider_id(self) -> None:
        report = FED.collect_health(
            [FakeProvider("zulu"), FakeProvider("alpha"), FakeProvider("mike")]
        )
        self.assertEqual(
            ["alpha", "mike", "zulu"],
            [descriptor.provider_id for descriptor in report.descriptors],
        )


class TruthfulFailureTests(unittest.TestCase):
    """A provider that produces no verdict still owes the operator a result."""

    def test_a_raising_provider_becomes_unavailable_not_an_exception(self) -> None:
        providers = [
            FakeProvider("good"),
            FakeProvider("bad", raises=RuntimeError("backend is down")),
        ]
        report = FED.collect_health(providers, timeout_s=30)
        by_provider = {r.provider_id: r for r in report.results}
        self.assertEqual(STATUS_PASS, by_provider["good"].status)
        broken = by_provider["bad"]
        self.assertEqual(STATUS_UNAVAILABLE, broken.status)
        self.assertIn("RuntimeError", broken.cause)
        self.assertIn("backend is down", broken.cause)

    def test_a_cause_carries_no_traceback(self) -> None:
        report = FED.collect_health(
            [FakeProvider("bad", raises=ValueError("line one\nline two"))], timeout_s=30
        )
        cause = report.results[0].cause
        self.assertIn("line one", cause)
        self.assertNotIn("line two", cause)
        self.assertNotIn("Traceback", cause)

    def test_an_overrunning_provider_becomes_timed_out_with_its_cap(self) -> None:
        report = FED.collect_health(
            [FakeProvider("slow", delay=1.5)], timeout_s=0.2, max_workers=2
        )
        self.assertEqual(1, len(report.results))
        timed_out = report.results[0]
        self.assertEqual(STATUS_TIMED_OUT, timed_out.status)
        self.assertEqual(0.2, timed_out.timeout_s)
        self.assertIn("cap", timed_out.cause)

    def test_one_slow_provider_does_not_erase_the_others(self) -> None:
        report = FED.collect_health(
            [FakeProvider("fast"), FakeProvider("slow", delay=1.5)],
            timeout_s=0.3,
            max_workers=4,
        )
        statuses = {r.provider_id: r.status for r in report.results}
        self.assertEqual(STATUS_PASS, statuses["fast"])
        self.assertEqual(STATUS_TIMED_OUT, statuses["slow"])

    def test_unknown_results_are_counted_as_unknown(self) -> None:
        payload = FED.federated_health_payload(
            FED.collect_health(
                [FakeProvider("bad", raises=RuntimeError("x"))], timeout_s=30
            )
        )
        self.assertEqual(1, payload["summary"]["unknown"])

    def test_a_provider_returning_a_non_result_is_a_programming_error(self) -> None:
        class Bogus(FakeProvider):
            def collect(self):
                return ["not a result"]

        with self.assertRaises(TypeError):
            FED.collect_health([Bogus("bogus")], timeout_s=30)


class PayloadContractTests(unittest.TestCase):
    """Stable identity, provenance, and exactly one primary action."""

    def payload(self):
        providers = [
            FakeProvider("alpha", results=(result("alpha", "a", STATUS_FAIL),)),
            FakeProvider("beta", results=(result("beta", "b", STATUS_WARN),)),
            FakeProvider("gamma", results=(result("gamma", "c", STATUS_PASS),)),
        ]
        return FED.federated_health_payload(FED.collect_health(providers, timeout_s=30))

    def test_the_envelope_declares_itself_read_only(self) -> None:
        payload = self.payload()
        self.assertEqual(FED.FEDERATION_SCHEMA, payload["schema"])
        self.assertTrue(payload["read_only"])
        for descriptor in payload["collection"]["providers"]:
            self.assertTrue(descriptor["read_only"])

    def test_every_check_carries_identity_and_provenance(self) -> None:
        for check in self.payload()["checks"]:
            self.assertTrue(check["check_id"])
            self.assertTrue(check["provider_id"])
            self.assertTrue(check["provenance"]["source"])
            self.assertEqual(check["provider_id"], check["provenance"]["provider_id"])

    def test_exactly_one_primary_action(self) -> None:
        prioritization = self.payload()["prioritization"]
        primary = prioritization["primary"]
        self.assertIsNotNone(primary)
        # The worst finding wins, and it is a single object, never a list.
        self.assertEqual("alpha", primary["provider_id"])
        self.assertIsInstance(primary, dict)

    def test_no_field_is_an_executable_action(self) -> None:
        # The bead's failure-avoided clause: a report must not become a menu of
        # commands something could run.
        payload = self.payload()
        for check in payload["checks"]:
            action = check.get("next_action") or {}
            if action:
                self.assertFalse(action.get("executable", False))
                self.assertTrue(action.get("requires_human", True))
        for reference in payload["prioritization"].get("secondary") or []:
            self.assertNotIn("fix_command", reference)

    def test_text_rendering_shows_one_next_action(self) -> None:
        lines = FED.federation_text_lines(self.payload())
        self.assertTrue(any("read-only" in line for line in lines))
        self.assertEqual(1, sum(1 for line in lines if line.strip().startswith("next:")))


class ProviderFidelityTests(unittest.TestCase):
    """Native verdicts survive projection; absent verdicts are not invented."""

    def test_structure_gate_statuses_map_natively(self) -> None:
        gates = {
            "gates": [
                {"name": "g_pass", "kind": "structure", "status": "pass", "detail": "ok"},
                {"name": "g_fail", "kind": "structure", "status": "fail", "detail": "broke"},
                {
                    "name": "g_cap",
                    "kind": "structure",
                    "status": "inco",
                    "detail": "exceeded its cap after 20s",
                    "cap_s": 20.0,
                },
                {
                    "name": "g_dep",
                    "kind": "runtime",
                    "status": "inco",
                    "detail": "skillbox-config repo not found on this box",
                },
            ]
        }
        provider = FED.StructureDoctorProvider(ROOT_DIR)
        with mock.patch(
            "runtime_manager.structure_doctor.run_structure_doctor", return_value=gates
        ):
            collected = {r.check_id: r for r in provider.collect()}
        self.assertEqual(STATUS_PASS, collected["g_pass"].status)
        self.assertEqual(STATUS_FAIL, collected["g_fail"].status)
        # A cap overrun is a timeout; an unreachable dependency is not.
        self.assertEqual(STATUS_TIMED_OUT, collected["g_cap"].status)
        self.assertEqual(20.0, collected["g_cap"].timeout_s)
        self.assertEqual(STATUS_UNAVAILABLE, collected["g_dep"].status)
        self.assertIn("not found", collected["g_dep"].cause)

    def test_factual_evidence_sections_do_not_become_invented_checks(self) -> None:
        # The regression this guards: most evidence sections carry facts, not a
        # verdict. Mapping "no status" to unavailable manufactured seven unknowns
        # the provider never reported.
        packet = {
            "overall": "red",
            "blocked_conditions": ["doctor: 2 failing"],
            "next_actions": ["python3 .env-manager/manage.py doctor"],
            "sections": {
                "doctor": {"status": "fail", "next_actions": ["run doctor"]},
                "git": {"branch": "main", "dirty": True, "dirty_count": 103},
                "beads": {"present": True},
            },
        }
        provider = FED.RuntimeEvidenceProvider(ROOT_DIR, {})
        with mock.patch(
            "runtime_manager.evidence.collect_runtime_evidence", return_value=packet
        ):
            collected = {r.check_id: r for r in provider.collect()}
        self.assertEqual({"doctor", "evidence-packet"}, set(collected))
        self.assertNotIn("git", collected)
        self.assertNotIn("beads", collected)
        self.assertEqual(STATUS_FAIL, collected["doctor"].status)

    def test_the_packet_verdict_is_carried_and_facts_ride_as_evidence(self) -> None:
        packet = {
            "overall": "green",
            "blocked_conditions": [],
            "sections": {"git": {"dirty": False}, "beads": {"present": True}},
        }
        provider = FED.RuntimeEvidenceProvider(ROOT_DIR, {})
        with mock.patch(
            "runtime_manager.evidence.collect_runtime_evidence", return_value=packet
        ):
            collected = {r.check_id: r for r in provider.collect()}
        packet_check = collected["evidence-packet"]
        self.assertEqual(STATUS_PASS, packet_check.status)
        self.assertEqual(
            ["beads", "git"], packet_check.details["factual_sections"]
        )

    def test_an_unrecognized_packet_verdict_is_unavailable_not_a_guess(self) -> None:
        provider = FED.RuntimeEvidenceProvider(ROOT_DIR, {})
        with mock.patch(
            "runtime_manager.evidence.collect_runtime_evidence",
            return_value={"overall": "chartreuse", "sections": {}},
        ):
            collected = {r.check_id: r for r in provider.collect()}
        self.assertEqual(STATUS_UNAVAILABLE, collected["evidence-packet"].status)
        self.assertIn("chartreuse", collected["evidence-packet"].cause)

    def test_a_missing_outer_script_is_reported_not_raised(self) -> None:
        provider = FED.OuterReconcileProvider(ROOT_DIR / "nowhere")
        report = FED.collect_health([provider], timeout_s=30)
        self.assertEqual(1, len(report.results))
        self.assertEqual(STATUS_UNAVAILABLE, report.results[0].status)
        self.assertTrue(report.results[0].cause)

    def test_the_default_providers_are_the_three_surfaces(self) -> None:
        providers = FED.default_providers(ROOT_DIR, {})
        self.assertEqual(
            [FED.PROVIDER_OUTER, FED.PROVIDER_STRUCTURE, FED.PROVIDER_RUNTIME],
            [provider.describe().provider_id for provider in providers],
        )
        for provider in providers:
            self.assertTrue(provider.describe().read_only)


class ReadOnlyBoundaryTests(unittest.TestCase):
    """`--all` reports; it never applies a fix."""

    def test_all_with_fix_is_refused(self) -> None:
        from runtime_manager import cli

        args = mock.Mock()
        args.format = "json"
        args.fix = True
        args.cwd = None
        emitted: list[dict] = []
        with mock.patch.object(cli, "emit_json", emitted.append):
            code = cli._handle_doctor_all(args, ROOT_DIR, {})
        self.assertNotEqual(0, code)
        self.assertEqual("DOCTOR_ALL_IS_READ_ONLY", emitted[0]["error"]["code"])

    def test_the_federation_module_runs_no_command(self) -> None:
        source = (ENV_MANAGER_DIR / "runtime_manager" / "health_federation.py").read_text(
            encoding="utf-8"
        )
        for banned in ("subprocess", "os.system", "popen", "shell=True"):
            self.assertNotIn(banned, source, banned)


if __name__ == "__main__":
    unittest.main()
