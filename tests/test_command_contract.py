"""Cross-surface command contract: inventory, safety parity, and the ratchet.

Three things are proven here.

1. **The inventory is deterministic.** Two renders are byte-identical, ids are
   stable, and every record carries provenance.
2. **The ratchet is shrink-only.** The checked-in baseline is the set of gaps we
   have decided to live with, each with a reason and an owner. A new gap fails;
   a resolved one does not.
3. **Safety forwarding is real, not asserted.** The repaired ``box down``
   confirmation contract is exercised as a golden across CLI, Make and the
   operator MCP handler — and the pre-repair argv is replayed as a fixture that
   must still fail. A linter that cannot fail on the historical bug proves
   nothing about the current fix.

Nothing here executes a command or touches infrastructure: the MCP probe runs
the real handler with its process-spawning seam stubbed, and records the argv
that *would* have been passed to the CLI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import command_contract as CC  # noqa: E402

BASELINE_PATH = ROOT_DIR / "tests" / "goldens" / "command_contract_gaps.json"


def _load_operator_mcp():
    spec = importlib.util.spec_from_file_location(
        "test_command_contract_mcp", str(ROOT_DIR / "scripts" / "operator_mcp_server.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tiny_parser(*, safety: tuple[str, ...] = ()) -> argparse.ArgumentParser:
    """A two-command parser used to drive synthetic drift."""
    parser = argparse.ArgumentParser(prog="fake")
    subparsers = parser.add_subparsers(dest="command")
    quiet = subparsers.add_parser("status")
    quiet.add_argument("--format")
    loud = subparsers.add_parser("nuke")
    for option in safety:
        loud.add_argument(option, action="store_true" if option != "--confirm" else "store")
    return parser


def _spec(spec_id: str, *, side_effect: str = "none", risk: str = "low", surface=("cli",)):
    from runtime_manager import command_registry as cr

    return cr.CommandSpec(
        id=spec_id,
        tier=1,
        surface=tuple(surface),
        summary="fixture",
        inputs={},
        outputs={},
        side_effect=side_effect,
        risk=risk,
        entrypoint="manage.py",
        examples=(),
    )


class InventoryShapeTests(unittest.TestCase):
    """Stable ids, provenance, and a byte-identical render."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = CC.build_report()

    def test_every_command_carries_a_stable_id_and_provenance(self) -> None:
        self.assertTrue(self.report.commands)
        seen = set()
        for command in self.report.commands:
            with self.subTest(command=command.id):
                self.assertEqual(command.id, f"{command.surface}:{command.name}")
                self.assertIn(command.surface, CC.SURFACES)
                self.assertTrue(command.provenance)
                self.assertNotIn(command.id, seen, "duplicate command id")
                seen.add(command.id)

    def test_every_surface_is_actually_extracted(self) -> None:
        """A silently-empty extractor would make the whole report vacuous."""
        for surface in CC.SURFACES:
            with self.subTest(surface=surface):
                self.assertTrue(
                    [c for c in self.report.commands if c.surface == surface],
                    f"{surface} extracted to nothing",
                )

    def test_observed_safety_is_drawn_from_the_closed_vocabulary(self) -> None:
        for command in self.report.commands:
            with self.subTest(command=command.id):
                self.assertLessEqual(set(command.observed_safety), set(CC.SAFETY_OPTIONS))
                self.assertEqual(
                    list(command.observed_safety), sorted(command.observed_safety)
                )

    def test_every_gap_kind_is_declared(self) -> None:
        for gap in self.report.gaps:
            with self.subTest(gap=gap.id):
                self.assertIn(gap.kind, CC.GAP_KINDS)
                self.assertTrue(gap.detail)

    def test_two_consecutive_renders_are_byte_identical(self) -> None:
        first = CC.render_report(CC.build_report())
        second = CC.render_report(CC.build_report())
        self.assertEqual(first, second)
        self.assertEqual(first, CC.render_report(self.report))

    def test_report_payload_is_json_round_trippable(self) -> None:
        payload = json.loads(CC.render_report(self.report))
        self.assertEqual(payload["schema"], CC.REPORT_SCHEMA)
        self.assertEqual(payload["counts"]["commands"], len(self.report.commands))
        self.assertEqual(payload["counts"]["gaps"], len(self.report.gaps))


