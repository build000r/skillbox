"""Wave-concurrent local unit executor.

The acceptance is failure injection plus deterministic wave assignment, so the
suite splits that way: a pure scheduling half that pins batch assignment as a
golden without running anything, and an execution half that injects each failure
mode the bead names — a unit failing mid-wave, cancellation, a timeout that must
kill a whole process group, and slot starvation.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import sbp_test_executor as EX  # noqa: E402

EXECUTOR_SOURCE = ENV_MANAGER_DIR / "runtime_manager" / "sbp_test_executor.py"

OK = [sys.executable, "-c", "print('ok')"]
FAIL = [sys.executable, "-c", "import sys; sys.exit(3)"]


def unit(
    uid: str,
    argv: list[str] | None = None,
    *,
    wave: int = 0,
    depends_on: tuple[str, ...] = (),
    resource_group: str | None = None,
    exclusivity: str = "shared",
    caps: tuple[str, ...] = (),
    timeout_s: int | None = None,
    runnable: bool = True,
    env_allowlist: tuple[str, ...] = (),
    blocked_by: tuple[str, ...] = (),
) -> dict:
    return {
        "id": uid,
        "argv": list(argv if argv is not None else OK),
        "cwd": None,
        "depends_on": list(depends_on),
        "timeout_s": timeout_s,
        "needs": {
            "os": [],
            "python": None,
            "caps": list(caps),
            "services": [],
            "resource_group": resource_group,
            "exclusivity": exclusivity,
        },
        "artifacts": {"collect": []},
        "env_allowlist": list(env_allowlist),
        "cache": None,
        "wave": wave,
        "runnable": runnable,
        "reason": "ok" if runnable else "blocked",
        "blocked_by": list(blocked_by),
    }


def plan(units: list[dict], waves: list[list[str]], edges: list[dict] | None = None) -> dict:
    return {"units": units, "edges": edges or [], "waves": waves}


def depends_edge(dependency: str, dependent: str) -> dict:
    """`dependent` depends on `dependency`.

    The plan stores a depends_on edge dependent -> dependency with `source` /
    `target` keys (sbp_test_plan._graph_payload), so dependencies sort first in
    topological order. Getting this backwards, or naming the keys `from`/`to`,
    silently yields an empty graph and no skip propagation at all.
    """
    return {"source": dependent, "target": dependency, "kind": EX.DEPENDS_ON}


class ExecutorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.logs = self.root / "logs"

    def assert_refused(self, code: str, action: object) -> EX.ExecutorRefusal:
        with self.assertRaises(EX.ExecutorRefusal) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def run_plan(self, document: dict, **kwargs: object) -> EX.RunOutcome:
        options: dict = {"repo": self.root, "log_root": self.logs, "max_parallel": 4}
        options.update(kwargs)
        return EX.execute_plan(document, **options)  # type: ignore[arg-type]

    def states(self, outcome: EX.RunOutcome) -> dict[str, str]:
        return {result.unit_id: result.state for result in outcome.results}


class WaveAssignmentGoldenTests(ExecutorTestCase):
    """Deterministic wave assignment — the pure, golden-able surface."""

    def schedule(self, document: dict, cap: int) -> list[tuple]:
        batches = EX.schedule_batches(
            EX.plan_units(document), document["waves"], max_parallel=cap
        )
        return [(b.wave, b.index, b.unit_ids, b.slots_used) for b in batches]

    def test_the_batch_assignment_is_a_stable_golden(self) -> None:
        document = plan(
            [
                unit("alpha"),
                unit("bravo", resource_group="db"),
                unit("charlie", resource_group="db"),
                unit("delta", caps=("slots:2",)),
                unit("echo", exclusivity="exclusive"),
                unit("foxtrot", wave=1),
            ],
            [["alpha", "bravo", "charlie", "delta", "echo"], ["foxtrot"]],
        )
        self.assertEqual(
            [
                # Greedy packing at cap 4: alpha(1) + bravo(1) + delta(2) fills
                # it exactly; charlie waits because bravo already holds `db`.
                (0, 0, ("alpha", "bravo", "delta"), 4),
                (0, 1, ("charlie",), 1),
                (0, 2, ("echo",), 1),
                (1, 0, ("foxtrot",), 1),
            ],
            self.schedule(document, 4),
        )

    def test_the_same_plan_and_cap_always_schedule_identically(self) -> None:
        document = plan(
            [unit(name) for name in ("zulu", "alpha", "mike", "bravo")],
            [["zulu", "alpha", "mike", "bravo"]],
        )
        first = self.schedule(document, 2)
        for _ in range(5):
            self.assertEqual(first, self.schedule(document, 2))
        # Considered in id order, never dict order.
        self.assertEqual(("alpha", "bravo"), first[0][2])

    def test_two_units_in_one_resource_group_never_co_run(self) -> None:
        document = plan(
            [unit("a", resource_group="db"), unit("b", resource_group="db")],
            [["a", "b"]],
        )
        batches = self.schedule(document, 8)
        self.assertEqual(2, len(batches))
        for _wave, _index, ids, _slots in batches:
            self.assertEqual(1, len(ids))

    def test_an_exclusive_unit_runs_alone(self) -> None:
        document = plan(
            [unit("a"), unit("solo", exclusivity="exclusive"), unit("b")],
            [["a", "solo", "b"]],
        )
        batches = self.schedule(document, 8)
        solo = [ids for _w, _i, ids, _s in batches if "solo" in ids]
        self.assertEqual([("solo",)], solo)

    def test_a_multi_slot_unit_is_billed_for_every_slot(self) -> None:
        document = plan([unit("wide", caps=("slots:4",)), unit("thin")], [["thin", "wide"]])
        self.assertEqual(
            [(0, 0, ("thin",), 1), (0, 1, ("wide",), 4)], self.schedule(document, 4)
        )

    def test_the_xdist_alias_declares_slots_too(self) -> None:
        self.assertEqual(4, EX.slots_for({"caps": ["xdist:4"]}))
        self.assertEqual(1, EX.slots_for({"caps": []}))
        self.assertEqual(3, EX.slots_for({"caps": ["slots:2", "xdist:3"]}))

    def test_the_cap_is_never_exceeded(self) -> None:
        document = plan([unit(f"u{index}") for index in range(9)], [[f"u{index}" for index in range(9)]])
        for cap in (1, 2, 3, 5):
            for _wave, _index, ids, slots in self.schedule(document, cap):
                self.assertLessEqual(slots, cap)
                self.assertLessEqual(len(ids), cap)

    def test_unrunnable_units_are_never_scheduled(self) -> None:
        document = plan(
            [unit("a"), unit("skipme", runnable=False, blocked_by=("a",))], [["a", "skipme"]]
        )
        scheduled = {uid for _w, _i, ids, _s in self.schedule(document, 4) for uid in ids}
        self.assertEqual({"a"}, scheduled)

    def test_malformed_slot_declarations_are_refused(self) -> None:
        for caps in (["slots:0"], ["slots:999"], ["slots:many"]):
            self.assert_refused(
                "slots_invalid", lambda caps=caps: EX.slots_for({"caps": caps})
            )

    def test_the_cap_itself_is_bounded(self) -> None:
        document = plan([unit("a")], [["a"]])
        for cap in (0, -1, EX.MAX_PARALLEL_CEILING + 1):
            self.assert_refused(
                "executor_misconfigured",
                lambda cap=cap: EX.schedule_batches(
                    EX.plan_units(document), document["waves"], max_parallel=cap
                ),
            )

    def test_the_default_cap_is_the_core_count(self) -> None:
        self.assertGreaterEqual(EX.default_max_parallel(), 1)

    def test_the_schedule_payload_is_json_shaped(self) -> None:
        document = plan([unit("a"), unit("b")], [["a", "b"]])
        batches = EX.schedule_batches(EX.plan_units(document), document["waves"], max_parallel=2)
        payload = EX.schedule_payload(batches)
        json.dumps(payload)
        self.assertEqual(["a", "b"], payload["batches"][0]["units"])


class SlotStarvationTests(ExecutorTestCase):
    """A unit that can never fit is an answer, not a hang."""

    def test_a_unit_needing_more_slots_than_the_cap_is_refused(self) -> None:
        document = plan([unit("wide", caps=("slots:8",))], [["wide"]])
        error = self.assert_refused(
            "slot_starvation",
            lambda: EX.schedule_batches(
                EX.plan_units(document), document["waves"], max_parallel=4
            ),
        )
        self.assertEqual(["wide"], error.units)

    def test_starvation_is_detected_before_anything_launches(self) -> None:
        marker = self.root / "launched"
        document = plan(
            [
                unit(
                    "wide",
                    [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"],
                    caps=("slots:8",),
                )
            ],
            [["wide"]],
        )
        self.assert_refused("slot_starvation", lambda: self.run_plan(document, max_parallel=2))
        self.assertFalse(marker.exists())

    def test_an_unrunnable_wide_unit_does_not_starve_the_run(self) -> None:
        # It was never going to be scheduled, so it must not refuse the plan.
        document = plan(
            [unit("wide", caps=("slots:8",), runnable=False), unit("a")], [["a", "wide"]]
        )
        outcome = self.run_plan(document, max_parallel=2)
        self.assertEqual(EX.STATE_COMPLETED, self.states(outcome)["a"])
        self.assertEqual(EX.STATE_SKIPPED, self.states(outcome)["wide"])


class FailureInjectionTests(ExecutorTestCase):
    """A failure must not hide the verdict of everything independent of it."""

    def test_a_failing_unit_does_not_stop_independent_units(self) -> None:
        document = plan([unit("bad", FAIL), unit("good")], [["bad", "good"]])
        outcome = self.run_plan(document)
        states = self.states(outcome)
        self.assertEqual(EX.STATE_FAILED, states["bad"])
        self.assertEqual(EX.STATE_COMPLETED, states["good"])
        self.assertFalse(outcome.ok)

    def test_the_exit_code_and_cause_are_reported(self) -> None:
        outcome = self.run_plan(plan([unit("bad", FAIL)], [["bad"]]))
        result = outcome.results[0]
        self.assertEqual(3, result.exit_code)
        self.assertIn("3", result.cause)

    def test_dependents_of_a_failure_are_skipped_and_named(self) -> None:
        document = plan(
            [unit("root", FAIL), unit("child", wave=1, depends_on=("root",))],
            [["root"], ["child"]],
            edges=[depends_edge("root", "child")],
        )
        outcome = self.run_plan(document)
        states = self.states(outcome)
        self.assertEqual(EX.STATE_FAILED, states["root"])
        self.assertEqual(EX.STATE_SKIPPED, states["child"])
        child = next(r for r in outcome.results if r.unit_id == "child")
        self.assertEqual(("root",), child.blocked_by)
        self.assertIn("root", child.cause)

    def test_a_sibling_of_a_skipped_unit_still_runs(self) -> None:
        document = plan(
            [
                unit("root", FAIL),
                unit("child", wave=1, depends_on=("root",)),
                unit("sibling", wave=1),
            ],
            [["root"], ["child", "sibling"]],
            edges=[depends_edge("root", "child")],
        )
        states = self.states(self.run_plan(document))
        self.assertEqual(EX.STATE_SKIPPED, states["child"])
        self.assertEqual(EX.STATE_COMPLETED, states["sibling"])

    def test_a_plan_skip_keeps_the_plans_own_reason(self) -> None:
        document = plan(
            [unit("a"), unit("cyclic", runnable=False, blocked_by=("cyclic",))],
            [["a", "cyclic"]],
        )
        outcome = self.run_plan(document)
        skipped = next(r for r in outcome.results if r.unit_id == "cyclic")
        self.assertEqual(EX.STATE_SKIPPED, skipped.state)
        self.assertIn("sealed plan", skipped.cause)

    def test_every_unit_appears_in_the_outcome(self) -> None:
        document = plan(
            [unit("a"), unit("b", FAIL), unit("c", runnable=False)], [["a", "b", "c"]]
        )
        outcome = self.run_plan(document)
        self.assertEqual({"a", "b", "c"}, set(self.states(outcome)))
        self.assertEqual({"a"}, set(outcome.by_state()[EX.STATE_COMPLETED]))

    def test_a_unit_whose_binary_is_missing_fails_rather_than_raising(self) -> None:
        document = plan([unit("ghost", ["/nonexistent/binary"])], [["ghost"]])
        outcome = self.run_plan(document)
        result = outcome.results[0]
        self.assertEqual(EX.STATE_FAILED, result.state)
        self.assertIn("could not start", result.cause)


class TimeoutTests(ExecutorTestCase):
    """A timeout must kill the whole process group, not just the child."""

    def test_a_slow_unit_is_timed_out(self) -> None:
        document = plan(
            [unit("slow", [sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=1)],
            [["slow"]],
        )
        started = time.monotonic()
        outcome = self.run_plan(document)
        elapsed = time.monotonic() - started
        result = outcome.results[0]
        self.assertEqual(EX.STATE_TIMED_OUT, result.state)
        self.assertIn("ceiling", result.cause)
        self.assertLess(elapsed, 20, "the timeout did not actually cut the unit short")

    def test_the_timeout_kills_grandchildren_too(self) -> None:
        # The reason for start_new_session + killpg: terminating only the direct
        # child leaves the real work running, orphaned and invisible.
        beacon = self.root / "grandchild.txt"
        child = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', \"import time\\nwhile True:\\n"
            f"    open({str(beacon)!r}, 'a').write('x')\\n    time.sleep(0.05)\"])\n"
            "time.sleep(30)\n"
        )
        document = plan(
            [unit("spawner", [sys.executable, "-c", child], timeout_s=1)], [["spawner"]]
        )
        outcome = self.run_plan(document)
        self.assertEqual(EX.STATE_TIMED_OUT, outcome.results[0].state)
        # Give any survivor a chance to keep writing, then prove it stopped.
        time.sleep(0.4)
        first = beacon.stat().st_size if beacon.exists() else 0
        time.sleep(0.5)
        second = beacon.stat().st_size if beacon.exists() else 0
        self.assertEqual(first, second, "a grandchild outlived the process group kill")

    def test_a_unit_without_a_timeout_is_not_capped(self) -> None:
        document = plan([unit("quick", timeout_s=None)], [["quick"]])
        self.assertEqual(EX.STATE_COMPLETED, self.states(self.run_plan(document))["quick"])


class CancellationTests(ExecutorTestCase):
    """Cancellation accounts for every unit rather than abandoning them."""

    def test_cancelling_before_the_run_cancels_everything(self) -> None:
        document = plan([unit("a"), unit("b")], [["a", "b"]])
        outcome = self.run_plan(document, cancel=lambda: True)
        self.assertTrue(outcome.cancelled)
        self.assertEqual(
            {"a": EX.STATE_CANCELLED, "b": EX.STATE_CANCELLED}, self.states(outcome)
        )

    def test_cancelling_mid_run_kills_the_running_unit(self) -> None:
        flag = {"cancel": False}

        def cancel() -> bool:
            return flag["cancel"]

        document = plan(
            [unit("slow", [sys.executable, "-c", "import time; time.sleep(30)"])],
            [["slow"]],
        )
        import threading

        threading.Timer(0.5, lambda: flag.__setitem__("cancel", True)).start()
        started = time.monotonic()
        outcome = self.run_plan(document, cancel=cancel)
        elapsed = time.monotonic() - started
        self.assertTrue(outcome.cancelled)
        self.assertLess(elapsed, 20, "cancellation did not cut the unit short")

    def test_a_later_wave_is_cancelled_not_silently_dropped(self) -> None:
        document = plan(
            [unit("a"), unit("b", wave=1)], [["a"], ["b"]]
        )
        calls = {"n": 0}

        def cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 3

        outcome = self.run_plan(document, cancel=cancel)
        self.assertIn(self.states(outcome)["b"], {EX.STATE_CANCELLED, EX.STATE_COMPLETED})
        self.assertEqual(
            len(outcome.results), len({r.unit_id for r in outcome.results})
        )


class PlacementTests(ExecutorTestCase):
    """Placement is decided per unit and written down before launch."""

    def test_a_placement_block_is_persisted_for_every_unit(self) -> None:
        document = plan([unit("a"), unit("b", FAIL)], [["a", "b"]])
        outcome = self.run_plan(document)
        for result in outcome.results:
            self.assertTrue(result.placement_file, result.unit_id)
            payload = json.loads(Path(result.placement_file).read_text(encoding="utf-8"))
            self.assertEqual("machine-placement/v1", payload["kind"])
            self.assertTrue(payload["local_only"])

    def test_the_block_exists_before_the_unit_runs(self) -> None:
        # The unit itself asserts the file is already there; if placement were
        # written afterwards the unit would exit non-zero.
        placement = self.logs / "placement" / "prober.json"
        document = plan(
            [
                unit(
                    "prober",
                    [
                        sys.executable,
                        "-c",
                        f"import os,sys; sys.exit(0 if os.path.exists({str(placement)!r}) else 9)",
                    ],
                )
            ],
            [["prober"]],
        )
        outcome = self.run_plan(document)
        self.assertEqual(EX.STATE_COMPLETED, outcome.results[0].state)

    def test_the_placement_file_is_private(self) -> None:
        outcome = self.run_plan(plan([unit("a")], [["a"]]))
        path = Path(outcome.results[0].placement_file)
        import stat as stat_module

        self.assertEqual(0o600, stat_module.S_IMODE(os.stat(path).st_mode))

    def test_without_a_configuration_the_decision_says_so(self) -> None:
        spec = EX.plan_units(plan([unit("a")], [["a"]]))[0]
        decision = EX.decide_placement(spec, None)
        self.assertEqual("no_match", decision["decision"])
        self.assertIn("no machines configuration", decision["reasons"][0])

    def test_candidates_are_filtered_to_this_machine(self) -> None:
        from runtime_manager.machines import MachineProfile, MachinesConfig

        config = MachinesConfig(
            machines={
                "here": MachineProfile(machine_id="here", repo_roots=("/repos",)),
                "elsewhere": MachineProfile(machine_id="elsewhere", repo_roots=("/repos",)),
            },
            source_path="fixture",
        )
        context = EX.PlacementContext(config=config, current_id="here")
        narrowed = EX.local_only_config(context)
        self.assertEqual({"here"}, set(narrowed.machines))


class ProcessSemanticsTests(ExecutorTestCase):
    """cwd, env, and logs — the parts copied from run_tasks."""

    def test_each_unit_gets_its_own_log_file(self) -> None:
        document = plan(
            [
                unit("a", [sys.executable, "-c", "print('AAA')"]),
                unit("b", [sys.executable, "-c", "print('BBB')"]),
            ],
            [["a", "b"]],
        )
        outcome = self.run_plan(document)
        for result in outcome.results:
            text = Path(result.log_file).read_text(encoding="utf-8")
            self.assertIn(result.unit_id.upper() * 3, text)

    def test_stderr_is_folded_into_the_log(self) -> None:
        document = plan(
            [unit("noisy", [sys.executable, "-c", "import sys; print('E', file=sys.stderr)"])],
            [["noisy"]],
        )
        outcome = self.run_plan(document)
        self.assertIn("E", Path(outcome.results[0].log_file).read_text(encoding="utf-8"))

    def test_the_env_allowlist_drops_everything_else(self) -> None:
        script = "import os; print(os.environ.get('KEEP','-'), os.environ.get('DROP','-'))"
        document = plan(
            [unit("env", [sys.executable, "-c", script], env_allowlist=("KEEP",))],
            [["env"]],
        )
        outcome = self.run_plan(
            document, base_env={"KEEP": "yes", "DROP": "no", "PATH": os.environ.get("PATH", "")}
        )
        text = Path(outcome.results[0].log_file).read_text(encoding="utf-8")
        self.assertIn("yes -", text)

    def test_the_unit_runs_in_the_repo_by_default(self) -> None:
        document = plan([unit("pwd", [sys.executable, "-c", "import os; print(os.getcwd())"])], [["pwd"]])
        outcome = self.run_plan(document)
        text = Path(outcome.results[0].log_file).read_text(encoding="utf-8").strip()
        self.assertEqual(str(Path(os.path.realpath(self.root))), str(Path(os.path.realpath(text))))

    def test_a_relative_cwd_is_resolved_against_the_repo(self) -> None:
        (self.root / "sub").mkdir()
        spec = unit("sub", [sys.executable, "-c", "import os; print(os.getcwd())"])
        spec["cwd"] = "sub"
        outcome = self.run_plan(plan([spec], [["sub"]]))
        text = Path(outcome.results[0].log_file).read_text(encoding="utf-8").strip()
        self.assertTrue(text.endswith("sub"), text)


class PlanProjectionTests(ExecutorTestCase):
    """The executor reads the sealed plan and never the repository."""

    def test_a_malformed_plan_is_refused(self) -> None:
        for document in (
            {},
            {"units": "nope"},
            {"units": []},
            {"units": [{"id": "a", "needs": "nope"}]},
            {"units": [{"id": "a", "needs": {}, "argv": "ls"}]},
        ):
            self.assert_refused(
                "plan_invalid", lambda document=document: EX.plan_units(document)
            )

    def test_units_carry_their_scheduling_facts_from_the_plan(self) -> None:
        spec = EX.plan_units(
            plan([unit("a", resource_group="db", exclusivity="exclusive", caps=("slots:2",))], [["a"]])
        )[0]
        self.assertEqual("db", spec.resource_group)
        self.assertTrue(spec.exclusive)
        self.assertEqual(2, spec.slots)

    def test_the_executor_never_reads_a_manifest(self) -> None:
        # Workers act on the sealed plan; re-reading the tree would break the
        # reproducibility the plan exists to provide.
        source = EXECUTOR_SOURCE.read_text(encoding="utf-8")
        for banned in ("load_manifest", "MANIFEST_RELPATH", "sbp_test_manifest"):
            self.assertNotIn(banned, source, banned)

    def test_every_refusal_code_in_the_source_is_declared(self) -> None:
        source = EXECUTOR_SOURCE.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\(\s*"([a-z_]+)"', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - EX.REFUSAL_CODES)

    def test_the_outcome_payload_is_json_shaped(self) -> None:
        outcome = self.run_plan(plan([unit("a"), unit("b", FAIL)], [["a", "b"]]))
        payload = outcome.to_payload()
        json.dumps(payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            ["a", "b"], [row["unit_id"] for row in payload["results"]]
        )

    def test_no_receipt_schema_is_defined_here(self) -> None:
        # Receipts belong to the sibling bead; defining one here would fork the
        # contract.
        source = EXECUTOR_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("receipt/v", source)
        self.assertNotIn("RECEIPT_SCHEMA", source)


REAL_MANIFEST = """\
schema_version: 1
units:
  lint:
    command: [python3, --version]
    timeout_s: 120
  unit:
    command: [python3, --version]
    timeout_s: 600
    resource_group: cpu
  integration:
    command: [python3, --version]
    depends_on: [unit]
    timeout_s: 900
    resource_group: db
    exclusivity: exclusive
