"""Contract tests for the `sbp test` front door (skillbox-sbp-test-front-door-1y29).

Slice 1 ships an explicit native verb, not the generic ``sbp-<cmd>`` PATH
resolver. What has to hold on day one:

* ``plan``/``lint`` are real and **read-only** -- an agent can inspect a repo's
  test contract without side effects.
* ``run``/``dispatch`` are declared but not implemented, and say so in a typed
  envelope rather than looking like a successful no-op.
* An unknown subcommand gets did-you-mean and exit 2.
* The verbs are registered (registry + atlas + capabilities) and classified in
  the DCG policy so agent hooks fail closed before an executor exists.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import dcg_policy as DP  # noqa: E402
from runtime_manager import sbp_test as ST  # noqa: E402
from runtime_manager.command_registry import default_registry  # noqa: E402

# Strict schema v1 (skillbox-sbp-test-manifest-v1-23t3): argv commands, explicit
# schema_version, declared groups.
MANIFEST = """\
schema_version: 1
units:
  unit-lint:
    command: [ruff, check, .]
  unit-py:
    command: [python3, -m, unittest, discover, -s, tests]
    timeout_s: 600
    services: [postgres]
    artifacts: [junit.xml]
groups:
  default: [unit-lint]
  full: [unit-lint, unit-py]
