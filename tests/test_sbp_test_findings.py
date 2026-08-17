"""``suite-readiness/v1`` registry + report contract tests (skillbox-sbp-test-finding-registry-yxm7).

Three things are being pinned, and they are pinned in different ways:

* the **registry** is frozen against a fixture, so renaming or dropping a code
  is a test failure rather than a silent API break for the refactoring skill;
* the **authority rules** (unknown never passes, only proven evidenced blockers
  gate, the rollup is advisory) are asserted as behaviour, including the cases
  where a naive implementation would quietly pass;
* the **report** is pinned byte-for-byte, because a report that is not
  deterministic cannot be compared before and after a refactor.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import sbp_test_findings as F  # noqa: E402

FIXTURES = ROOT_DIR / "tests" / "fixtures" / "sbp_test"
REGISTRY_FIXTURE = FIXTURES / "finding_registry.v1.json"
REPORT_GOLDEN = FIXTURES / "golden" / "readiness_report.v1.json"

REGEN_ENV = "REGEN_SBP_TEST_GOLDENS"


def _file(locator: str, detail: str | None = None) -> F.Evidence:
    return F.Evidence("file", locator, detail)


def sample_inputs() -> tuple[F.Subject, list[F.Finding], list[F.Cleared]]:
    """The golden report's input: every evidence state exercised at least once.

    Shaped after the duel's sweet-potato read (strong service primitives, real
    gaps in partitioning and composable proof) so the golden is a plausible
    report rather than a synthetic one.
    """
    subject = F.Subject(
        label="exemplar-repo",
        capsule_digest="sha256:0000000000000000000000000000000000000000000000000000000000000000",
    )
    findings = [
        F.Finding(
            "PATH_FRAGILE",
            "proven",
            evidence=(_file("Makefile:118", "cd ../../ && pytest"),),
            affected_units=("pytest-full", "browser-e2e"),
            proposed_fragment={"units": {"pytest-full": {"cwd": "packages/server"}}},
        ),
        F.Finding(
            "CROSS_MACHINE_PARTITION_MISSING",
            "proven",
            evidence=(
                F.Evidence("parsed_target", "make -qp:test", "one pytest invocation, no selection"),
            ),
            affected_units=("pytest-full",),
        ),
        F.Finding(
            "RECEIPT_NOT_COMPOSABLE",
            "likely",
            evidence=(_file("scripts/exact_tree_test_state.py:41", "whole-tree receipt only"),),
            severity="high",
        ),
        F.Finding(
            "TARGET_MONOLITHIC",
            "unknown",
            reason="the gate target is assembled by an included makefile this adapter does not parse",
        ),
        F.Finding(
            "SERVICE_REQUIREMENT_UNDERIVED",
            "blocked",
            reason="deciding this needs a bounded probe, and --probe was not granted",
        ),
        F.Finding(
            "PACKAGE_LANES_UNENUMERATED",
            "not_applicable",
            reason="the repo declares no package-manager test scripts",
        ),
    ]
    cleared = [
        F.Cleared(
            "SERVICE_ENDPOINT_STATIC",
            (_file("scripts/orb_test_dependencies.sh:64", "compose port -> env injection"),),
        ),
        F.Cleared(
            "SERVICE_IMAGES_UNPINNED",
            (_file("scripts/orb_test_dependencies.sh:12", "pgvector + redis pinned by digest"),),
        ),
        F.Cleared(
            "SERVICE_FREE_LANE_MISSING",
            (F.Evidence("parsed_target", "make -qp:pytest-unit", "no service dependency"),),
        ),
        F.Cleared(
            "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING",
            (_file("Makefile:203", "lock-free lane exists alongside the locking one"),),
        ),
    ]
    return subject, findings, cleared


class RegistryShapeTests(unittest.TestCase):
    def test_schema_id_is_frozen(self) -> None:
        self.assertEqual("suite-readiness/v1", F.SCHEMA_ID)
        self.assertEqual(1, F.SCHEMA_VERSION)

    def test_the_ten_axis_model_is_recorded_in_order(self) -> None:
        self.assertEqual(10, len(F.AXES))
        self.assertEqual(list(range(1, 11)), [axis.ordinal for axis in F.AXES])
        self.assertEqual(
            [
                "entrypoint_clarity",
                "selection_completeness",
                "workspace_isolation",
                "service_isolation",
                "concurrency_safety",
                "determinism",
                "resource_declaration",
                "observability",
                "source_fidelity",
                "failure_cleanup",
            ],
            [axis.id for axis in F.AXES],
        )

    def test_the_v1_subset_is_named_and_is_a_proper_subset(self) -> None:
        """v1 covers seven axes. The other three are named, not assumed clean."""
        self.assertEqual(7, len(F.V1_AXIS_IDS))
        self.assertEqual(
            {"resource_declaration", "source_fidelity", "failure_cleanup"},
            set(F.UNCOVERED_AXIS_IDS),
        )
        self.assertEqual(set(), F.V1_AXIS_IDS & F.UNCOVERED_AXIS_IDS)

    def test_every_code_binds_to_a_known_axis_and_vocabulary(self) -> None:
        for code, spec in F.CODES.items():
            with self.subTest(code=code):
                self.assertIn(spec.axis, F.AXES_BY_ID)
                self.assertIn(spec.default_severity, F.SEVERITIES)
                self.assertIn(spec.blocks, F.BLOCKS_VALUES)
                self.assertTrue(spec.invariant, "a code without an invariant is a label")
                self.assertEqual(code, code.upper(), "codes are SCREAMING_SNAKE")

    def test_the_renamed_codes_bind_invariants_not_one_repos_naming(self) -> None:
        """The two renames the duel's post-reveal consensus required."""
        lock = F.CODES["EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING"]
        self.assertEqual("LOCK_SEAM_MISSING", lock.duel_origin)
        self.assertNotIn("unlocked", lock.invariant, "the invariant is not a target-name match")

        partition = F.CODES["CROSS_MACHINE_PARTITION_MISSING"]
        self.assertEqual("SHARD_VOCAB_MISSING", partition.duel_origin)

        for retired in ("LOCK_SEAM_MISSING", "SHARD_VOCAB_MISSING"):
            self.assertNotIn(retired, F.CODES, "the duel's original spelling must not be emittable")

    def test_renamed_codes_keep_their_lineage(self) -> None:
        renamed = {code: spec.duel_origin for code, spec in F.CODES.items() if spec.duel_origin}
        self.assertEqual(
            {
                "SERVICE_FREE_LANE_MISSING": "UNIT_DB_FREE_MISSING",
                "SERVICE_REQUIREMENT_UNDERIVED": "MARKERS_HAND_ANNOTATED",
                "SERVICE_ENDPOINT_STATIC": "SERVICES_STATIC_PORT",
                "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING": "LOCK_SEAM_MISSING",
                "CROSS_MACHINE_PARTITION_MISSING": "SHARD_VOCAB_MISSING",
                "PACKAGE_LANES_UNENUMERATED": "JS_AGGREGATOR_MISSING",
            },
            renamed,
        )


