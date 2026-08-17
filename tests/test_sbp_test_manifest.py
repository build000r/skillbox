"""Strict `.skillbox/test.yaml` v1 schema/loader tests (skillbox-sbp-test-manifest-v1-23t3).

The loader is the compiler's only defence: everything downstream trusts that a
parsed manifest is well-formed. So the interesting tests are the adversarial
ones -- each fixture under ``tests/fixtures/sbp_test/`` isolates exactly one
defect, and its golden lint output is pinned.
"""
from __future__ import annotations

import json
import os
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
from runtime_manager import sbp_test_manifest as M  # noqa: E402

FIXTURES = ROOT_DIR / "tests" / "fixtures" / "sbp_test"
GOLDEN = FIXTURES / "golden"

REGEN_ENV = "REGEN_SBP_TEST_GOLDENS"


def _fixture(name: str) -> str:
    return (FIXTURES / f"{name}.yaml").read_text(encoding="utf-8")


def _codes(findings) -> list[str]:
    return sorted({finding.code for finding in findings})


class SchemaVersionTests(unittest.TestCase):
    def test_v1_is_the_frozen_supported_version(self) -> None:
        self.assertEqual(1, M.SCHEMA_VERSION)
        self.assertEqual((1,), M.SUPPORTED_SCHEMA_VERSIONS)

    def test_unknown_schema_version_is_refused(self) -> None:
        manifest, findings = M.parse_manifest(_fixture("adversarial_unknown_schema"))
        self.assertIsNone(manifest, "a future schema must not be parsed on a guess")
        self.assertEqual(["unknown_schema_version"], _codes(findings))

    def test_missing_schema_version_is_refused(self) -> None:
        manifest, findings = M.parse_manifest("units: {}\ngroups: {}\n")
        self.assertIsNone(manifest)
        self.assertEqual(["missing_schema_version"], _codes(findings))

    def test_schema_freeze_does_not_include_attempt_or_receipt_keys(self) -> None:
        """Frozen surface: attempt/receipt harden later, not in v1."""
        for reserved in ("attempt", "attempts", "receipt", "receipts"):
            self.assertNotIn(reserved, M.TOP_LEVEL_KEYS)
            self.assertNotIn(reserved, M.UNIT_KEYS)


class GoodManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest, self.findings = M.parse_manifest(_fixture("good"))

    def test_the_reference_manifest_is_clean(self) -> None:
        self.assertIsNotNone(self.manifest)
        self.assertEqual([], self.findings, _codes(self.findings))

    def test_every_v1_field_round_trips(self) -> None:
        unit = self.manifest.units["integration"]
        self.assertEqual(("python3", "-m", "pytest", "-q", "integration"), unit.command)
        self.assertEqual(("unit-fast",), unit.depends_on)
        self.assertEqual(("postgres", "redis"), unit.services)
        self.assertEqual("db", unit.resource_group)
        self.assertEqual("exclusive", unit.exclusivity)
        self.assertEqual(1800, unit.timeout_s)

        fast = self.manifest.units["unit-fast"]
        self.assertEqual("tests", fast.cwd)
        self.assertEqual([">=3.11"], [fast.requires["python"]])
        self.assertEqual(["linux", "darwin"], fast.requires["os"])
        self.assertEqual(("junit.xml",), fast.artifacts)

        lint = self.manifest.units["lint"]
        self.assertEqual("never", lint.cache)
        self.assertEqual(("CI", "NO_COLOR"), lint.env)

    def test_exclusivity_defaults_to_shared(self) -> None:
        self.assertEqual("shared", self.manifest.units["lint"].exclusivity)


class PlanCompilationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest, _ = M.parse_manifest(_fixture("good"))

    def test_default_group_compiles_in_declared_order(self) -> None:
        units, findings = M.compile_plan(self.manifest, "default")
        self.assertEqual([], findings)
        self.assertEqual(["lint", "unit-fast"], [u.id for u in units])

    def test_dependencies_are_pulled_in_before_their_dependent(self) -> None:
        units, findings = M.compile_plan(self.manifest, "full")
        self.assertEqual([], findings)
        ids = [u.id for u in units]
        self.assertLess(ids.index("unit-fast"), ids.index("integration"))

    def test_a_transitive_dependency_is_included_even_if_the_group_omits_it(self) -> None:
        """A group names what you want run, not the closure required to run it."""
        manifest, findings = M.parse_manifest(
            "schema_version: 1\n"
            "units:\n"
            "  a:\n    command: ['true']\n"
            "  b:\n    command: ['true']\n    depends_on: [a]\n"
            "groups:\n  default: [b]\n"
        )
        self.assertEqual([], findings)
        units, _ = M.compile_plan(manifest, "default")
        self.assertEqual(["a", "b"], [u.id for u in units])

    def test_unknown_group_is_refused_not_silently_emptied(self) -> None:
        units, findings = M.compile_plan(self.manifest, "nope")
        self.assertEqual([], units)
        self.assertEqual(["unknown_group"], _codes(findings))

    def test_compile_refuses_a_cyclic_graph(self) -> None:
        manifest, _ = M.parse_manifest(_fixture("adversarial_cycle"))
        units, findings = M.compile_plan(manifest, "default")
        self.assertEqual([], units)
        self.assertEqual(["dependency_cycle"], _codes(findings))