"""


def _run_manage(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, ".env-manager/manage.py", *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
    )


class ManifestFixtureMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / ".skillbox").mkdir()
        (self.repo / ".skillbox" / "test.yaml").write_text(MANIFEST, encoding="utf-8")

    def _tree_snapshot(self) -> set[tuple[str, int]]:
        """Every file under the fixture repo with its size."""
        return {
            (str(path.relative_to(self.repo)), path.stat().st_size)
            for path in self.repo.rglob("*")
            if path.is_file()
        }


class ReadOnlyVerbTests(ManifestFixtureMixin):
    def test_plan_resolves_the_default_group(self) -> None:
        payload = ST.plan_payload(self.repo)
        self.assertTrue(payload["ok"])
        self.assertEqual(["unit-lint"], [unit["id"] for unit in payload["units"]])

    def test_plan_resolves_a_named_group_in_declared_order(self) -> None:
        payload = ST.plan_payload(self.repo, group="full")
        self.assertEqual(["unit-lint", "unit-py"], [u["id"] for u in payload["units"]])
        unit = payload["units"][1]
        self.assertEqual(600, unit["timeout_s"])
        self.assertEqual(["postgres"], unit["services"])
        self.assertEqual(["junit.xml"], unit["artifacts"])

    def test_plan_and_lint_write_nothing(self) -> None:
        """The point of a read-only verb: prove it by diffing the tree."""
        before = self._tree_snapshot()
        ST.plan_payload(self.repo)
        ST.plan_payload(self.repo, group="full")
        ST.lint_payload(self.repo)
        ST.status_payload(self.repo)
        self.assertEqual(before, self._tree_snapshot())

    def test_lint_accepts_a_valid_manifest(self) -> None:
        payload = ST.lint_payload(self.repo)
        self.assertEqual([], payload["issues"])
        self.assertEqual(2, payload["unit_count"])

    def test_lint_reports_a_unit_without_a_command(self) -> None:
        (self.repo / ".skillbox" / "test.yaml").write_text(
            "schema_version: 1\nunits:\n  broken: {}\ngroups:\n  default: []\n",
            encoding="utf-8",
        )
        payload = ST.lint_payload(self.repo)
        self.assertFalse(payload["ok"])
        self.assertIn(
            "missing_command", [issue["code"] for issue in payload["issues"]]
        )

    def test_lint_reports_a_group_referencing_an_unknown_unit(self) -> None:
        (self.repo / ".skillbox" / "test.yaml").write_text(
            "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
            "groups:\n  default: [a, ghost]\n",
            encoding="utf-8",
        )
        payload = ST.lint_payload(self.repo)
        self.assertFalse(payload["ok"])
        self.assertIn(
            "unknown_group_member", [issue["code"] for issue in payload["issues"]]
        )

    def test_missing_manifest_is_a_typed_result_not_a_crash(self) -> None:
        empty = Path(tempfile.mkdtemp(dir=self._tmp.name))
        payload = ST.lint_payload(empty)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["manifest_present"])
        self.assertTrue(payload["next_actions"])

    def test_unknown_group_does_not_silently_plan_everything(self) -> None:
        payload = ST.plan_payload(self.repo, group="nope")
        self.assertFalse(payload["ok"])
        self.assertEqual([], payload["units"])
        self.assertIn("full", payload["known_groups"])


class ScoreVerbTests(ManifestFixtureMixin):
    """`score` joins the read-only set (skillbox-sbp-test-scorer-adapters-jyg2)."""

    def test_score_is_declared_read_only(self) -> None:
        self.assertIn("score", ST.READ_ONLY_VERBS)
        self.assertNotIn("score", ST.GATED_VERBS)
        self.assertNotIn("score", ST.WRITE_VERBS)

    def test_score_writes_nothing(self) -> None:
        before = self._tree_snapshot()
        ST.score_payload(self.repo)
        self.assertEqual(before, self._tree_snapshot())

    def test_score_does_not_require_a_manifest(self) -> None:
        """plan/lint need the contract; score reads the repo itself."""
        (self.repo / ".skillbox" / "test.yaml").unlink()
        (self.repo / "Makefile").write_text(".PHONY: test\ntest:\n\tpytest -q\n", encoding="utf-8")
        payload = ST.score_payload(self.repo)
        self.assertTrue(payload["ok"], payload.get("error"))
        self.assertFalse(payload["manifest_present"])

    def test_score_is_analysis_not_a_generated_manifest(self) -> None:
        self.assertTrue(ST.score_payload(self.repo)["analysis_only"])

    def test_status_lists_score_among_the_implemented_verbs(self) -> None:
        payload = ST.status_payload(self.repo)
        self.assertIn("score", payload["read_only_verbs"])
        self.assertIn("score", payload["implemented_verbs"])


class DeferredVerbTests(ManifestFixtureMixin):
    def test_run_and_dispatch_refuse_in_a_typed_way(self) -> None:
        for verb in ("run", "dispatch"):
            with self.subTest(verb=verb):
                payload = ST.deferred_payload(verb, self.repo)
                self.assertFalse(payload["ok"])
                self.assertEqual("not_implemented", payload["error_code"])

    def test_deferred_verbs_are_declared_but_not_implemented(self) -> None:
        self.assertEqual(("run", "dispatch"), ST.GATED_VERBS)
        for verb in ST.GATED_VERBS:
            self.assertIn(verb, ST.VERBS)
            self.assertNotIn(verb, ST.READ_ONLY_VERBS)


class UnknownSubcommandTests(ManifestFixtureMixin):
    def test_typo_gets_a_suggestion(self) -> None:
        payload = ST.unknown_verb_payload("pln", self.repo)
        self.assertFalse(payload["ok"])
        self.assertEqual("plan", payload["suggestion"])
        self.assertEqual("unknown_subcommand", payload["error_code"])

    def test_nonsense_still_lists_the_known_verbs(self) -> None:
        payload = ST.unknown_verb_payload("zzzzzz", self.repo)
        self.assertIsNone(payload["suggestion"])
        self.assertEqual(list(ST.VERBS), payload["known_verbs"])


class CliEnvelopeTests(unittest.TestCase):
    """End-to-end through manage.py: typed envelope and the exit-code ladder."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmp.name)
        (cls.repo / ".skillbox").mkdir()
        (cls.repo / ".skillbox" / "test.yaml").write_text(MANIFEST, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_bare_test_returns_a_stamped_envelope(self) -> None:
        result = _run_manage("test", "--cwd", str(self.repo), "--format", "json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(ST.SBP_TEST_SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual("test", payload["command"])

    def test_plan_and_lint_exit_zero_on_a_valid_manifest(self) -> None:
        for verb in ("plan", "lint"):
            with self.subTest(verb=verb):
                result = _run_manage(
                    "test", verb, "--cwd", str(self.repo), "--format", "json"
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue(json.loads(result.stdout)["ok"])

    def test_unknown_subcommand_exits_two_with_did_you_mean(self) -> None:
        result = _run_manage("test", "pln", "--cwd", str(self.repo), "--format", "json")
        self.assertEqual(2, result.returncode, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual("plan", payload["suggestion"])

    def test_score_exits_zero_and_keeps_the_bare_status_home(self) -> None:
        """Adding a verb must not move `sbp test` itself off the status summary."""
        scored = _run_manage("test", "score", "--cwd", str(self.repo), "--format", "json")
        self.assertEqual(0, scored.returncode, scored.stderr)
        self.assertEqual("score", json.loads(scored.stdout)["verb"])

        bare = _run_manage("test", "--cwd", str(self.repo), "--format", "json")
        self.assertEqual(0, bare.returncode, bare.stderr)
        self.assertEqual("status", json.loads(bare.stdout)["verb"])

    def test_gated_verbs_do_not_exit_zero(self) -> None:
        for verb in ("run", "dispatch"):
            with self.subTest(verb=verb):
                result = _run_manage(
                    "test", verb, "--cwd", str(self.repo), "--format", "json"
                )
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(
                    "not_implemented", json.loads(result.stdout)["error_code"]
                )


class RegistrationTests(unittest.TestCase):
    def test_registry_declares_the_test_command(self) -> None:
        spec = next(s for s in default_registry() if s.id == "runtime.test")
        self.assertEqual(("cli",), spec.surface)
        # local_write, not none: `capsule` admits an archive into the local
        # content-addressed store (skillbox-sbp-test-source-capsule-e1jj).
        # plan/lint remain read-only, proven separately by a tree-diff test.
        self.assertEqual("local_write", spec.side_effect)
        self.assertEqual("sbp", spec.owner_binary)
        self.assertEqual("manage.py", spec.entrypoint)

    def test_test_command_has_no_mcp_mirror(self) -> None:
        """The in-box MCP surface is frozen; this verb must not mirror into it."""
        spec = next(s for s in default_registry() if s.id == "runtime.test")
        self.assertIsNone(spec.mcp_tool)
        self.assertNotIn("mcp", spec.surface)

    def test_safe_first_try_examples_are_read_only_and_json(self) -> None:
        spec = next(s for s in default_registry() if s.id == "runtime.test")
        self.assertTrue(spec.examples)
        for example in spec.examples:
            self.assertIn("--format json", example)
            for gated in ST.GATED_VERBS:
                self.assertNotIn(f"test {gated}", example)

    def test_atlas_lists_the_test_verbs(self) -> None:
        sys.path.insert(0, str(ROOT_DIR / "scripts" / "lib"))
        import sbp_help_human  # noqa: PLC0415

        usages = [
            cmd.invocation
            for group in sbp_help_human.atlas("sbp")
            for cmd in group.cmds
        ]
        blob = "\n".join(usages)
        self.assertIn("sbp test", blob)
        self.assertTrue(
            any("test plan" in usage for usage in usages), f"atlas usages: {usages}"
        )
        self.assertTrue(any("test lint" in usage for usage in usages))

    def test_wrapper_capabilities_expose_test(self) -> None:
        wrapper = (ROOT_DIR / "scripts" / "sbp").read_text(encoding="utf-8")
        self.assertIn('"name": "test"', wrapper)


class DcgClassificationTests(unittest.TestCase):
    """Agent hooks must fail closed on execution verbs from day one."""

    def setUp(self) -> None:
        self.rule = next(
            rule
            for rule in DP.DEFAULT_BLOCK_RULES
            if rule.pattern == DP.SBP_TEST_EXECUTION_PATTERN
        )

    def test_execution_verbs_are_blocked(self) -> None:
        pattern = re.compile(self.rule.pattern)
        for command in ("sbp test run", "sbp test dispatch", "SBP TEST RUN --all"):
            with self.subTest(command=command):
                self.assertTrue(pattern.search(command), command)

    def test_read_only_verbs_are_not_blocked(self) -> None:
        pattern = re.compile(self.rule.pattern)
        for command in (
            "sbp test",
            "sbp test plan",
            "sbp test lint --format json",
            "sbp test plan --group full",
            "sbp test score --format json",
        ):
            with self.subTest(command=command):
                self.assertIsNone(pattern.search(command), command)

    def test_similar_commands_are_not_caught(self) -> None:
        """The rule must gate the verb, not anything containing 'test'."""
        pattern = re.compile(self.rule.pattern)
        for command in ("sbp testing run", "make test run", "sbp test-plan"):
            with self.subTest(command=command):
                self.assertIsNone(pattern.search(command), command)

    def test_block_reason_names_the_read_only_alternatives(self) -> None:
        self.assertIn("sbp test plan", self.rule.reason)
        self.assertIn("read-only", self.rule.reason)


if __name__ == "__main__":
    unittest.main()