class CodeRecipeContractTests(unittest.TestCase):
    """PC-ready-1: every emitted code has a recipe; every recipe maps to a code."""

    def test_the_mapping_is_one_to_one(self) -> None:
        self.assertEqual(len(F.CODES), len(F.RECIPE_IDS))
        self.assertEqual(
            len(F.CODES), len({spec.recipe_id for spec in F.CODES.values()})
        )

    def test_the_registrys_own_catalog_has_no_drift(self) -> None:
        self.assertEqual([], F.validate_recipe_catalog(F.RECIPE_IDS))

    def test_a_code_without_a_recipe_is_drift(self) -> None:
        catalog = set(F.RECIPE_IDS) - {"suite-refactor/repo-relative-paths"}
        drift = F.validate_recipe_catalog(catalog)
        self.assertEqual(["recipe_missing_for_code"], [d.kind for d in drift])
        self.assertEqual("PATH_FRAGILE", drift[0].subject)

    def test_a_recipe_without_a_code_is_drift(self) -> None:
        drift = F.validate_recipe_catalog(set(F.RECIPE_IDS) | {"suite-refactor/invented"})
        self.assertEqual(["recipe_without_code"], [d.kind for d in drift])
        self.assertEqual("suite-refactor/invented", drift[0].subject)

    def test_drift_is_reported_in_both_directions_at_once(self) -> None:
        catalog = (set(F.RECIPE_IDS) - {"suite-refactor/pin-service-images"}) | {"stale/recipe"}
        self.assertEqual(
            {"recipe_missing_for_code", "recipe_without_code"},
            {d.kind for d in F.validate_recipe_catalog(catalog)},
        )

    def test_an_unregistered_code_cannot_be_emitted_at_all(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            F.Finding("MADE_UP_CODE", "unknown", reason="x")
        self.assertIn("unknown finding code", str(ctx.exception))


class EvidenceStateTests(unittest.TestCase):
    """PC-ready-2: the five states, with evidence obligations that are structural."""

    def test_the_five_states_are_the_vocabulary(self) -> None:
        self.assertEqual(
            ("proven", "likely", "unknown", "blocked", "not_applicable"), F.STATUSES
        )

    def test_a_proven_finding_without_evidence_is_unrepresentable(self) -> None:
        with self.assertRaises(ValueError):
            F.Finding("PATH_FRAGILE", "proven")

    def test_a_likely_finding_without_evidence_is_unrepresentable(self) -> None:
        with self.assertRaises(ValueError):
            F.Finding("PATH_FRAGILE", "likely")

    def test_an_unknown_without_a_reason_is_unrepresentable(self) -> None:
        with self.assertRaises(ValueError):
            F.Finding("PATH_FRAGILE", "unknown")

    def test_a_pass_cannot_be_claimed_without_evidence(self) -> None:
        """The structural half of 'unknown never becomes a pass'."""
        with self.assertRaises(ValueError):
            F.Cleared("PATH_FRAGILE", ())

    def test_file_evidence_must_be_repo_relative_with_a_line(self) -> None:
        _file("Makefile:12")  # valid
        for bad in ("/abs/Makefile:12", "Makefile", "Makefile:notanumber"):
            with self.subTest(locator=bad), self.assertRaises(ValueError):
                F.Evidence("file", bad)

    def test_absence_is_evidenceable_without_a_file_line(self) -> None:
        """A missing target has no line number; that must not force a fabrication."""
        finding = F.Finding(
            "SERVICE_FREE_LANE_MISSING",
            "proven",
            evidence=(F.Evidence("absent", "make -qp: no service-free lane"),),
        )
        self.assertEqual("proven", finding.status)

    def test_severity_defaults_from_the_registry(self) -> None:
        self.assertEqual(
            F.CODES["PATH_FRAGILE"].default_severity,
            F.Finding("PATH_FRAGILE", "unknown", reason="r").severity,
        )


class ProposedFragmentTests(unittest.TestCase):
    def test_a_fragment_is_refused_for_codes_where_it_could_never_be_safe(self) -> None:
        with self.assertRaises(ValueError):
            F.Finding(
                "TARGET_MONOLITHIC",
                "proven",
                evidence=(F.Evidence("parsed_target", "make -qp:test"),),
                proposed_fragment={"units": {"invented": {"command": ["true"]}}},
            )

    def test_a_fragment_from_an_unknown_finding_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            F.Finding(
                "PATH_FRAGILE",
                "unknown",
                reason="unparsed include",
                proposed_fragment={"units": {"a": {"cwd": "tests"}}},
            )

    def test_a_fragment_is_allowed_where_the_registry_says_it_is_safe(self) -> None:
        finding = F.Finding(
            "PATH_FRAGILE",
            "proven",
            evidence=(F.Evidence("file", "Makefile:9"),),
            proposed_fragment={"units": {"a": {"cwd": "tests"}}},
        )
        self.assertEqual({"units": {"a": {"cwd": "tests"}}}, finding.to_payload()["proposed_fragment"])


class GateAuthorityTests(unittest.TestCase):
    """PC-ready-3: only named, evidenced hard blockers gate; the rollup is advisory."""

    def _report(self, *findings: F.Finding, cleared: tuple[F.Cleared, ...] = ()) -> dict:
        return F.build_report(F.Subject("t"), findings, cleared)

    def test_a_proven_blocker_gates_exactly_the_intents_it_denies(self) -> None:
        report = self._report(
            F.Finding("PATH_FRAGILE", "proven", evidence=(F.Evidence("file", "Makefile:9"),))
        )
        self.assertFalse(report["gates"]["remote"]["admitted"])
        self.assertEqual(["PATH_FRAGILE"], report["gates"]["remote"]["blocked_by"])
        self.assertTrue(report["gates"]["local"]["admitted"], "local execution is untouched by it")

    def test_a_likely_finding_never_gates(self) -> None:
        report = self._report(
            F.Finding("PATH_FRAGILE", "likely", evidence=(F.Evidence("file", "Makefile:9"),))
        )
        self.assertTrue(report["gates"]["remote"]["admitted"])
        self.assertEqual([], report["gates"]["remote"]["blocked_by"])

    def test_an_unknown_never_gates_but_is_visible_on_the_intent(self) -> None:
        """Admission at concurrency one stays possible; the unknown is not hidden."""
        report = self._report(F.Finding("PATH_FRAGILE", "unknown", reason="unparsed include"))
        self.assertTrue(report["gates"]["remote"]["admitted"])
        self.assertIn("PATH_FRAGILE", report["gates"]["remote"]["unproven_for_intent"])

    def test_an_optimization_only_finding_gates_nothing_even_when_proven(self) -> None:
        report = self._report(
            F.Finding(
                "TARGET_MONOLITHIC",
                "proven",
                evidence=(F.Evidence("parsed_target", "make -qp:test"),),
            )
        )
        self.assertEqual((), F.CODES["TARGET_MONOLITHIC"].denied_intents)
        for intent in F.INTENTS:
            self.assertTrue(report["gates"][intent]["admitted"], intent)

    def test_severity_is_not_authority(self) -> None:
        """Relabelling a finding 'high' must not turn it into a gate."""
        report = self._report(
            F.Finding(
                "TARGET_MONOLITHIC",
                "proven",
                evidence=(F.Evidence("parsed_target", "make -qp:test"),),
                severity="high",
            )
        )
        self.assertTrue(all(report["gates"][intent]["admitted"] for intent in F.INTENTS))

    def test_the_rollup_is_advisory_and_never_overrides_a_blocker(self) -> None:
        cleared = tuple(
            F.Cleared(code, (F.Evidence("file", "Makefile:1"),))
            for code in F.CODES
            if code != "PATH_FRAGILE"
        )
        report = self._report(
            F.Finding("PATH_FRAGILE", "proven", evidence=(F.Evidence("file", "Makefile:9"),)),
            cleared=cleared,
        )
        self.assertEqual(900, report["rollup"]["score"], "nine of ten codes clean")
        self.assertTrue(report["rollup"]["advisory"])
        self.assertEqual("named_evidenced_blockers", report["rollup"]["authority"])
        self.assertEqual("blocked", report[F.V1_READINESS_KEY])
        self.assertFalse(report["gates"]["remote"]["admitted"])

    def test_gate_intents_are_the_declared_vocabulary(self) -> None:
        with self.assertRaises(ValueError):
            F.Finding("PATH_FRAGILE", "unknown", reason="r").gates("teleport")


class UnknownNeverPassesTests(unittest.TestCase):
    def test_an_omitted_code_is_filled_in_as_unknown_not_as_clean(self) -> None:
        """Silence is the cheapest lie a scorer can tell, so it is not available."""
        report = F.build_report(F.Subject("t"))
        self.assertEqual(len(F.CODES), report["counts"]["unknown"])
        self.assertEqual(0, report["counts"]["cleared"])
        self.assertEqual(0, report["rollup"]["score"])
        self.assertEqual("bounded", report[F.V1_READINESS_KEY])

    def test_a_single_unknown_prevents_ready(self) -> None:
        cleared = tuple(
            F.Cleared(code, (F.Evidence("file", "Makefile:1"),))
            for code in F.CODES
            if code != "PATH_FRAGILE"
        )
        report = F.build_report(
            F.Subject("t"),
            [F.Finding("PATH_FRAGILE", "unknown", reason="unparsed include")],
            cleared,
        )
        self.assertEqual("bounded", report[F.V1_READINESS_KEY])
        self.assertEqual("bounded", report["axes"]["workspace_isolation"]["state"])

    def test_ready_requires_every_applicable_code_to_be_evidenced(self) -> None:
        cleared = tuple(
            F.Cleared(code, (F.Evidence("file", "Makefile:1"),)) for code in F.CODES
        )
        report = F.build_report(F.Subject("t"), (), cleared)
        self.assertEqual("ready", report[F.V1_READINESS_KEY])
        self.assertEqual(1000, report["rollup"]["score"])

    def test_uncovered_axes_are_never_rendered_as_clean(self) -> None:
        cleared = tuple(
            F.Cleared(code, (F.Evidence("file", "Makefile:1"),)) for code in F.CODES
        )
        report = F.build_report(F.Subject("t"), (), cleared)
        for axis_id in F.UNCOVERED_AXIS_IDS:
            self.assertEqual("not_covered_in_v1", report["axes"][axis_id]["state"], axis_id)
        self.assertEqual(sorted(F.UNCOVERED_AXIS_IDS), report["coverage"]["not_covered_in_v1"])

    def test_not_applicable_leaves_the_denominator(self) -> None:
        """A repo must not score points for owning fewer things that can break."""
        report = F.build_report(
            F.Subject("t"),
            [F.Finding("PACKAGE_LANES_UNENUMERATED", "not_applicable", reason="no packages")],
            [
                F.Cleared(code, (F.Evidence("file", "Makefile:1"),))
                for code in F.CODES
                if code != "PACKAGE_LANES_UNENUMERATED"
            ],
        )
        self.assertEqual(9, report["rollup"]["applicable_codes"])
        self.assertEqual(1000, report["rollup"]["score"])
        self.assertEqual("ready", report[F.V1_READINESS_KEY])

    def test_a_code_cannot_be_evaluated_twice_in_one_report(self) -> None:
        with self.assertRaises(ValueError):
            F.build_report(
                F.Subject("t"),
                [F.Finding("PATH_FRAGILE", "unknown", reason="a")],
                [F.Cleared("PATH_FRAGILE", (F.Evidence("file", "Makefile:1"),))],
            )


class ReportDeterminismTests(unittest.TestCase):
    def test_the_same_inputs_render_the_same_bytes(self) -> None:
        subject, findings, cleared = sample_inputs()
        first = F.report_json(F.build_report(subject, findings, cleared))
        second = F.report_json(F.build_report(subject, list(reversed(findings)), cleared))
        self.assertEqual(first, second, "finding order must not change the report")

    def test_findings_are_ordered_by_axis_then_code(self) -> None:
        subject, findings, cleared = sample_inputs()
        report = F.build_report(subject, findings, cleared)
        ordered = [
            (F.AXES_BY_ID[item["axis"]].ordinal, item["finding_code"])
            for item in report["findings"]
        ]
        self.assertEqual(sorted(ordered), ordered)

    def test_a_report_is_only_comparable_when_bound_to_the_same_bytes(self) -> None:
        subject, findings, cleared = sample_inputs()
        bound = F.build_report(subject, findings, cleared)
        other = F.build_report(F.Subject("exemplar-repo", "sha256:" + "1" * 64), findings, cleared)
        unbound = F.build_report(F.Subject("exemplar-repo"), findings, cleared)
        self.assertTrue(F.is_comparable(bound, bound))
        self.assertFalse(F.is_comparable(bound, other))
        self.assertFalse(F.is_comparable(unbound, unbound), "an unbound report proves nothing")


class GoldenTests(unittest.TestCase):
    """The frozen artifacts: the registry itself, and one full report."""

    def test_the_registry_matches_its_frozen_fixture(self) -> None:
        rendered = json.dumps(F.registry_payload(), indent=2, sort_keys=True) + "\n"
        if os.environ.get(REGEN_ENV):
            REGISTRY_FIXTURE.write_text(rendered, encoding="utf-8")
            return
        self.assertTrue(REGISTRY_FIXTURE.is_file(), "the v1 registry fixture is missing")
        self.assertEqual(
            REGISTRY_FIXTURE.read_text(encoding="utf-8"),
            rendered,
            f"the frozen registry drifted; regenerate with {REGEN_ENV}=1 and review the diff",
        )

    def test_the_readiness_report_matches_its_golden(self) -> None:
        subject, findings, cleared = sample_inputs()
        rendered = F.report_json(F.build_report(subject, findings, cleared))
        if os.environ.get(REGEN_ENV):
            REPORT_GOLDEN.write_text(rendered, encoding="utf-8")
            return
        self.assertTrue(REPORT_GOLDEN.is_file(), "the readiness report golden is missing")
        self.assertEqual(
            REPORT_GOLDEN.read_text(encoding="utf-8"),
            rendered,
            f"report output drifted; regenerate with {REGEN_ENV}=1",
        )

    def test_the_golden_exercises_every_evidence_state(self) -> None:
        golden = json.loads(REPORT_GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(
            set(F.STATUSES),
            {item["status"] for item in golden["findings"]},
            "a golden that never shows an unknown does not pin the rule that matters",
        )
        self.assertTrue(golden["cleared"], "and it must show an evidenced pass too")

    def test_the_golden_report_is_gated_by_a_named_blocker(self) -> None:
        golden = json.loads(REPORT_GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual("blocked", golden[F.V1_READINESS_KEY])
        self.assertEqual(["PATH_FRAGILE"], golden["gates"]["remote"]["blocked_by"])
        self.assertTrue(golden["gates"]["local"]["admitted"])

    def test_every_golden_finding_names_a_registered_code_and_recipe(self) -> None:
        golden = json.loads(REPORT_GOLDEN.read_text(encoding="utf-8"))
        for item in golden["findings"]:
            with self.subTest(code=item["finding_code"]):
                self.assertIn(item["finding_code"], F.CODES)
                self.assertEqual(F.CODES[item["finding_code"]].recipe_id, item["recipe_id"])
                self.assertIn(item["recipe_id"], F.RECIPE_IDS)

    def test_the_registry_fixture_names_the_ten_axis_model_and_the_v1_subset(self) -> None:
        fixture = json.loads(REGISTRY_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(10, fixture["axis_model"]["axes_total"])
        self.assertEqual(10, len(fixture["axis_model"]["axes"]))
        self.assertEqual(sorted(F.V1_AXIS_IDS), fixture["axis_model"]["v1_covered_axis_ids"])
        self.assertEqual(
            sorted(F.UNCOVERED_AXIS_IDS), fixture["axis_model"]["not_covered_in_v1_axis_ids"]
        )
        self.assertFalse(fixture["authority"]["unknown_passes"])
        self.assertFalse(fixture["authority"]["severity_gates"])
        self.assertEqual("advisory", fixture["authority"]["rollup"])


if __name__ == "__main__":
    unittest.main()