class AdversarialManifestTests(unittest.TestCase):
    """One fixture per defect; each must be caught, and caught cleanly."""

    def test_each_fixture_isolates_exactly_one_defect_code(self) -> None:
        expected = {
            "adversarial_ambiguous_group": ["ambiguous_group_membership"],
            "adversarial_cycle": ["dependency_cycle"],
            "adversarial_duplicate_id": ["duplicate_unit_id"],
            "adversarial_no_default_group": ["missing_default_group"],
            "adversarial_shell_string": ["command_not_argv"],
            "adversarial_undeclared_dep": ["undeclared_dependency"],
            "adversarial_unknown_keys": ["unknown_key"],
            "adversarial_unknown_schema": ["unknown_schema_version"],
            "adversarial_unsafe_cwd": ["unsafe_cwd"],
        }
        for name, codes in sorted(expected.items()):
            with self.subTest(fixture=name):
                _, findings = M.parse_manifest(_fixture(name))
                self.assertEqual(codes, _codes(findings))

    def test_duplicate_unit_id_is_caught_at_the_parser(self) -> None:
        """PyYAML keeps the LAST duplicate, so a later block would silently win."""
        manifest, findings = M.parse_manifest(_fixture("adversarial_duplicate_id"))
        self.assertIsNone(manifest)
        self.assertEqual(["duplicate_unit_id"], _codes(findings))

    def test_shell_string_command_is_refused_with_an_argv_hint(self) -> None:
        _, findings = M.parse_manifest(_fixture("adversarial_shell_string"))
        self.assertIn("argv", findings[0].message)
        self.assertIn("no shell", findings[0].message)

    def test_unsafe_cwd_covers_absolute_escape_and_post_normalization_escape(self) -> None:
        _, findings = M.parse_manifest(_fixture("adversarial_unsafe_cwd"))
        units = sorted(f.unit for f in findings)
        self.assertEqual(["absolute", "escapes", "sneaky"], units)

    def test_env_rejects_values_and_keeps_valid_names(self) -> None:
        manifest, findings = M.parse_manifest(_fixture("adversarial_env_values"))
        codes = _codes(findings)
        self.assertIn("env_value_supplied", codes)
        self.assertIn("invalid_env_name", codes)
        # The one legal name survives; the value-carrying entry never does.
        self.assertEqual(("OK_NAME",), manifest.units["leaky"].env)

    def test_a_declared_but_invalid_unit_is_not_also_reported_as_unknown(self) -> None:
        """No cascade: the real cause must not be buried under follow-on noise."""
        _, findings = M.parse_manifest(_fixture("adversarial_shell_string"))
        self.assertNotIn("unknown_group_member", _codes(findings))

    def test_self_dependency_is_a_cycle(self) -> None:
        _, findings = M.parse_manifest(
            "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
            "    depends_on: [a]\ngroups:\n  default: [a]\n"
        )
        self.assertIn("dependency_cycle", _codes(findings))

    def test_unknown_group_member_is_reported_when_never_declared(self) -> None:
        _, findings = M.parse_manifest(
            "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
            "groups:\n  default: [a, ghost]\n"
        )
        self.assertEqual(["unknown_group_member"], _codes(findings))

    def test_invalid_timeout_and_cache_and_exclusivity_are_typed(self) -> None:
        _, findings = M.parse_manifest(
            "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
            "    timeout_s: -5\n    cache: always\n    exclusivity: sometimes\n"
            "groups:\n  default: [a]\n"
        )
        self.assertEqual(
            ["invalid_cache", "invalid_exclusivity", "invalid_timeout"], _codes(findings)
        )

    def test_invalid_yaml_is_a_typed_finding_not_a_crash(self) -> None:
        manifest, findings = M.parse_manifest("schema_version: 1\nunits: [oops\n")
        self.assertIsNone(manifest)
        self.assertEqual(["invalid_yaml"], _codes(findings))


