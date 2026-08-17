"""test-plan/v1 compiler tests (skillbox-sbp-test-plan-compiler-er74).

The plan is the execution authority, so the properties under test are the ones
that make it trustworthy: it is deterministic, it refuses rather than degrades,
it explains itself, and compiling it changes nothing on disk.

One claim is pinned deliberately: ``critical_path`` in
``agent_graph_algorithms`` is node-count-only, so this plan must never present a
timeout-weighted estimate. ``NoFabricatedEstimateTests`` guards that.
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

from runtime_manager import agent_graph_algorithms as GA  # noqa: E402
from runtime_manager import sbp_test as ST  # noqa: E402
from runtime_manager import sbp_test_plan as P  # noqa: E402

GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")

MANIFEST = """\
schema_version: 1
units:
  lint:
    command: [python3, --version]
    timeout_s: 120
    env: [CI]
  unit:
    command: [python3, --version]
    cwd: tests
    timeout_s: 600
    resource_group: cpu
    requires:
      os: [linux, darwin]
      caps: [python]
    artifacts: [junit.xml]
  integration:
    command: [python3, --version]
    depends_on: [unit]
    timeout_s: 900
    resource_group: db
    exclusivity: exclusive
    services: [postgres]
groups:
  default: [lint, unit]
  full: [lint, unit, integration]
