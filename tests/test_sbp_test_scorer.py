"""`sbp test score` adapter tests (skillbox-sbp-test-scorer-adapters-jyg2).

The scorer's whole value is that it can be trusted unattended, so the tests are
organised around the three ways it could betray that:

* it could **change the repo it inspected** -- every adapter and the real CLI
  invocation are wrapped in a before/after tree snapshot (content hashes, not
  just names, so an in-place rewrite of the same size is caught);
* it could **guess** -- unsupported constructs must come back ``unknown`` with a
  manual-manifest next action, never as a clean read;
* it could **lie about what it found** -- fixture repos pin the verdicts,
  including the two the duel called out by name on the sweet-potato shape.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
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
from runtime_manager import sbp_test_scorer as S  # noqa: E402

FIXTURES = ROOT_DIR / "tests" / "fixtures" / "sbp_test" / "scorer"

GOOD = FIXTURES / "good"
BAD = FIXTURES / "bad"
SWEET_POTATO = FIXTURES / "sweet_potato_shaped"
SKILLBOX = FIXTURES / "skillbox_shaped"
UNSUPPORTED = FIXTURES / "unsupported"
EMPTY = FIXTURES / "empty"
MALFORMED = FIXTURES / "malformed"


def _tree_digest(root: Path) -> dict[str, str]:
    """Path -> content hash for every file. Catches same-size in-place edits."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _statuses(report: dict) -> dict[str, str]:
    states = {f["finding_code"]: f["status"] for f in report["findings"]}
    states.update({c["finding_code"]: "cleared" for c in report["cleared"]})
    return states


def _run_manage(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, ".env-manager/manage.py", *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
    )