class GoldenLintTests(unittest.TestCase):
    """Pin the exact lint output per fixture.

    Regenerate deliberately with:
        REGEN_SBP_TEST_GOLDENS=1 python3 -m unittest tests.test_sbp_test_manifest
    """

    def test_every_fixture_matches_its_golden_lint_output(self) -> None:
        regen = bool(os.environ.get(REGEN_ENV))
        for path in sorted(FIXTURES.glob("*.yaml")):
            with self.subTest(fixture=path.stem):
                manifest, findings = M.parse_manifest(path.read_text(encoding="utf-8"))
                payload = {
                    "manifest_parsed": manifest is not None,
                    "unit_ids": sorted(manifest.units) if manifest else [],
                    "groups": (
                        {k: list(v) for k, v in sorted(manifest.groups.items())}
                        if manifest
                        else {}
                    ),
                    "findings": M.findings_payload(findings),
                }
                rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
                golden = GOLDEN / f"{path.stem}.lint.json"
                if regen:
                    golden.write_text(rendered, encoding="utf-8")
                    continue
                self.assertTrue(golden.is_file(), f"missing golden for {path.stem}")
                self.assertEqual(
                    golden.read_text(encoding="utf-8"),
                    rendered,
                    f"{path.stem} lint output drifted; regenerate with {REGEN_ENV}=1",
                )

    def test_goldens_and_fixtures_are_one_to_one(self) -> None:
        fixtures = {path.stem for path in FIXTURES.glob("*.yaml")}
        goldens = {path.name.removesuffix(".lint.json") for path in GOLDEN.glob("*.lint.json")}
        self.assertEqual(fixtures, goldens)


class DriftTests(unittest.TestCase):
    """EXIT_DRIFT is manifest-vs-reality. A red test is never drift."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / ".skillbox").mkdir()

    def _write(self, body: str) -> None:
        (self.repo / ".skillbox" / "test.yaml").write_text(body, encoding="utf-8")

    def test_missing_command_is_drift_not_a_schema_error(self) -> None:
        self._write(
            "schema_version: 1\nunits:\n  a:\n"
            "    command: [definitely-not-a-real-binary-xyz]\n"
            "groups:\n  default: [a]\n"
        )
        manifest, issues = M.load_manifest(self.repo)
        self.assertEqual([], issues, "manifest itself is well formed")
        drift = M.detect_drift(manifest, self.repo)
        self.assertEqual(["command_not_found"], _codes(drift))

    def test_missing_cwd_is_drift(self) -> None:
        self._write(
            "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
            "    cwd: nowhere\ngroups:\n  default: [a]\n"
        )
        manifest, _ = M.load_manifest(self.repo)
        self.assertEqual(["cwd_not_found"], _codes(M.detect_drift(manifest, self.repo)))

    def test_a_present_command_reports_no_drift(self) -> None:
        self._write(
            "schema_version: 1\nunits:\n  a:\n    command: [python3, --version]\n"
            "groups:\n  default: [a]\n"
        )
        manifest, _ = M.load_manifest(self.repo)
        self.assertEqual([], M.detect_drift(manifest, self.repo))


class ExitLadderTests(unittest.TestCase):
    """Schema break -> 1, manifest/reality mismatch -> 4 (EXIT_DRIFT), clean -> 0."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / ".skillbox").mkdir()

    def _write(self, body: str) -> None:
        (self.repo / ".skillbox" / "test.yaml").write_text(body, encoding="utf-8")

    def _lint(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, ".env-manager/manage.py", "test", "lint",
                "--cwd", str(self.repo), "--format", "json",
            ],
            cwd=ROOT_DIR, capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
        )

    def test_clean_manifest_exits_zero(self) -> None:
        self._write(
            "schema_version: 1\nunits:\n  a:\n    command: [python3, --version]\n"
            "groups:\n  default: [a]\n"
        )
        result = self._lint()
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_schema_break_exits_one(self) -> None:
        self._write("schema_version: 99\nunits: {}\ngroups: {}\n")
        result = self._lint()
        self.assertEqual(1, result.returncode, result.stdout)

    def test_reality_mismatch_exits_drift_four(self) -> None:
        self._write(
            "schema_version: 1\nunits:\n  a:\n"
            "    command: [definitely-not-a-real-binary-xyz]\n"
            "groups:\n  default: [a]\n"
        )
        result = self._lint()
        self.assertEqual(4, result.returncode, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual([], payload["issues"], "drift must not be reported as a schema issue")
        self.assertEqual("command_not_found", payload["drift"][0]["code"])


class SkillboxOwnManifestTests(unittest.TestCase):
    """This repo ships its own draft manifest; it must lint clean here."""

    def test_repo_manifest_exists_and_is_schema_clean(self) -> None:
        manifest, findings = M.load_manifest(ROOT_DIR)
        self.assertIsNotNone(manifest, "skillbox should ship a draft .skillbox/test.yaml")
        self.assertEqual([], findings, _codes(findings))

    def test_repo_manifest_declares_the_canonical_groups(self) -> None:
        manifest, _ = M.load_manifest(ROOT_DIR)
        self.assertIn(M.DEFAULT_GROUP, manifest.groups)
        self.assertIn(M.FULL_GROUP, manifest.groups)

    def test_front_door_reports_the_repo_manifest(self) -> None:
        payload = ST.status_payload(ROOT_DIR)
        self.assertTrue(payload["manifest_present"])
        self.assertTrue(payload["ok"], payload.get("issues"))


if __name__ == "__main__":
    unittest.main()