"""


class PlanRepoMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".skillbox").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "keep.txt").write_text("x\n", encoding="utf-8")
        self._write(MANIFEST)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=GIT_ENV)
        for args in (
            ["config", "user.email", "plan@example.invalid"],
            ["config", "user.name", "Plan Test"],
            ["config", "commit.gpgsign", "false"],
        ):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=GIT_ENV)
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=GIT_ENV)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", "base"], check=True, env=GIT_ENV
        )

    def _write(self, body: str) -> None:
        (self.repo / ".skillbox" / "test.yaml").write_text(body, encoding="utf-8")

    def _compile(self, group: str = "full", **kwargs) -> P.Plan:
        return P.compile_plan(self.repo, group=group, **kwargs)


class DeterminismTests(PlanRepoMixin):
    def test_same_tree_and_manifest_compile_byte_identically(self) -> None:
        first = self._compile()
        second = self._compile()
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(P.canonical_json(first.content), P.canonical_json(second.content))

    def test_digest_covers_the_content_exactly(self) -> None:
        plan = self._compile()
        self.assertEqual(plan.digest, P.compute_plan_digest(plan.content))

    def test_a_manifest_change_moves_the_plan_digest(self) -> None:
        before = self._compile().digest
        self._write(MANIFEST.replace("timeout_s: 120", "timeout_s: 121"))
        self.assertNotEqual(before, self._compile().digest)

    def test_a_different_group_is_a_different_plan(self) -> None:
        self.assertNotEqual(self._compile("full").digest, self._compile("default").digest)

    def test_plan_carries_no_absolute_host_paths(self) -> None:
        """A host path would make the digest host-specific and leak the operator."""
        blob = P.canonical_json(self._compile().content)
        self.assertNotIn(str(self.repo), blob)
        self.assertNotIn(str(Path.home()), blob)

    def test_source_digests_are_bound_into_the_plan(self) -> None:
        source = {"source_tree_oid": "a" * 40, "capsule_manifest_sha256": "b" * 64,
                  "archive_sha256": "c" * 64}
        plan = self._compile(source_digests=source)
        self.assertEqual(source, plan.content["source"])
        other = self._compile(source_digests={**source, "archive_sha256": "d" * 64})
        self.assertNotEqual(plan.digest, other.digest, "source is part of plan identity")


class SealedContentTests(PlanRepoMixin):
    def test_plan_declares_compiler_and_runner_protocol_versions(self) -> None:
        content = self._compile().content
        self.assertEqual(P.PLAN_SCHEMA, content["schema"])
        self.assertEqual(P.COMPILER_VERSION, content["compiler_version"])
        self.assertEqual(P.RUNNER_PROTOCOL_VERSION, content["runner_protocol_version"])

    def test_plan_carries_the_manifest_digest(self) -> None:
        content = self._compile().content
        self.assertEqual(64, len(content["manifest_digest"]))

    def test_units_carry_argv_cwd_timeouts_needs_and_artifact_rules(self) -> None:
        units = {u["id"]: u for u in self._compile().content["units"]}
        unit = units["unit"]
        self.assertEqual(["python3", "--version"], unit["argv"])
        self.assertEqual("tests", unit["cwd"])
        self.assertEqual(600, unit["timeout_s"])
        self.assertEqual(["junit.xml"], unit["artifacts"]["collect"])
        self.assertEqual(["linux", "darwin"], unit["needs"]["os"])
        self.assertEqual(["python"], unit["needs"]["caps"])
        self.assertEqual("cpu", unit["needs"]["resource_group"])
        self.assertEqual(["postgres"], units["integration"]["needs"]["services"])
        self.assertEqual("exclusive", units["integration"]["needs"]["exclusivity"])

    def test_edges_are_recorded_dependent_to_dependency(self) -> None:
        edges = self._compile().content["edges"]
        self.assertIn(
            {"source": "integration", "target": "unit", "kind": "depends_on"}, edges
        )

    def test_stable_unit_ids_and_deterministic_topological_order(self) -> None:
        content = self._compile().content
        self.assertEqual(["lint", "unit", "integration"], content["order"])
        self.assertEqual([["lint", "unit"], ["integration"]], content["waves"])

    def test_policy_decisions_are_recorded_in_the_plan(self) -> None:
        policy = self._compile().content["policy"]
        self.assertTrue(policy["refuses_invalid_manifest"])
        self.assertTrue(policy["sealed_before_placement"])
        self.assertFalse(policy["workers_reread_repo"])


class ConcurrencyCeilingTests(PlanRepoMixin):
    def test_ceiling_counts_parallelisable_slots_per_wave(self) -> None:
        # wave 0 = lint (no group) + unit (group cpu) => 2 slots.
        self.assertEqual(2, self._compile().content["concurrency_ceiling"])

    def test_exclusive_unit_forces_a_wave_of_one(self) -> None:
        self.assertEqual(1, P._wave_concurrency(["integration"], self._units()))

    def test_shared_resource_group_contributes_one_slot(self) -> None:
        self._write(
            "schema_version: 1\nunits:\n"
            "  a:\n    command: ['true']\n    resource_group: db\n"
            "  b:\n    command: ['true']\n    resource_group: db\n"
            "groups:\n  default: [a, b]\n"
        )
        self.assertEqual(1, self._compile("default").content["concurrency_ceiling"])

    def _units(self):
        from runtime_manager import sbp_test_manifest as M

        manifest, _ = M.load_manifest(self.repo)
        return manifest.units


class ExplainabilityTests(PlanRepoMixin):
    def test_every_unit_says_whether_it_is_runnable_and_why(self) -> None:
        for unit in self._compile().content["units"]:
            self.assertIn("runnable", unit)
            self.assertTrue(unit["reason"], f"{unit['id']} has no reason")

    def test_summary_counts_runnable_and_skipped(self) -> None:
        summary = self._compile().content["summary"]
        self.assertEqual(3, summary["unit_count"])
        self.assertEqual(3, summary["runnable_count"])
        self.assertEqual(0, summary["skipped_count"])
        self.assertEqual(2, summary["wave_count"])

    def test_units_without_a_timeout_are_named_as_a_policy_decision(self) -> None:
        self._write(
            "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
            "groups:\n  default: [a]\n"
        )
        self.assertEqual(["a"], self._compile("default").content["policy"]["units_without_timeout"])


class RefusalTests(PlanRepoMixin):
    def test_invalid_manifest_refuses_rather_than_compiling_partially(self) -> None:
        self._write("schema_version: 99\nunits: {}\ngroups: {}\n")
        with self.assertRaises(P.PlanRefusal) as ctx:
            self._compile("default")
        self.assertEqual("manifest_invalid", ctx.exception.code)
        self.assertTrue(ctx.exception.findings, "refusal must carry the findings")

    def test_every_manifest_leaf_error_class_refuses_compilation(self) -> None:
        """The compile refusals are exactly the schema leaf's error classes."""
        cases = {
            "cycle": "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
                     "    depends_on: [b]\n  b:\n    command: ['true']\n"
                     "    depends_on: [a]\ngroups:\n  default: [a]\n",
            "undeclared_dep": "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
                              "    depends_on: [ghost]\ngroups:\n  default: [a]\n",
            "unsafe_cwd": "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
                          "    cwd: ../../etc\ngroups:\n  default: [a]\n",
            "ambiguous_group": "schema_version: 1\nunits:\n  a:\n    command: ['true']\n"
                               "groups:\n  default: [a, a]\n",
            "shell_string": "schema_version: 1\nunits:\n  a:\n    command: 'pytest -q'\n"
                            "groups:\n  default: [a]\n",
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self._write(body)
                with self.assertRaises(P.PlanRefusal):
                    self._compile("default")

    def test_unknown_group_refuses(self) -> None:
        with self.assertRaises(P.PlanRefusal) as ctx:
            self._compile("nope")
        self.assertEqual("group_uncompilable", ctx.exception.code)

    def test_refusal_payload_is_typed(self) -> None:
        self._write("schema_version: 99\nunits: {}\ngroups: {}\n")
        with self.assertRaises(P.PlanRefusal) as ctx:
            self._compile("default")
        payload = ctx.exception.to_payload()
        self.assertFalse(payload["ok"])
        self.assertIn("findings", payload)


class NoFabricatedEstimateTests(PlanRepoMixin):
    """critical_path is node-count-only; the plan must not imply otherwise."""

    def test_underlying_critical_path_is_not_timeout_weighted(self) -> None:
        graph = {
            "nodes": [{"id": "a", "kind": "test_unit"}, {"id": "b", "kind": "test_unit"}],
            "edges": [{"source": "b", "target": "a", "kind": "depends_on"}],
        }
        # Two nodes in a chain -> length 2, regardless of any timeout anywhere.
        self.assertEqual(2, GA.critical_path(graph, edge_kinds=("depends_on",))["length"])

    def test_dependency_depth_is_a_node_count_not_seconds(self) -> None:
        content = self._compile().content
        self.assertEqual(2, content["dependency_depth"])  # unit -> integration
        self.assertNotEqual(content["dependency_depth"], content["timeout_ceiling_s"])

    def test_timeout_ceiling_is_the_sum_of_declared_ceilings(self) -> None:
        self.assertEqual(120 + 600 + 900, self._compile().content["timeout_ceiling_s"])

    def test_plan_publishes_no_runtime_estimate(self) -> None:
        content = self._compile().content
        self.assertIsNone(content["estimates"])
        blob = P.canonical_json(content).lower()
        for forbidden in ("estimated_duration", "eta", "predicted", "forecast"):
            self.assertNotIn(forbidden, blob)


class GraphAlgorithmReuseTests(PlanRepoMixin):
    def test_waves_match_topological_layers(self) -> None:
        from runtime_manager import sbp_test_manifest as M

        manifest, _ = M.load_manifest(self.repo)
        ordered, _ = M.compile_plan(manifest, "full")
        graph = P._graph_payload({u.id: u for u in ordered})
        topo = GA.topological_layers(graph, edge_kinds=("depends_on",))
        self.assertEqual(
            [sorted(layer) for layer in topo["layers"]], self._compile().content["waves"]
        )

    def test_blast_radius_supplies_downstream_skip_explanations(self) -> None:
        graph = {
            "nodes": [{"id": n, "kind": "test_unit"} for n in ("a", "b", "c")],
            "edges": [
                {"source": "b", "target": "a", "kind": "depends_on"},
                {"source": "c", "target": "b", "kind": "depends_on"},
            ],
        }
        radius = GA.blast_radius(graph, "a", edge_kinds=("depends_on",))
        # `affected` is a list of RECORDS, not bare ids -- pinning that here
        # because reading it as ids makes the membership test silently empty.
        self.assertEqual(["b", "c"], sorted(item["node_id"] for item in radius["affected"]))

    def test_cycle_defence_in_depth_marks_units_unrunnable(self) -> None:
        """The manifest leaf refuses cycles first, so this path is unreachable
        through a valid manifest -- which is exactly how a bug in it hides.
        Force it and prove the skip explanation is real."""
        from unittest import mock

        real = GA.topological_layers

        def fake(graph, **kwargs):
            result = dict(real(graph, **kwargs))
            result["cycle_nodes"] = ["unit"]
            result["blocked_by_cycle_nodes"] = ["unit"]
            result["ok"] = False
            return result

        with mock.patch.object(P.GA, "topological_layers", side_effect=fake):
            content = self._compile().content

        units = {u["id"]: u for u in content["units"]}
        self.assertFalse(units["unit"]["runnable"])
        self.assertIn("cycle", units["unit"]["reason"])
        # integration depends on unit, so blast_radius must implicate it too.
        self.assertFalse(units["integration"]["runnable"])
        self.assertEqual(["unit"], units["integration"]["blocked_by"])
        self.assertTrue(units["lint"]["runnable"], "an unrelated unit stays runnable")
        self.assertEqual(2, content["summary"]["skipped_count"])


class ProjectionTests(PlanRepoMixin):
    def test_projection_carries_what_a_worker_needs(self) -> None:
        plan = self._compile()
        proj = P.projection(plan, "unit")
        self.assertEqual(plan.digest, proj["plan_digest"])
        self.assertEqual(["python3", "--version"], proj["unit"]["argv"])
        self.assertEqual("tests", proj["unit"]["cwd"])

    def test_projection_does_not_hand_the_worker_the_repo(self) -> None:
        """Workers receive projections and never re-read the repository."""
        blob = json.dumps(P.projection(self._compile(), "unit"))
        self.assertNotIn(str(self.repo), blob)
        self.assertNotIn("manifest_digest", blob)

    def test_projection_of_an_unknown_unit_refuses(self) -> None:
        with self.assertRaises(P.PlanRefusal):
            P.projection(self._compile(), "ghost")


class ZeroSideEffectTests(PlanRepoMixin):
    def _snapshot(self) -> set[tuple[str, int]]:
        return {
            (str(p.relative_to(self.repo)), p.stat().st_size)
            for p in self.repo.rglob("*")
            if p.is_file()
        }

    def test_compiling_a_plan_writes_nothing(self) -> None:
        before = self._snapshot()
        self._compile()
        self._compile("default")
        self.assertEqual(before, self._snapshot())

    def test_plan_mode_does_not_create_a_capsule_store(self) -> None:
        """Plan needs capsule digests but must not admit or create the store."""
        ST.plan_payload(self.repo, group="full")
        store = self.repo / ".skillbox-state" / "test-capsules"
        self.assertFalse(store.exists(), "plan mode must not create the capsule store")

    def test_front_door_plan_writes_nothing(self) -> None:
        before = self._snapshot()
        ST.plan_payload(self.repo, group="full")
        ST.plan_payload(self.repo)
        self.assertEqual(before, self._snapshot())

    def test_git_state_is_untouched_by_planning(self) -> None:
        before = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain=v1"],
            capture_output=True, text=True, check=True, env=GIT_ENV,
        ).stdout
        ST.plan_payload(self.repo, group="full")
        after = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain=v1"],
            capture_output=True, text=True, check=True, env=GIT_ENV,
        ).stdout
        self.assertEqual(before, after)