class BaselineRatchetTests(unittest.TestCase):
    """Shrink-only: new gaps fail, resolved gaps do not."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = CC.build_report()
        cls.baseline = CC.load_baseline(BASELINE_PATH)

    def test_no_gap_exists_outside_the_reviewed_baseline(self) -> None:
        new_gaps, _resolved = CC.diff_against_baseline(self.report, self.baseline)
        self.assertEqual(
            [],
            [f"{gap.kind}:{gap.id} — {gap.detail}" for gap in new_gaps],
            msg=(
                "new command-surface gap(s). Either fix the drift, or add an entry with a "
                f"reason and an owner to {BASELINE_PATH.relative_to(ROOT_DIR)}"
            ),
        )

    def test_every_baseline_entry_carries_a_reason_and_an_owner(self) -> None:
        self.assertTrue(self.baseline)
        for key, entry in sorted(self.baseline.items()):
            with self.subTest(gap=key):
                self.assertIn(entry["kind"], CC.GAP_KINDS)
                self.assertTrue(str(entry.get("reason", "")).strip(), "reason is required")
                self.assertTrue(str(entry.get("owner", "")).strip(), "owner is required")

    def test_resolving_a_gap_does_not_fail_the_ratchet(self) -> None:
        """Deleting a fixed gap from the baseline must be a passing edit.

        If it were not, fixing a gap would redden the tree until someone
        remembered to also edit a JSON file — which is how ratchets get deleted.
        """
        trimmed = dict(self.baseline)
        trimmed.pop(next(iter(sorted(trimmed))), None)
        new_gaps, resolved = CC.diff_against_baseline(self.report, trimmed)
        # The gap is still live, so trimming its entry IS new drift.
        self.assertTrue(new_gaps)
        # And with a baseline entry for something already fixed, nothing fails.
        stale = dict(self.baseline)
        stale[("runtime:already-fixed", CC.GAP_LIVE_COMMAND_UNREGISTERED)] = {
            "id": "runtime:already-fixed",
            "kind": CC.GAP_LIVE_COMMAND_UNREGISTERED,
            "reason": "fixture",
            "owner": "fixture",
        }
        new_gaps, resolved = CC.diff_against_baseline(self.report, stale)
        self.assertEqual([], new_gaps)
        self.assertIn(("runtime:already-fixed", CC.GAP_LIVE_COMMAND_UNREGISTERED), resolved)

    def test_synthetic_new_drift_fails(self) -> None:
        """A newly added live command with no spec is caught."""
        report = CC.build_report(
            runtime_parser=_tiny_parser(),
            box_parser=_tiny_parser(),
            mcp_tools=[],
            makefile_text="",
            specs=[],
        )
        new_gaps, _ = CC.diff_against_baseline(report, self.baseline)
        self.assertTrue(new_gaps)
        self.assertIn(
            CC.GAP_LIVE_COMMAND_UNREGISTERED, {gap.kind for gap in new_gaps}
        )


class SafetyParityTests(unittest.TestCase):
    """Declared destructiveness versus what the parser actually offers."""

    def test_destructive_spec_without_a_confirmation_option_is_a_gap(self) -> None:
        report = CC.build_report(
            runtime_parser=_tiny_parser(safety=("--dry-run",)),
            box_parser=_tiny_parser(),
            mcp_tools=[],
            makefile_text="",
            specs=[_spec("runtime.nuke", side_effect="destructive", risk="destructive")],
        )
        kinds = {gap.kind for gap in report.gaps if gap.id == "runtime:nuke"}
        self.assertIn(CC.GAP_DESTRUCTIVE_WITHOUT_CONFIRMATION, kinds)

    def test_dry_run_alone_never_counts_as_confirmation(self) -> None:
        self.assertNotIn("--dry-run", CC.CONFIRMATION_OPTIONS)
        self.assertNotIn("--force", CC.CONFIRMATION_OPTIONS)

    def test_bespoke_confirmation_spellings_are_recognized(self) -> None:
        """Not every confirmation is spelled ``--confirm`` or ``--yes``.

        ``state-backup restore`` guards itself with ``--i-understand-data-loss``
        and ``dcg relinquish`` with ``--approved-by``. An incomplete vocabulary
        reports those two correctly-guarded commands as unguarded — a false
        alarm in a checked-in baseline is worse than a missing check, because it
        teaches the reader to ignore the file.
        """
        for option in ("--i-understand-data-loss", "--approved-by"):
            with self.subTest(option=option):
                self.assertIn(option, CC.SAFETY_OPTIONS)
                self.assertIn(option, CC.CONFIRMATION_OPTIONS)

    def test_only_confirm_is_treated_as_identity_bound(self) -> None:
        """A blanket flag confirms an act; only ``--confirm <id>`` names it."""
        self.assertEqual(("--confirm",), CC.IDENTITY_BOUND_OPTIONS)
        for option in CC.CONFIRMATION_OPTIONS:
            if option in CC.IDENTITY_BOUND_OPTIONS:
                continue
            with self.subTest(option=option):
                self.assertFalse(CC.argv_confirms_identity(["down", "b", option], "b"))

    def test_every_live_destructive_command_offers_a_confirmation(self) -> None:
        """The headline safety invariant, stated directly rather than by baseline."""
        report = CC.build_report()
        offenders = [
            gap.id
            for gap in report.gaps
            if gap.kind == CC.GAP_DESTRUCTIVE_WITHOUT_CONFIRMATION
        ]
        self.assertEqual([], offenders)

    def test_a_confirmation_option_clears_the_gap(self) -> None:
        report = CC.build_report(
            runtime_parser=_tiny_parser(safety=("--confirm",)),
            box_parser=_tiny_parser(),
            mcp_tools=[],
            makefile_text="",
            specs=[_spec("runtime.nuke", side_effect="destructive", risk="destructive")],
        )
        kinds = {gap.kind for gap in report.gaps if gap.id == "runtime:nuke"}
        self.assertNotIn(CC.GAP_DESTRUCTIVE_WITHOUT_CONFIRMATION, kinds)

    def test_destructive_mcp_tool_without_a_contract_is_a_gap(self) -> None:
        report = CC.build_report(
            runtime_parser=_tiny_parser(),
            box_parser=_tiny_parser(),
            mcp_tools=[{"name": "op_wipe", "annotations": {"destructiveHint": True}}],
            makefile_text="",
            specs=[],
        )
        kinds = {gap.kind for gap in report.gaps if gap.id == "mcp:op_wipe"}
        self.assertIn(CC.GAP_MCP_DESTRUCTIVE_WITHOUT_CONTRACT, kinds)

    def test_live_destructive_mcp_tools_all_declare_a_contract(self) -> None:
        report = CC.build_report()
        offenders = [
            gap.id
            for gap in report.gaps
            if gap.kind == CC.GAP_MCP_DESTRUCTIVE_WITHOUT_CONTRACT
        ]
        self.assertEqual([], offenders)


class MakeWrapperTests(unittest.TestCase):
    """The Make surface, read only through supported wrapper patterns."""

    DESTRUCTIVE_SPEC = _spec("box.down", side_effect="destructive", risk="destructive")

    def _report(self, makefile_text: str) -> CC.ContractReport:
        box_parser = argparse.ArgumentParser(prog="box")
        subparsers = box_parser.add_subparsers(dest="command")
        down = subparsers.add_parser("down")
        down.add_argument("--dry-run", action="store_true")
        down.add_argument("--confirm", default="")
        return CC.build_report(
            runtime_parser=_tiny_parser(),
            box_parser=box_parser,
            mcp_tools=[],
            makefile_text=makefile_text,
            specs=[self.DESTRUCTIVE_SPEC],
        )

    def test_the_pre_repair_recipe_is_caught(self) -> None:
        """The historical bug: a wrapper with no way to pass a confirmation.

        This is the fixture that keeps the linter honest. If this ever stops
        failing, the Make surface check has quietly stopped working.
        """
        report = self._report("box-down:\n\t@python3 scripts/box.py down $(BOX_ARGS)\n")
        kinds = {gap.kind for gap in report.gaps if gap.id == "make:box-down"}
        self.assertIn(CC.GAP_MAKE_CANNOT_FORWARD_CONFIRMATION, kinds)

    def test_the_repaired_recipe_passes(self) -> None:
        makefile = (
            "BOX_DOWN_ARGS := $(strip $(if $(strip $(DRY_RUN)),--dry-run) "
            "$(if $(strip $(CONFIRM)),--confirm $(strip $(CONFIRM))))\n"
            "box-down:\n\t@python3 scripts/box.py down $(BOX_ARGS) $(BOX_DOWN_ARGS)\n"
        )
        report = self._report(makefile)
        kinds = {gap.kind for gap in report.gaps if gap.id == "make:box-down"}
        self.assertNotIn(CC.GAP_MAKE_CANNOT_FORWARD_CONFIRMATION, kinds)
        wrapper = next(c for c in report.commands if c.id == "make:box-down")
        self.assertEqual(("--confirm", "--dry-run"), wrapper.observed_safety)
        self.assertEqual("down", wrapper.declared["wraps_command"])

    def test_a_wrapper_over_a_non_destructive_command_needs_nothing(self) -> None:
        """`make dev-sanity` wraps a doctor that owns a --yes for its --fix.

        Requiring it to forward one would be noise, so the trigger is what the
        registry DECLARES about the wrapped command, not which flags that
        command happens to own.
        """
        report = CC.build_report(
            runtime_parser=_tiny_parser(safety=("--yes",)),
            box_parser=_tiny_parser(),
            mcp_tools=[],
            makefile_text="quiet:\n\t@python3 .env-manager/manage.py nuke\n",
            specs=[_spec("runtime.nuke")],
        )
        kinds = {gap.kind for gap in report.gaps if gap.id == "make:quiet"}
        self.assertNotIn(CC.GAP_MAKE_CANNOT_FORWARD_CONFIRMATION, kinds)

    def test_unsupported_recipe_shapes_are_skipped_not_guessed(self) -> None:
        report = self._report("box-down:\n\t@bash -c 'python3 scripts/box.py down'\n")
        self.assertEqual([], [c for c in report.commands if c.surface == CC.SURFACE_MAKE])

    def test_the_live_makefile_has_no_forwarding_gap(self) -> None:
        report = CC.build_report()
        offenders = [
            gap.id
            for gap in report.gaps
            if gap.kind == CC.GAP_MAKE_CANNOT_FORWARD_CONFIRMATION
        ]
        self.assertEqual([], offenders)


class TeardownForwardingGoldenTests(unittest.TestCase):
    """The repaired teardown confirmation, end to end, as the first golden."""

    BOX_ID = "acme-prod"

    def _observed_argv(self, *, dry_run: bool, marker: bool) -> list[str]:
        """Run the real MCP handler with its subprocess seam stubbed.

        Nothing is executed: ``run_script`` is replaced by a recorder, and the
        audit sink is silenced. What comes back is the argv the handler would
        have handed to ``scripts/box.py``.
        """
        module = _load_operator_mcp()
        captured: list[list[str]] = []

        def fake_run_script(script, args, timeout=None):
            captured.append(list(args))
            return (True, 0, {"ok": True})

        with (
            mock.patch.object(module, "run_script", side_effect=fake_run_script),
            mock.patch.object(module, "emit_event"),
            mock.patch.object(module, "_has_dryrun_marker", return_value=marker),
        ):
            module.handle_operator_teardown({"box_id": self.BOX_ID, "dry_run": dry_run})
        return captured[0] if captured else []

    def test_the_mcp_preview_forwards_dry_run_and_no_confirmation(self) -> None:
        argv = self._observed_argv(dry_run=True, marker=False)
        self.assertIn("--dry-run", argv)
        self.assertFalse(CC.argv_confirms_identity(argv, self.BOX_ID))

    def test_the_mcp_real_run_forwards_an_identity_bound_confirmation(self) -> None:
        argv = self._observed_argv(dry_run=False, marker=True)
        self.assertTrue(
            CC.argv_confirms_identity(argv, self.BOX_ID),
            f"MCP real teardown argv does not name the box it destroys: {argv}",
        )
        self.assertIn("--confirm", CC.forwarded_safety_options(argv))

    def test_the_mcp_real_run_refuses_without_a_marker(self) -> None:
        self.assertEqual([], self._observed_argv(dry_run=False, marker=False))

    def test_the_pre_repair_argv_still_fails(self) -> None:
        """The exact argv the handler built before the teardown repair.

        A linter that cannot fail on the historical bug is not evidence that the
        bug is fixed, so the old shape is replayed here forever.
        """
        old_argv = ["down", self.BOX_ID, "--format", "json"]
        self.assertFalse(CC.argv_confirms_identity(old_argv, self.BOX_ID))
        self.assertEqual((), CC.forwarded_safety_options(old_argv))

    def test_a_blanket_yes_is_not_an_identity_bound_confirmation(self) -> None:
        self.assertFalse(
            CC.argv_confirms_identity(["down", self.BOX_ID, "--yes"], self.BOX_ID)
        )

    def test_a_confirmation_naming_a_different_box_does_not_count(self) -> None:
        argv = ["down", self.BOX_ID, "--confirm", "some-other-box"]
        self.assertFalse(CC.argv_confirms_identity(argv, self.BOX_ID))

    def test_the_equals_form_is_accepted(self) -> None:
        argv = ["down", self.BOX_ID, f"--confirm={self.BOX_ID}"]
        self.assertTrue(CC.argv_confirms_identity(argv, self.BOX_ID))


class RegistryJoinTests(unittest.TestCase):
    """Joining compressed registry ids to live command paths."""

    def test_underscore_readings_are_enumerated_not_guessed(self) -> None:
        candidates = CC.registry_join_candidates("runtime.state_backup_restore")
        self.assertIn("state-backup restore", candidates)
        self.assertIn("state_backup_restore", candidates)
        self.assertIn("state-backup-restore", candidates)

    def test_exactly_one_live_match_resolves(self) -> None:
        resolved, matches = CC.resolve_registry_command(
            "runtime.state_backup_restore", {"runtime": {"state-backup restore"}}
        )
        self.assertEqual("runtime:state-backup restore", resolved)
        self.assertEqual(("state-backup restore",), matches)

    def test_several_live_matches_refuse_to_resolve(self) -> None:
        resolved, matches = CC.resolve_registry_command(
            "runtime.skill_off", {"runtime": {"skill off", "skill-off"}}
        )
        self.assertIsNone(resolved)
        self.assertEqual(2, len(matches))

    def test_an_unmodelled_prefix_is_reported_as_such(self) -> None:
        report = CC.build_report(
            runtime_parser=_tiny_parser(),
            box_parser=_tiny_parser(),
            mcp_tools=[],
            makefile_text="",
            specs=[_spec("clipboard.status")],
        )
        kinds = {gap.kind for gap in report.gaps if gap.id == "registry:clipboard.status"}
        self.assertEqual({CC.GAP_REGISTRY_SURFACE_NOT_MODELLED}, kinds)


class PurityTests(unittest.TestCase):
    """The linter must never run the commands it inventories."""

    def test_building_a_report_spawns_no_process(self) -> None:
        import subprocess

        with (
            mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess.run")),
            mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Popen")),
            mock.patch.object(subprocess, "check_output", side_effect=AssertionError("check_output")),
        ):
            report = CC.build_report()
        self.assertTrue(report.commands)

    def test_building_a_report_writes_nothing(self) -> None:
        import builtins

        real_open = builtins.open

        def guarded(file, mode="r", *args, **kwargs):
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                raise AssertionError(f"write attempted on {file!r}")
            return real_open(file, mode, *args, **kwargs)

        with mock.patch.object(builtins, "open", side_effect=guarded):
            report = CC.build_report()
        self.assertTrue(report.commands)


def _command(
    command_id: str,
    *,
    surface: str = CC.SURFACE_BOX,
    name: str | None = None,
    safety: tuple[str, ...] = (),
    provenance: str = "scripts/box.py:build_parser",
) -> CC.SurfaceCommand:
    return CC.SurfaceCommand(
        id=command_id,
        surface=surface,
        name=name if name is not None else command_id.split(":", 1)[1],
        provenance=provenance,
        observed_safety=tuple(sorted(safety)),
    )


class DestructivePolicyGoldenTests(unittest.TestCase):
    """The repaired teardown is the first golden: it must PASS the policy.

    A safety linter that only ever fires on synthetic input proves nothing about
    the tree it guards, so the live surfaces are checked first and the fixtures
    exist to prove the check can still fail.
    """

    def setUp(self) -> None:
        from runtime_manager import command_registry as cr

        self.specs = cr.default_registry()
        self.report = CC.build_report()
        self.findings = CC.check_destructive_policy(
            specs=self.specs, commands=self.report.commands
        )

    def test_the_repaired_box_teardown_passes_every_invariant(self) -> None:
        offending = [f for f in self.findings if f.surface in ("box:down", "registry:box.down")]
        self.assertEqual(
            [], offending, CC.render_findings(offending)
        )

    def test_the_live_tree_carries_only_owned_findings(self) -> None:
        """The ratchet: a NEW destructive violation fails here."""
        unaccepted = CC.unaccepted_findings(self.findings)
        self.assertEqual([], list(unaccepted), CC.render_findings(unaccepted))

    def test_every_owned_finding_is_still_real(self) -> None:
        """Shrink-only: repairing one means deleting its row, not leaving it."""
        live = {finding.key for finding in self.findings}
        for entry in CC.ACCEPTED_DESTRUCTIVE_FINDINGS:
            key = (entry["surface"], entry["invariant"])
            with self.subTest(key=key):
                self.assertIn(
                    key,
                    live,
                    "owned gap no longer reproduces; delete its row from "
                    "ACCEPTED_DESTRUCTIVE_FINDINGS",
                )
                self.assertTrue(str(entry["owner"]).strip())
                self.assertTrue(str(entry["reason"]).strip())

    def test_the_policy_is_deterministic(self) -> None:
        again = CC.check_destructive_policy(
            specs=self.specs, commands=self.report.commands
        )
        self.assertEqual(
            [f.to_payload() for f in self.findings], [f.to_payload() for f in again]
        )

    def test_every_declared_destructive_spec_is_actually_checked(self) -> None:
        """Guard the trigger: if this ever returns nothing the suite is vacuous."""
        destructive = [s for s in self.specs if CC.spec_is_destructive(s)]
        self.assertTrue(destructive)
        self.assertIn("box.down", [s.id for s in destructive])


class DestructivePolicyFixtureTests(unittest.TestCase):
    """Synthetic drift: each invariant must fail deterministically, alone."""

    def _findings(self, spec, command):
        return CC.check_destructive_policy(specs=[spec], commands=[command])

    def _only(self, findings):
        self.assertEqual(1, len(findings), CC.render_findings(findings))
        return findings[0]

    def test_a_missing_preview_fails(self) -> None:
        finding = self._only(
            self._findings(
                _spec("box.nuke", side_effect="destructive", risk="destructive"),
                _command("box:nuke", safety=("--confirm",)),
            )
        )
        self.assertEqual(finding.invariant, CC.INVARIANT_PREVIEW)
        self.assertEqual(finding.surface, "box:nuke")
        self.assertIn("--dry-run", finding.fix)

    def test_a_missing_confirmation_fails(self) -> None:
        finding = self._only(
            self._findings(
                _spec("box.nuke", side_effect="destructive", risk="destructive"),
                _command("box:nuke", safety=("--dry-run",)),
            )
        )
        self.assertEqual(finding.invariant, CC.INVARIANT_CONFIRMATION)
        self.assertIn("--confirm", finding.fix)

    def test_a_confirmation_flag_is_not_a_preview(self) -> None:
        """Previewing and confirming are opposites; neither substitutes.

        Without this, PREVIEW_OPTIONS could be widened to accept --yes and no
        fixture would notice -- found by mutating the module and re-running.
        """
        for option in CC.CONFIRMATION_OPTIONS:
            with self.subTest(option=option):
                finding = self._only(
                    self._findings(
                        _spec("box.nuke", side_effect="destructive", risk="destructive"),
                        _command("box:nuke", safety=(option,)),
                    )
                )
                self.assertEqual(finding.invariant, CC.INVARIANT_PREVIEW)

    def test_a_preview_flag_is_not_a_confirmation(self) -> None:
        finding = self._only(
            self._findings(
                _spec("box.nuke", side_effect="destructive", risk="destructive"),
                _command("box:nuke", safety=("--dry-run",)),
            )
        )
        self.assertEqual(finding.invariant, CC.INVARIANT_CONFIRMATION)

    def test_preview_and_confirmation_vocabularies_stay_disjoint(self) -> None:
        self.assertEqual(
            set(), set(CC.PREVIEW_OPTIONS) & set(CC.CONFIRMATION_OPTIONS)
        )

    def test_a_downgraded_risk_fails(self) -> None:
        finding = self._only(
            self._findings(
                _spec("box.nuke", side_effect="destructive", risk="low"),
                _command("box:nuke", safety=("--confirm", "--dry-run")),
            )
        )
        self.assertEqual(finding.invariant, CC.INVARIANT_RISK)
        self.assertEqual(finding.surface, "registry:box.nuke")
        self.assertIn("'low'", finding.detail)
        self.assertIn("command_registry.py", finding.fix)

    def test_medium_risk_is_also_a_downgrade(self) -> None:
        finding = self._only(
            self._findings(
                _spec("box.nuke", side_effect="destructive", risk="medium"),
                _command("box:nuke", safety=("--confirm", "--dry-run")),
            )
        )
        self.assertEqual(finding.invariant, CC.INVARIANT_RISK)

    def test_high_risk_is_accepted(self) -> None:
        self.assertEqual(
            (),
            self._findings(
                _spec("box.nuke", side_effect="destructive", risk="high"),
                _command("box:nuke", safety=("--confirm", "--dry-run")),
            ),
        )

    def test_an_undeclared_side_effect_fails(self) -> None:
        finding = self._only(
            self._findings(
                _spec("box.nuke", side_effect="local_write", risk="destructive"),
                _command("box:nuke", safety=("--confirm", "--dry-run")),
            )
        )
        self.assertEqual(finding.invariant, CC.INVARIANT_SIDE_EFFECT)
        self.assertIn("local_write", finding.detail)

    def test_a_fully_declared_and_guarded_surface_passes(self) -> None:
        self.assertEqual(
            (),
            self._findings(
                _spec("box.nuke", side_effect="destructive", risk="destructive"),
                _command("box:nuke", safety=("--confirm", "--dry-run", "--yes")),
            ),
        )

    def test_a_non_destructive_spec_is_not_subject_to_the_policy(self) -> None:
        self.assertEqual(
            (),
            self._findings(
                _spec("box.nuke", side_effect="local_write", risk="low"),
                _command("box:nuke", safety=()),
            ),
        )

    def test_violations_accumulate_rather_than_short_circuit(self) -> None:
        """One repair must not hide the next: all four are reported at once."""
        findings = self._findings(
            _spec("box.nuke", side_effect="local_write", risk="low"),
            _command("box:nuke", safety=()),
        )
        self.assertEqual((), findings, "a low/local_write spec is not destructive")
        findings = self._findings(
            _spec("box.nuke", side_effect="destructive", risk="low"),
            _command("box:nuke", safety=()),
        )
        self.assertEqual(
            [CC.INVARIANT_CONFIRMATION, CC.INVARIANT_PREVIEW, CC.INVARIANT_RISK],
            sorted(f.invariant for f in findings),
        )


class DestructiveMappingAmbiguityTests(unittest.TestCase):
    """An unresolvable mapping is stated, never guessed into a pass or a fail."""

    def test_zero_live_matches_is_an_explicit_mapping_finding(self) -> None:
        findings = CC.check_destructive_policy(
            specs=[_spec("box.nuke", side_effect="destructive", risk="destructive")],
            commands=[_command("box:other", safety=("--confirm", "--dry-run"))],
        )
        self.assertEqual([CC.INVARIANT_MAPPING], [f.invariant for f in findings])
        self.assertIn("0 live box commands", findings[0].detail)
        self.assertIn("do not guess", findings[0].fix)

    def test_several_live_matches_is_an_explicit_mapping_finding(self) -> None:
        findings = CC.check_destructive_policy(
            specs=[_spec("box.a_b", side_effect="destructive", risk="destructive")],
            commands=[
                _command("box:a-b", name="a-b", safety=()),
                _command("box:a b", name="a b", safety=()),
            ],
        )
        self.assertEqual([CC.INVARIANT_MAPPING], [f.invariant for f in findings])
        self.assertIn("2 live box commands", findings[0].detail)

    def test_an_unmodelled_surface_is_stated_not_asserted_about(self) -> None:
        findings = CC.check_destructive_policy(
            specs=[
                _spec("clipboard.wipe", side_effect="destructive", risk="destructive")
            ],
            commands=[],
        )
        self.assertEqual([CC.INVARIANT_MAPPING], [f.invariant for f in findings])
        self.assertIn("outside the modelled surfaces", findings[0].detail)

    def test_an_ambiguous_mapping_never_yields_a_preview_or_confirm_verdict(self) -> None:
        """The point of stopping: no invented claim about a CLI never read."""
        findings = CC.check_destructive_policy(
            specs=[_spec("box.nuke", side_effect="destructive", risk="destructive")],
            commands=[],
        )
        self.assertNotIn(CC.INVARIANT_PREVIEW, [f.invariant for f in findings])
        self.assertNotIn(CC.INVARIANT_CONFIRMATION, [f.invariant for f in findings])


class ForwardedArgvPolicyTests(unittest.TestCase):
    """The half no static read can answer, classified from an observed argv."""

    BOX_ID = "acme-prod"
    SOURCE = "scripts/operator_mcp_server.py:handle_operator_teardown"
    OWNER = "scripts/operator_mcp_server.py"

    def _check(self, argv, **kwargs):
        return CC.check_forwarded_argv(
            argv,
            surface="mcp:operator_teardown",
            subject=self.BOX_ID,
            source=self.SOURCE,
            owner=self.OWNER,
            **kwargs,
        )

    def test_the_repaired_teardown_argv_forwards_identity(self) -> None:
        argv = ["down", self.BOX_ID, "--format", "json", "--confirm", self.BOX_ID]
        self.assertEqual((), self._check(argv))

    def test_the_pre_repair_argv_fails_with_a_complete_diagnostic(self) -> None:
        finding = self._check(["down", self.BOX_ID, "--format", "json"])[0]
        self.assertEqual(finding.invariant, CC.INVARIANT_ARGV_FORWARDED)
        self.assertEqual(finding.surface, "mcp:operator_teardown")
        self.assertEqual(finding.source, self.SOURCE)
        self.assertEqual(finding.owner, self.OWNER)
        self.assertIn("no safety option at all", finding.detail)
        self.assertIn("--confirm", finding.fix)
        self.assertIn(self.BOX_ID, finding.fix)

    def test_a_blanket_yes_fails_where_identity_is_required(self) -> None:
        finding = self._check(["down", self.BOX_ID, "--yes"])[0]
        self.assertIn("--yes", finding.detail)
        self.assertIn("never names", finding.detail)

    def test_a_confirmation_for_another_subject_fails(self) -> None:
        finding = self._check(["down", self.BOX_ID, "--confirm", "other-box"])[0]
        self.assertEqual(finding.invariant, CC.INVARIANT_ARGV_FORWARDED)

    def test_a_blanket_yes_passes_where_identity_is_not_required(self) -> None:
        """Not every destructive surface has a subject to name."""
        self.assertEqual(
            (), self._check(["off", "skill", "--yes"], require_identity=False)
        )

    def test_no_confirmation_fails_even_without_identity(self) -> None:
        finding = self._check(["off", "skill"], require_identity=False)[0]
        self.assertEqual(finding.invariant, CC.INVARIANT_ARGV_FORWARDED)
        self.assertIn("no confirmation option", finding.detail)

    def test_the_real_mcp_handler_argv_satisfies_the_policy(self) -> None:
        """Golden: the live handler, observed under stubs, passes the gate."""
        module = _load_operator_mcp()
        captured: list[list[str]] = []

        def fake_run_script(script, args, timeout=None):
            captured.append(list(args))
            return (True, 0, {"ok": True})

        with (
            mock.patch.object(module, "run_script", side_effect=fake_run_script),
            mock.patch.object(module, "emit_event"),
            mock.patch.object(module, "_has_dryrun_marker", return_value=True),
            mock.patch.object(module, "_clear_dryrun_marker"),
        ):
            module.handle_operator_teardown({"box_id": self.BOX_ID, "dry_run": False})
        self.assertTrue(captured, "handler never reached the CLI")
        self.assertEqual((), self._check(captured[0]))


class PolicyDiagnosticShapeTests(unittest.TestCase):
    """Every diagnostic names surface, source, owner, invariant, and the fix."""

    def test_every_field_is_present_and_non_empty(self) -> None:
        findings = CC.check_destructive_policy(
            specs=[_spec("box.nuke", side_effect="destructive", risk="low")],
            commands=[_command("box:nuke", safety=())],
        )
        self.assertTrue(findings)
        for finding in findings:
            with self.subTest(invariant=finding.invariant):
                payload = finding.to_payload()
                self.assertEqual(
                    sorted(payload),
                    ["detail", "fix", "invariant", "owner", "source", "surface"],
                )
                for key, value in payload.items():
                    self.assertTrue(str(value).strip(), f"{key} is empty")
                self.assertIn(finding.invariant, CC.DESTRUCTIVE_INVARIANTS)

    def test_the_owner_falls_back_to_the_surface_when_no_binary_is_declared(self) -> None:
        findings = CC.check_destructive_policy(
            specs=[_spec("box.nuke", side_effect="destructive", risk="low")],
            commands=[_command("box:nuke", safety=("--confirm", "--dry-run"))],
        )
        self.assertEqual(findings[0].owner, "scripts/box.py")

    def test_a_declared_owner_binary_wins(self) -> None:
        from runtime_manager import command_registry as cr

        spec = cr.CommandSpec(
            id="runtime.skill_off",
            tier=1,
            surface=("cli",),
            summary="fixture",
            inputs={},
            outputs={},
            side_effect="destructive",
            risk="low",
            entrypoint="manage.py",
            examples=(),
            owner_binary="sbp",
        )
        findings = CC.check_destructive_policy(specs=[spec], commands=[])
        self.assertTrue(findings)
        self.assertEqual({f.owner for f in findings}, {"sbp"})

    def test_rendering_is_stable_and_readable(self) -> None:
        findings = CC.check_destructive_policy(
            specs=[_spec("box.nuke", side_effect="destructive", risk="low")],
            commands=[_command("box:nuke", safety=())],
        )
        text = CC.render_findings(findings)
        self.assertEqual(text, CC.render_findings(findings))
        for label in ("source:", "owner:", "detail:", "fix:"):
            self.assertIn(label, text)


class ContractLintEntrypointTests(unittest.TestCase):
    """The ratchet as one read-only command, so it is not an obscure unit test.

    Drift is injected by giving the payload a root whose baseline is missing an
    entry, rather than by patching the linter: what CI actually gates on is the
    combination of live surfaces and checked-in baselines, so that is what the
    fixture varies.
    """

    def setUp(self) -> None:
        from runtime_manager import cli as cli_module

        self.cli = cli_module

    def _root_with_baseline(self, entries) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        goldens = root / "tests" / "goldens"
        goldens.mkdir(parents=True)
        (goldens / "command_contract_gaps.json").write_text(
            json.dumps({"schema": CC.BASELINE_SCHEMA, "gaps": list(entries)}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        return root

    def test_the_accepted_baseline_reports_no_new_drift(self) -> None:
        payload = self.cli.contract_lint_payload(ROOT_DIR)
        self.assertTrue(payload["ok"], json.dumps(payload, indent=2))
        self.assertEqual(payload["new_gaps"], [])
        self.assertEqual(payload["new_policy_findings"], [])
        self.assertEqual(payload["schema"], "skillbox.contract-lint.v1")

    def test_the_payload_shape_is_stable(self) -> None:
        payload = self.cli.contract_lint_payload(ROOT_DIR)
        self.assertEqual(
            sorted(payload),
            [
                "counts",
                "new_gaps",
                "new_policy_findings",
                "next_actions",
                "ok",
                "resolved_baseline_entries",
                "schema",
            ],
        )
        self.assertEqual(
            sorted(payload["counts"]),
            [
                "commands",
                "gaps",
                "new_gaps",
                "new_policy_findings",
                "policy_findings",
                "registry_specs",
                "resolved_baseline_entries",
            ],
        )

    def test_an_empty_baseline_reports_every_gap_as_new(self) -> None:
        """Synthetic drift: the linter must be able to fail."""
        payload = self.cli.contract_lint_payload(self._root_with_baseline([]))
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["new_gaps"])
        self.assertEqual(payload["counts"]["new_gaps"], payload["counts"]["gaps"])

    def test_a_removed_baseline_entry_surfaces_as_exactly_one_new_gap(self) -> None:
        real = CC.load_baseline(BASELINE_PATH)
        entries = [dict(entry) for entry in real.values()]
        self.assertTrue(entries, "baseline is empty; this fixture proves nothing")
        dropped = entries.pop(0)
        payload = self.cli.contract_lint_payload(self._root_with_baseline(entries))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["counts"]["new_gaps"], 1)
        gap = payload["new_gaps"][0]
        self.assertEqual(gap["id"], dropped["id"])
        self.assertEqual(gap["kind"], dropped["kind"])

    def test_every_new_gap_names_its_owning_surface(self) -> None:
        payload = self.cli.contract_lint_payload(self._root_with_baseline([]))
        for gap in payload["new_gaps"]:
            with self.subTest(gap=gap["id"]):
                self.assertTrue(str(gap["id"]).strip())
                self.assertIn(gap["kind"], CC.GAP_KINDS)
                self.assertTrue(str(gap["detail"]).strip())

    def test_a_stale_baseline_entry_is_reported_but_never_fails(self) -> None:
        entries = [dict(entry) for entry in CC.load_baseline(BASELINE_PATH).values()]
        entries.append(
            {
                "id": "runtime:long-gone",
                "kind": CC.GAP_LIVE_COMMAND_UNREGISTERED,
                "detail": "fixture",
            }
        )
        payload = self.cli.contract_lint_payload(self._root_with_baseline(entries))
        self.assertTrue(payload["ok"], "a resolved entry must not fail the gate")
        self.assertEqual(
            payload["resolved_baseline_entries"],
            [{"id": "runtime:long-gone", "kind": CC.GAP_LIVE_COMMAND_UNREGISTERED}],
        )

    def test_a_new_policy_finding_fails_the_lint(self) -> None:
        """The destructive half of the ratchet reaches the same command."""
        from runtime_manager import command_registry as cr

        downgraded = cr.CommandSpec(
            id="box.down",
            tier=2,
            surface=("cli",),
            summary="fixture",
            inputs={},
            outputs={},
            side_effect="destructive",
            risk="low",
            entrypoint="box.py",
            examples=(),
        )
        with mock.patch.object(
            cr, "default_registry", return_value=[downgraded]
        ):
            payload = self.cli.contract_lint_payload(ROOT_DIR)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["new_policy_findings"])
        invariants = {f["invariant"] for f in payload["new_policy_findings"]}
        self.assertIn(CC.INVARIANT_RISK, invariants)
        for finding in payload["new_policy_findings"]:
            self.assertEqual(
                sorted(finding),
                ["detail", "fix", "invariant", "owner", "source", "surface"],
            )

    def test_next_actions_tell_the_reader_what_to_do(self) -> None:
        clean = self.cli.contract_lint_payload(ROOT_DIR)
        self.assertEqual(clean["next_actions"], ["no new contract drift"])
        drifted = self.cli.contract_lint_payload(self._root_with_baseline([]))
        self.assertTrue(drifted["next_actions"])
        self.assertIn("command_contract_gaps.json", " ".join(drifted["next_actions"]))

    def test_the_payload_is_deterministic(self) -> None:
        first = self.cli.contract_lint_payload(ROOT_DIR)
        second = self.cli.contract_lint_payload(ROOT_DIR)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