class ReadOnlyGuaranteeTests(unittest.TestCase):
    """The claim an agent cannot verify for itself, so it is proven here."""

    def test_every_adapter_leaves_every_fixture_byte_identical(self) -> None:
        for fixture in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
            with self.subTest(fixture=fixture.name):
                before = _tree_digest(fixture)
                for adapter in (S.read_make, S.read_package, S.read_pytest, S.read_compose):
                    try:
                        adapter(fixture)
                    except S.ScorerRefusal:
                        pass
                try:
                    S.score_report(fixture)
                except S.ScorerRefusal:
                    pass
                self.assertEqual(before, _tree_digest(fixture))

    def test_the_real_cli_invocation_mutates_nothing(self) -> None:
        before = _tree_digest(GOOD)
        result = _run_manage("test", "score", "--cwd", str(GOOD), "--format", "json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, _tree_digest(GOOD))

    def test_the_default_path_declares_and_keeps_zero_execution(self) -> None:
        """No subprocess on the default path -- proven by breaking subprocess."""
        report = S.score_report(GOOD)
        self.assertFalse(report["provenance"]["executed_anything"])
        self.assertEqual(
            {"probed": False, "refused": "not_probed_by_default"},
            report["provenance"]["make_database"],
        )

        original = S.subprocess.run

        def _forbidden(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError(f"the scorer executed something: {args!r}")

        S.subprocess.run = _forbidden
        try:
            self.assertTrue(S.score_report(GOOD))
            self.assertTrue(S.score_report(SWEET_POTATO))
        finally:
            S.subprocess.run = original

    def test_no_network_module_is_imported_by_the_scorer(self) -> None:
        source = (ENV_MANAGER_DIR / "runtime_manager" / "sbp_test_scorer.py").read_text(
            encoding="utf-8"
        )
        for banned in ("import socket", "import urllib", "import requests", "import http"):
            self.assertNotIn(banned, source)


class DeterminismTests(unittest.TestCase):
    def test_repeated_scores_are_byte_identical(self) -> None:
        for fixture in (GOOD, BAD, SWEET_POTATO, SKILLBOX, UNSUPPORTED):
            with self.subTest(fixture=fixture.name):
                first = R.report_json(S.score_report(fixture))
                second = R.report_json(S.score_report(fixture))
                self.assertEqual(first, second)

    def test_evidence_locators_are_repo_relative(self) -> None:
        """An absolute path in evidence would leak this host into the report."""
        for fixture in (GOOD, BAD, SWEET_POTATO, SKILLBOX):
            report = S.score_report(fixture)
            for finding in report["findings"] + report["cleared"]:
                for evidence in finding["evidence"]:
                    with self.subTest(fixture=fixture.name, locator=evidence["locator"]):
                        self.assertFalse(evidence["locator"].startswith("/"))
                        self.assertNotIn(str(ROOT_DIR), evidence["locator"])

    def test_the_subject_label_does_not_embed_a_host_path(self) -> None:
        self.assertEqual("good", S.score_report(GOOD)["subject"]["label"])


class GoodRepoTests(unittest.TestCase):
    def test_a_well_factored_repo_clears_every_code(self) -> None:
        report = S.score_report(GOOD)
        self.assertEqual("ready", report[R.V1_READINESS_KEY])
        self.assertEqual(1000, report["rollup"]["score"])
        self.assertEqual(10, report["counts"]["cleared"])

    def test_every_intent_is_admitted(self) -> None:
        report = S.score_report(GOOD)
        for intent in R.INTENTS:
            self.assertTrue(report["gates"][intent]["admitted"], intent)

    def test_digest_pinned_images_and_dynamic_ports_are_recognised(self) -> None:
        states = _statuses(S.score_report(GOOD))
        self.assertEqual("cleared", states["SERVICE_IMAGES_UNPINNED"])
        self.assertEqual("cleared", states["SERVICE_ENDPOINT_STATIC"])

    def test_a_deriving_conftest_clears_the_marker_code(self) -> None:
        self.assertEqual("cleared", _statuses(S.score_report(GOOD))["SERVICE_REQUIREMENT_UNDERIVED"])

    def test_a_clean_repo_still_gets_a_next_action(self) -> None:
        """Handing an agent an empty list to interpret is its own failure."""
        actions = S.score_report(GOOD)["next_actions"]
        self.assertTrue(actions)
        self.assertIn("no v1 blockers", actions[0])


class BadRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = S.score_report(BAD)
        self.states = _statuses(self.report)

    def test_the_repo_is_blocked_with_named_blockers(self) -> None:
        self.assertEqual("blocked", self.report[R.V1_READINESS_KEY])
        self.assertIn("PATH_FRAGILE", self.report["gates"]["remote"]["blocked_by"])
        self.assertFalse(self.report["gates"]["remote"]["admitted"])

    def test_an_absolute_path_in_a_recipe_is_proven_with_a_line(self) -> None:
        finding = next(
            f for f in self.report["findings"] if f["finding_code"] == "PATH_FRAGILE"
        )
        self.assertEqual("proven", finding["status"])
        self.assertRegex(finding["evidence"][0]["locator"], r"^Makefile:\d+$")

    def test_a_chained_gate_is_monolithic(self) -> None:
        self.assertEqual("proven", self.states["TARGET_MONOLITHIC"])

    def test_floating_tags_and_fixed_ports_are_proven(self) -> None:
        self.assertEqual("proven", self.states["SERVICE_IMAGES_UNPINNED"])
        self.assertEqual("proven", self.states["SERVICE_ENDPOINT_STATIC"])

    def test_hand_annotated_markers_are_likely_not_proven(self) -> None:
        """We cannot prove hand-maintenance statically, so we must not claim to."""
        self.assertEqual("likely", self.states["SERVICE_REQUIREMENT_UNDERIVED"])

    def test_a_likely_finding_does_not_gate(self) -> None:
        for gate in self.report["gates"].values():
            self.assertNotIn("SERVICE_REQUIREMENT_UNDERIVED", gate["blocked_by"])


class SweetPotatoShapedTests(unittest.TestCase):
    """The two findings the duel named on this shape, plus the refusal."""

    def setUp(self) -> None:
        self.report = S.score_report(SWEET_POTATO)
        self.states = _statuses(self.report)

    def test_strong_service_primitives_are_credited(self) -> None:
        for code in (
            "SERVICE_IMAGES_UNPINNED",
            "SERVICE_ENDPOINT_STATIC",
            "SERVICE_REQUIREMENT_UNDERIVED",
        ):
            self.assertEqual("cleared", self.states[code], code)

    def test_cross_machine_partition_is_proven_missing(self) -> None:
        self.assertEqual("proven", self.states["CROSS_MACHINE_PARTITION_MISSING"])

    def test_xdist_is_not_accepted_as_a_cross_machine_partition(self) -> None:
        """`-n auto` shards inside one machine; that is the whole rename rationale."""
        finding = next(
            f
            for f in self.report["findings"]
            if f["finding_code"] == "CROSS_MACHINE_PARTITION_MISSING"
        )
        self.assertIn("in-process sharding", finding["evidence"][0]["detail"])

    def test_the_shared_dependency_lock_refuses_cross_group_concurrency(self) -> None:
        """A lock that is correct within a group still blocks independent groups."""
        finding = next(
            f
            for f in self.report["findings"]
            if f["finding_code"] == "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING"
        )
        self.assertEqual("proven", finding["status"])
        details = " ".join(e["detail"] or "" for e in finding["evidence"])
        self.assertIn("lock", details)
        self.assertIn("shared", details)

    def test_unreachable_package_lanes_are_not_credited_as_a_partition(self) -> None:
        """Clearing one code with the evidence of another is the subtle failure."""
        self.assertEqual("proven", self.states["PACKAGE_LANES_UNENUMERATED"])
        self.assertEqual("proven", self.states["CROSS_MACHINE_PARTITION_MISSING"])

    def test_a_manifestless_repo_is_told_to_declare_one(self) -> None:
        self.assertFalse(self.report["provenance"]["manifest_present"])
        self.assertIn("test.yaml", self.report["next_actions"][0])


class SkillboxShapedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = S.score_report(SKILLBOX)
        self.states = _statuses(self.report)

    def test_declared_units_make_the_suite_partitionable(self) -> None:
        self.assertEqual("cleared", self.states["CROSS_MACHINE_PARTITION_MISSING"])

    def test_per_unit_artifacts_clear_composable_proof(self) -> None:
        self.assertEqual("cleared", self.states["RECEIPT_NOT_COMPOSABLE"])

    def test_a_repo_with_no_services_marks_service_codes_not_applicable(self) -> None:
        for code in (
            "SERVICE_IMAGES_UNPINNED",
            "SERVICE_ENDPOINT_STATIC",
            "SERVICE_FREE_LANE_MISSING",
            "SERVICE_REQUIREMENT_UNDERIVED",
        ):
            self.assertEqual("not_applicable", self.states[code], code)

    def test_not_applicable_is_not_a_silent_pass(self) -> None:
        """It is stated, reasoned, and visible in the counts."""
        for finding in self.report["findings"]:
            if finding["status"] == "not_applicable":
                self.assertTrue(finding["reason"])
        self.assertEqual(5, self.report["counts"]["not_applicable"])


class UnsupportedConstructTests(unittest.TestCase):
    """Includes, `$(shell ...)`, pattern rules and recursive make: never guessed."""

    def setUp(self) -> None:
        self.report = S.score_report(UNSUPPORTED)
        self.states = _statuses(self.report)

    def test_hazards_are_named_individually(self) -> None:
        gaps = set(self.report["provenance"]["adapters"]["make"]["gaps"])
        self.assertEqual(
            {
                "include_directive",
                "shell_expansion",
                "shell_assignment",
                "pattern_rule",
                "recursive_make",
            },
            gaps,
        )

    def test_unreadable_constructs_become_unknown_not_clean(self) -> None:
        for code in ("PATH_FRAGILE", "TARGET_MONOLITHIC", "RECEIPT_NOT_COMPOSABLE"):
            self.assertEqual("unknown", self.states[code], code)

    def test_an_unreadable_makefile_cannot_certify_lock_freedom(self) -> None:
        """The lock could be behind the include we refused to follow."""
        self.assertEqual("unknown", self.states["EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING"])

    def test_every_unknown_carries_a_reason(self) -> None:
        for finding in self.report["findings"]:
            if finding["status"] == "unknown":
                self.assertTrue(finding["reason"], finding["finding_code"])

    def test_the_manual_manifest_next_action_is_offered(self) -> None:
        blob = " ".join(self.report["next_actions"])
        self.assertIn(".skillbox/test.yaml", blob)

    def test_unknowns_never_produce_a_ready_verdict(self) -> None:
        self.assertNotEqual("ready", self.report[R.V1_READINESS_KEY])


class NoManifestTests(unittest.TestCase):
    def test_scoring_works_without_a_manifest(self) -> None:
        report = S.score_report(BAD)
        self.assertFalse(report["provenance"]["manifest_present"])
        self.assertEqual("suite-readiness/v1", report["schema"])

    def test_analysis_is_never_presented_as_a_generated_manifest(self) -> None:
        payload = ST.score_payload(SWEET_POTATO)
        self.assertTrue(payload["analysis_only"])
        self.assertIn(
            "this analysis is not a manifest",
            " ".join(payload["next_actions"]),
        )

    def test_a_manifest_improves_the_evidence_rather_than_being_required(self) -> None:
        without = _statuses(S.score_report(SWEET_POTATO))
        with_manifest = _statuses(S.score_report(SKILLBOX))
        self.assertEqual("proven", without["CROSS_MACHINE_PARTITION_MISSING"])
        self.assertEqual("cleared", with_manifest["CROSS_MACHINE_PARTITION_MISSING"])


class MalformedInputTests(unittest.TestCase):
    def test_malformed_package_json_is_a_typed_refusal(self) -> None:
        with self.assertRaises(S.ScorerRefusal) as ctx:
            S.score_report(MALFORMED)
        self.assertEqual("malformed_package_json", ctx.exception.code)
        self.assertTrue(ctx.exception.next_actions)

    def test_malformed_input_reaches_the_caller_as_a_payload_not_a_traceback(self) -> None:
        payload = ST.score_payload(MALFORMED)
        self.assertFalse(payload["ok"])
        self.assertEqual("malformed_package_json", payload["error_code"])
        self.assertNotIn("Traceback", json.dumps(payload))

    def test_a_missing_directory_is_bad_input_not_an_internal_error(self) -> None:
        payload = ST.score_payload(FIXTURES / "does-not-exist")
        self.assertEqual("cwd_not_found", payload["error_code"])
        self.assertEqual("error", ST.score_exit_class(payload))

    def test_a_repo_with_no_test_surface_needs_input(self) -> None:
        payload = ST.score_payload(EMPTY)
        self.assertEqual("no_test_surface", payload["error_code"])
        self.assertEqual("needs_input", ST.score_exit_class(payload))

    def test_an_internal_failure_is_typed_and_carries_no_traceback(self) -> None:
        original = S.score_report

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic scorer bug")

        S.score_report = _boom
        try:
            payload = ST.score_payload(GOOD)
        finally:
            S.score_report = original
        self.assertEqual("internal_error", payload["error_code"])
        self.assertNotIn("synthetic scorer bug", json.dumps(payload))

    def test_an_oversized_config_is_refused_rather_than_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text("{}" + " " * S.MAX_READ_BYTES, encoding="utf-8")
            with self.assertRaises(S.ScorerRefusal) as ctx:
                S.score_report(repo)
            self.assertEqual("file_too_large", ctx.exception.code)


class MakeDatabaseProbeTests(unittest.TestCase):
    """`make -qp` exists, is hazard-gated, bounded, and off by default."""

    def test_the_cli_never_enables_the_probe(self) -> None:
        cli = (ENV_MANAGER_DIR / "runtime_manager" / "cli.py").read_text(encoding="utf-8")
        front = (ENV_MANAGER_DIR / "runtime_manager" / "sbp_test.py").read_text(encoding="utf-8")
        for source in (cli, front):
            self.assertNotIn("probe_make_database", source)

    def test_a_hazardous_makefile_is_refused_before_make_is_invoked(self) -> None:
        read = S.probe_make_database(UNSUPPORTED, S.read_make(UNSUPPORTED))
        database = read.facts["make_database"]
        self.assertFalse(database["probed"])
        self.assertTrue(database["refused"].startswith("hazards:"))

    @unittest.skipUnless(shutil.which("make"), "make is not on PATH here")
    def test_a_clean_makefile_can_be_probed_and_nothing_changes(self) -> None:
        before = _tree_digest(GOOD)
        read = S.probe_make_database(GOOD, S.read_make(GOOD))
        self.assertTrue(read.facts["make_database"]["probed"])
        self.assertIn("test-unit", read.facts["database_targets"])
        self.assertEqual(before, _tree_digest(GOOD))

    def test_the_probe_is_bounded(self) -> None:
        self.assertLessEqual(S.MAKE_DB_TIMEOUT_S, 10)
        self.assertLessEqual(S.MAKE_DB_MAX_BYTES, 8 * 1024 * 1024)

    def test_enrichment_never_erases_a_recorded_gap(self) -> None:
        static = S.read_make(UNSUPPORTED)
        probed = S.probe_make_database(UNSUPPORTED, static)
        self.assertEqual(set(static.gaps), set(static.gaps) & set(probed.gaps))


class CliUxTests(unittest.TestCase):
    """Agent-facing and human-facing surfaces of the same run."""

    def test_json_is_deterministic_across_invocations(self) -> None:
        first = _run_manage("test", "score", "--cwd", str(BAD), "--format", "json")
        second = _run_manage("test", "score", "--cwd", str(BAD), "--format", "json")
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(first.stdout, second.stdout)

    def test_json_carries_findings_coverage_gates_and_rollup(self) -> None:
        result = _run_manage("test", "score", "--cwd", str(BAD), "--format", "json")
        report = json.loads(result.stdout)["report"]
        for key in ("findings", "coverage", "gates", "rollup", "next_actions"):
            self.assertIn(key, report)
        self.assertTrue(report["rollup"]["advisory"])

    def test_a_scored_repo_full_of_blockers_still_exits_zero(self) -> None:
        """Findings are data. Exiting nonzero here trains agents to stop reading."""
        result = _run_manage("test", "score", "--cwd", str(BAD), "--format", "json")
        self.assertEqual(0, result.returncode)
        self.assertEqual("blocked", json.loads(result.stdout)["report"][R.V1_READINESS_KEY])

    def test_no_test_surface_exits_needs_input_with_an_action(self) -> None:
        result = _run_manage("test", "score", "--cwd", str(EMPTY), "--format", "json")
        self.assertEqual(3, result.returncode, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual("no_test_surface", payload["error_code"])
        self.assertTrue(payload["next_actions"])

    def test_malformed_input_exits_one_without_leaking_stderr_onto_stdout(self) -> None:
        result = _run_manage("test", "score", "--cwd", str(MALFORMED), "--format", "json")
        self.assertEqual(1, result.returncode)
        payload = json.loads(result.stdout)
        self.assertEqual("malformed_package_json", payload["error_code"])
        self.assertNotIn("Traceback", result.stdout)

    def test_text_output_states_the_empty_case_instead_of_printing_nothing(self) -> None:
        result = _run_manage("test", "score", "--cwd", str(GOOD), "--format", "text")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("blockers: none (0 proven)", result.stdout)
        self.assertIn("coverage:", result.stdout)
        self.assertIn("not covered:", result.stdout)

    def test_text_output_labels_unproven_findings_without_implying_a_block(self) -> None:
        result = _run_manage(
            "test", "score", "--cwd", str(UNSUPPORTED), "--format", "text"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("remote: admitted unproven=[PATH_FRAGILE]", result.stdout)
        self.assertIn(
            "parallel: admitted unproven=[EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING, "
            "RECEIPT_NOT_COMPOSABLE]",
            result.stdout,
        )

    def test_text_output_names_blockers_and_gates(self) -> None:
        result = _run_manage("test", "score", "--cwd", str(BAD), "--format", "text")
        self.assertIn("PATH_FRAGILE", result.stdout)
        self.assertIn("remote: blocked", result.stdout)

    def test_text_error_output_is_actionable(self) -> None:
        result = _run_manage("test", "score", "--cwd", str(EMPTY), "--format", "text")
        self.assertEqual(3, result.returncode)
        self.assertIn("error:", result.stdout)
        self.assertIn("next:", result.stdout)

    def test_score_never_prompts(self) -> None:
        """stdin closed: an interactive prompt would hang or crash, not pass."""
        result = subprocess.run(
            [
                sys.executable, ".env-manager/manage.py", "test", "score",
                "--cwd", str(GOOD), "--format", "json",
            ],
            cwd=ROOT_DIR, capture_output=True, text=True, check=False,
            stdin=subprocess.DEVNULL, timeout=120,
            env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_scoring_this_repo_itself_works(self) -> None:
        """The zeroth consumer: skillbox must be scoreable without special-casing."""
        result = _run_manage("test", "score", "--cwd", str(ROOT_DIR), "--format", "json")
        self.assertEqual(0, result.returncode, result.stderr[:400])
        report = json.loads(result.stdout)["report"]
        self.assertEqual("suite-readiness/v1", report["schema"])
        self.assertTrue(report["provenance"]["manifest_present"])


class RegistryContractTests(unittest.TestCase):
    """The scorer may emit only what the registry defines."""

    def test_every_emitted_code_is_registered_with_a_recipe(self) -> None:
        for fixture in (GOOD, BAD, SWEET_POTATO, SKILLBOX, UNSUPPORTED):
            report = S.score_report(fixture)
            for item in report["findings"] + report["cleared"]:
                with self.subTest(fixture=fixture.name, code=item["finding_code"]):
                    self.assertIn(item["finding_code"], R.CODES)
        self.assertEqual([], R.validate_recipe_catalog(R.RECIPE_IDS))

    def test_the_scorer_evaluates_every_registered_code(self) -> None:
        """A code the adapters forget still appears -- as unknown, never absent."""
        report = S.score_report(GOOD)
        seen = set(_statuses(report))
        self.assertEqual(set(R.CODES), seen)

    def test_the_scorer_defines_no_codes_of_its_own(self) -> None:
        source = (ENV_MANAGER_DIR / "runtime_manager" / "sbp_test_scorer.py").read_text(
            encoding="utf-8"
        )
        for code in R.CODES:
            self.assertIn(code, source, "codes are referenced, not redefined")
        self.assertNotIn("CodeSpec(", source)

    def test_uncovered_axes_stay_uncovered(self) -> None:
        report = S.score_report(GOOD)
        self.assertEqual(
            sorted(R.UNCOVERED_AXIS_IDS), report["coverage"]["not_covered_in_v1"]
        )
        for axis_id in R.UNCOVERED_AXIS_IDS:
            self.assertEqual("not_covered_in_v1", report["axes"][axis_id]["state"])


class BeforeAfterComparisonTests(unittest.TestCase):
    def test_reports_are_comparable_only_when_bound_to_the_same_bytes(self) -> None:
        report = S.score_report(GOOD)
        self.assertFalse(S.compare(report, report)["comparable"])

    def test_a_score_delta_is_reported_alongside_comparability(self) -> None:
        delta = S.compare(S.score_report(BAD), S.score_report(GOOD))
        self.assertGreater(delta["score_delta"], 0)
        self.assertFalse(delta["comparable"])


if __name__ == "__main__":
    unittest.main()