class FrontDoorPlanTests(PlanRepoMixin):
    def test_plan_payload_embeds_the_sealed_plan_and_digest(self) -> None:
        payload = ST.plan_payload(self.repo, group="full")
        self.assertTrue(payload["ok"], payload.get("issues"))
        self.assertEqual(payload["plan_digest"], payload["plan"]["plan_digest"])
        self.assertEqual(P.PLAN_SCHEMA, payload["plan"]["schema"])

    def test_plan_payload_is_stable_across_invocations(self) -> None:
        first = ST.plan_payload(self.repo, group="full")["plan_digest"]
        second = ST.plan_payload(self.repo, group="full")["plan_digest"]
        self.assertEqual(first, second)

    def test_invalid_manifest_yields_no_plan(self) -> None:
        self._write("schema_version: 99\nunits: {}\ngroups: {}\n")
        payload = ST.plan_payload(self.repo, group="default")
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload.get("plan"))

    def test_cli_plan_returns_the_plan_in_json(self) -> None:
        result = subprocess.run(
            [sys.executable, ".env-manager/manage.py", "test", "plan",
             "--group", "full", "--cwd", str(self.repo), "--format", "json"],
            cwd=ROOT_DIR, capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(P.PLAN_SCHEMA, payload["plan"]["schema"])
        self.assertEqual(64, len(payload["plan_digest"]))


if __name__ == "__main__":
    unittest.main()