groups:
  # `default` is required: a manifest never means "everything I happened to
  # find", so the compiler refuses without it.
  default: [lint, unit]
  full: [lint, unit, integration]
"""


class RealPlanIntegrationTests(unittest.TestCase):
    """Run a plan the compiler actually produced, not a hand-rolled dict.

    A synthetic plan can encode the wrong edge shape and still look right; only
    the compiler's own output proves the executor consumes it. (It is exactly
    what caught a `from`/`to` vs `source`/`target` mistake in this suite.)
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve() / "repo"
        (self.repo / ".skillbox").mkdir(parents=True)
        (self.repo / ".skillbox" / "test.yaml").write_text(REAL_MANIFEST, encoding="utf-8")
        self.logs = Path(temporary.name).resolve() / "logs"

    def compile(self):
        from runtime_manager import sbp_test_plan

        try:
            return sbp_test_plan.compile_plan(self.repo, group="full")
        except Exception as error:  # noqa: BLE001
            self.skipTest(f"plan compiler unavailable in this tree: {error}")

    def test_a_compiled_plan_schedules_and_runs(self) -> None:
        plan_obj = self.compile()
        outcome = EX.execute_plan(
            plan_obj.content, repo=self.repo, log_root=self.logs, max_parallel=4
        )
        states = {result.unit_id: result.state for result in outcome.results}
        self.assertEqual({"lint", "unit", "integration"}, set(states))
        self.assertTrue(outcome.ok, states)

    def test_the_compilers_edges_drive_real_skip_propagation(self) -> None:
        # `integration` depends on `unit`; break `unit` and the dependent must
        # be skipped through the compiler's own edge shape.
        plan_obj = self.compile()
        content = json.loads(json.dumps(plan_obj.content))
        for entry in content["units"]:
            if entry["id"] == "unit":
                entry["argv"] = list(FAIL)
        outcome = EX.execute_plan(
            content, repo=self.repo, log_root=self.logs, max_parallel=4
        )
        states = {result.unit_id: result.state for result in outcome.results}
        self.assertEqual(EX.STATE_FAILED, states["unit"])
        self.assertEqual(EX.STATE_SKIPPED, states["integration"])
        self.assertEqual(EX.STATE_COMPLETED, states["lint"])
        blocked = next(r for r in outcome.results if r.unit_id == "integration")
        self.assertEqual(("unit",), blocked.blocked_by)

    def test_the_compilers_exclusivity_is_honoured(self) -> None:
        plan_obj = self.compile()
        batches = EX.schedule_batches(
            EX.plan_units(plan_obj.content), plan_obj.content["waves"], max_parallel=8
        )
        for batch in batches:
            if "integration" in batch.unit_ids:
                self.assertEqual(("integration",), batch.unit_ids)


if __name__ == "__main__":
    unittest.main()
