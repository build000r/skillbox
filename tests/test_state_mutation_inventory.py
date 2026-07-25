"""Ratchet for the state-root mutation boundary manifest.

These tests exist to make one specific failure impossible: a new public surface
landing without anyone deciding whether it mutates state, and a new wrapper
reaching a mutating entrypoint without declaring it.

Three families:

1. **Coverage** — every live ``manage`` / ``pulse`` / ``box`` / operator-MCP /
   Make surface has exactly one manifest row, and every manifest row still
   corresponds to a live surface.
2. **Shape** — every mutation carries a real boundary ID, entry points, a
   canonical state-root source, a dry-run predicate, a nested-call policy, an
   intended lease span, and a final lock owner; rendering is byte-stable.
3. **Synthetic bypass fixtures** — a fabricated "new command" and a fabricated
   Makefile wrapper that shells out to a mutating entrypoint must both be
   detected. If these ever stop failing, the ratchet is broken and the rest of
   this file proves nothing.

Run it the way the rest of the tree is run::

    python3 -m unittest tests.test_state_mutation_inventory
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import state_mutation as SM  # noqa: E402


class CoverageTests(unittest.TestCase):
    """Zero unclassified public surfaces, in either direction."""

    def test_every_live_surface_is_classified(self) -> None:
        report = SM.coverage_report(ROOT_DIR)
        self.assertEqual(
            report["unclassified"],
            (),
            "new public surface(s) landed without a state-mutation classification; "
            "add a row to runtime_manager.state_mutation.MANIFEST",
        )

    def test_manifest_names_no_surface_that_no_longer_exists(self) -> None:
        report = SM.coverage_report(ROOT_DIR)
        self.assertEqual(report["stale"], (), "manifest classifies a surface that is gone")

    def test_coverage_report_is_ok_and_totals_agree(self) -> None:
        report = SM.coverage_report(ROOT_DIR)
        self.assertTrue(report["ok"])
        self.assertEqual(report["total_live"], report["total_classified"])
        self.assertEqual(report["total_classified"], len(SM.MANIFEST))

    def test_every_declared_surface_kind_is_covered(self) -> None:
        live = SM.enumerate_live_surfaces(ROOT_DIR)
        for surface in SM.SURFACE_KINDS:
            with self.subTest(surface=surface):
                self.assertTrue(live[surface], f"{surface} enumerated to nothing — enumerator broke")

    def test_live_enumeration_is_deterministic(self) -> None:
        self.assertEqual(
            SM.enumerate_live_surfaces(ROOT_DIR),
            SM.enumerate_live_surfaces(ROOT_DIR),
        )

    def test_known_anchor_surfaces_are_present(self) -> None:
        """Guard the enumerators themselves: an enumerator that silently returns
        a subset would make the coverage tests vacuous."""
        live = SM.enumerate_live_surfaces(ROOT_DIR)
        self.assertIn("state-backup restore", live[SM.SURFACE_MANAGE])
        self.assertIn("focus", live[SM.SURFACE_MANAGE])
        self.assertIn("skill default", live[SM.SURFACE_MANAGE])
        self.assertIn("status", live[SM.SURFACE_PULSE])
        self.assertIn("inventory-rebuild", live[SM.SURFACE_BOX])
        self.assertIn("operator_teardown", live[SM.SURFACE_OPERATOR_MCP])
        self.assertIn("box-down", live[SM.SURFACE_MAKE])


class BoundaryShapeTests(unittest.TestCase):
    """Every mutation carries the fields a future lease needs."""

    _REQUIRED_ON_MUTATIONS = (
        "state_root_source",
        "dry_run_predicate",
        "nested_call_policy",
        "lease_span",
        "lock_owner",
    )

    def test_boundary_ids_are_unique_and_stable_shaped(self) -> None:
        ids = SM.boundary_ids()
        self.assertEqual(len(ids), len(set(ids)))
        for entry in SM.MANIFEST:
            with self.subTest(boundary=entry.boundary_id):
                self.assertEqual(
                    entry.boundary_id, f"{entry.surface}.{entry.key.replace(' ', '.')}"
                )

    def test_classifications_are_from_the_closed_vocabulary(self) -> None:
        for entry in SM.MANIFEST:
            with self.subTest(boundary=entry.boundary_id):
                self.assertIn(entry.classification, SM.CLASSIFICATIONS)
                self.assertIn(entry.surface, SM.SURFACE_KINDS)

    def test_every_boundary_declares_entry_points_and_evidence(self) -> None:
        for entry in SM.MANIFEST:
            with self.subTest(boundary=entry.boundary_id):
                self.assertTrue(entry.entry_points, "no entry points")
                self.assertTrue(entry.evidence, "no evidence citation")

    def test_every_mutation_carries_the_full_contract(self) -> None:
        for entry in SM.mutations():
            with self.subTest(boundary=entry.boundary_id):
                for field_name in self._REQUIRED_ON_MUTATIONS:
                    value = str(getattr(entry, field_name) or "").strip()
                    self.assertTrue(value, f"{field_name} is empty")
                    self.assertNotEqual(value, "n/a", f"{field_name} is n/a on a mutation")
                self.assertTrue(entry.writes, "mutation declares nothing it writes")

    def test_every_mutation_names_a_known_state_root_source(self) -> None:
        for entry in SM.mutations():
            with self.subTest(boundary=entry.boundary_id):
                self.assertIn(entry.state_root_source, SM.STATE_ROOT_SOURCES)

    def test_reads_do_not_claim_a_lease_or_a_lock(self) -> None:
        for entry in SM.MANIFEST:
            if entry.classification != SM.READ:
                continue
            with self.subTest(boundary=entry.boundary_id):
                self.assertEqual(entry.lease_span, "n/a")
                self.assertEqual(entry.lock_owner, "n/a")
                self.assertEqual(entry.writes, ())

    def test_delegates_to_only_names_known_boundary_ids(self) -> None:
        known = set(SM.boundary_ids())
        for entry in SM.MANIFEST:
            for delegate in entry.delegates_to:
                with self.subTest(boundary=entry.boundary_id, delegate=delegate):
                    self.assertIn(delegate, known)

    def test_all_four_classifications_are_actually_used(self) -> None:
        counts = SM.classification_counts()
        for name in SM.CLASSIFICATIONS:
            with self.subTest(classification=name):
                self.assertGreater(counts[name], 0)
        self.assertEqual(sum(counts.values()), len(SM.MANIFEST))


class OwnedGapTests(unittest.TestCase):
    """An honest recorded gap is the correct outcome, not a failure to hide."""

    def test_gaps_are_recorded_with_a_reason(self) -> None:
        for entry in SM.owned_gaps():
            with self.subTest(boundary=entry.boundary_id):
                self.assertTrue(entry.gap.startswith("BLOCKING:"), "gap must be marked BLOCKING")
                self.assertGreater(len(entry.gap), 80, "gap reason must be specific")

    def test_gaps_are_classified_pessimistically_as_mutations(self) -> None:
        for entry in SM.owned_gaps():
            with self.subTest(boundary=entry.boundary_id):
                self.assertTrue(entry.is_mutation)

    def test_inventory_is_not_complete_while_a_gap_is_open(self) -> None:
        self.assertEqual(SM.inventory_complete(), not SM.owned_gaps())
        if SM.owned_gaps():
            self.assertFalse(SM.inventory_complete())

    def test_manifest_payload_surfaces_the_gaps(self) -> None:
        payload = SM.manifest_payload()
        self.assertEqual(
            payload["owned_gaps"], [entry.boundary_id for entry in SM.owned_gaps()]
        )
        self.assertEqual(payload["inventory_complete"], SM.inventory_complete())


class DeterminismTests(unittest.TestCase):
    """Rendering the manifest twice must be byte-identical."""

    def test_json_render_is_byte_stable(self) -> None:
        self.assertEqual(SM.render_manifest(), SM.render_manifest())

    def test_text_render_is_byte_stable(self) -> None:
        self.assertEqual(SM.render_manifest_text(), SM.render_manifest_text())

    def test_render_is_parseable_and_round_trips(self) -> None:
        payload = json.loads(SM.render_manifest())
        self.assertEqual(payload["total"], len(SM.MANIFEST))
        self.assertEqual(len(payload["boundaries"]), len(SM.MANIFEST))

    def test_manifest_order_is_sorted_not_dict_iteration_order(self) -> None:
        keys = [(entry.surface, entry.key) for entry in SM.MANIFEST]
        self.assertEqual(keys, sorted(keys))

    def test_render_contains_no_timestamp_or_absolute_host_path(self) -> None:
        rendered = SM.render_manifest()
        self.assertNotIn(str(ROOT_DIR), rendered)
        self.assertNotIn(str(Path.home()), rendered)
        for marker in ("20", "T00:", "Z\","):
            if marker == "20":
                continue
            self.assertNotIn(marker, rendered)

    def test_payload_key_order_is_fixed(self) -> None:
        payload = SM.manifest_payload()
        first = payload["boundaries"][0]
        self.assertEqual(tuple(first), SM._PAYLOAD_KEY_ORDER)  # noqa: SLF001


class SyntheticNewCommandFixtureTests(unittest.TestCase):
    """A fabricated new surface must fail coverage on every surface kind."""

    def _assert_new_key_is_caught(self, surface: str, enumerator: str, fake: str) -> None:
        live = SM.enumerate_live_surfaces(ROOT_DIR)
        patched = tuple(sorted(set(live[surface]) | {fake}))
        with mock.patch.object(SM, enumerator, return_value=patched):
            report = SM.coverage_report(ROOT_DIR)
        self.assertFalse(report["ok"], f"{surface} ratchet did not fire")
        self.assertIn(f"{surface}:{fake}", report["unclassified"])
        self.assertEqual(report["stale"], ())

    def test_new_manage_command_is_unclassified(self) -> None:
        self._assert_new_key_is_caught(
            SM.SURFACE_MANAGE, "enumerate_manage_surfaces", "zz-synthetic-nuke"
        )

    def test_new_manage_subaction_is_unclassified(self) -> None:
        self._assert_new_key_is_caught(
            SM.SURFACE_MANAGE, "enumerate_manage_surfaces", "state-backup zz-synthetic-purge"
        )

    def test_new_pulse_command_is_unclassified(self) -> None:
        self._assert_new_key_is_caught(
            SM.SURFACE_PULSE, "enumerate_pulse_surfaces", "zz-synthetic-reap"
        )

    def test_new_box_command_is_unclassified(self) -> None:
        self._assert_new_key_is_caught(
            SM.SURFACE_BOX, "enumerate_box_surfaces", "zz-synthetic-wipe"
        )

    def test_new_operator_mcp_tool_is_unclassified(self) -> None:
        self._assert_new_key_is_caught(
            SM.SURFACE_OPERATOR_MCP, "enumerate_operator_mcp_surfaces", "operator_zz_synthetic"
        )

    def test_new_make_target_is_unclassified(self) -> None:
        self._assert_new_key_is_caught(
            SM.SURFACE_MAKE, "enumerate_make_surfaces", "zz-synthetic-target"
        )

    def test_removed_surface_is_reported_stale(self) -> None:
        live = SM.enumerate_live_surfaces(ROOT_DIR)
        shrunk = tuple(key for key in live[SM.SURFACE_PULSE] if key != "status")
        with mock.patch.object(SM, "enumerate_pulse_surfaces", return_value=shrunk):
            report = SM.coverage_report(ROOT_DIR)
        self.assertFalse(report["ok"])
        self.assertIn("pulse:status", report["stale"])


class SyntheticWrapperBypassFixtureTests(unittest.TestCase):
    """A wrapper that reaches a mutating entrypoint must be detected.

    Make adds no gating of its own — ``make box-down BOX=id`` destroys
    infrastructure without ever touching the operator MCP dry-run marker. So a
    new target that shells out to a mutating entrypoint has to be declared.
    """

    def test_the_real_makefile_has_no_undeclared_wrapper(self) -> None:
        self.assertEqual(SM.detect_wrapper_bypass(ROOT_DIR), ())

    def test_undeclared_new_target_invoking_box_down_is_caught(self) -> None:
        fake = {
            "zz-sneaky-teardown": ["\t@python3 scripts/box.py down $(BOX_ARGS)"],
        }
        with mock.patch.object(SM, "_make_recipes", return_value=fake):
            findings = SM.detect_wrapper_bypass(ROOT_DIR)
        self.assertTrue(findings, "wrapper-bypass detector did not fire")
        self.assertIn(
            "make:zz-sneaky-teardown invokes ['box.down'] but has no manifest row", findings
        )

    def test_undeclared_new_target_invoking_manage_sync_is_caught(self) -> None:
        fake = {
            "zz-sneaky-sync": ["\t@python3 .env-manager/manage.py sync --client personal"],
        }
        with mock.patch.object(SM, "_make_recipes", return_value=fake):
            findings = SM.detect_wrapper_bypass(ROOT_DIR)
        self.assertIn(
            "make:zz-sneaky-sync invokes ['manage.sync'] but has no manifest row", findings
        )

    def test_existing_target_gaining_an_undeclared_delegate_is_caught(self) -> None:
        """The nastier case: a classified read target quietly grows a mutation."""
        recipes = dict(SM._make_recipes(ROOT_DIR))  # noqa: SLF001
        recipes["box-list"] = list(recipes["box-list"]) + [
            "\t@python3 scripts/box.py inventory-rebuild --from-journal"
        ]
        with mock.patch.object(SM, "_make_recipes", return_value=recipes):
            findings = SM.detect_wrapper_bypass(ROOT_DIR)
        self.assertIn(
            "make:box-list invokes box.inventory-rebuild but does not declare it in delegates_to",
            findings,
        )

    def test_pulse_wrapper_bypass_is_caught(self) -> None:
        fake = {"zz-sneaky-pulse": ["\t@python3 .env-manager/pulse.py start"]}
        with mock.patch.object(SM, "_make_recipes", return_value=fake):
            findings = SM.detect_wrapper_bypass(ROOT_DIR)
        self.assertIn(
            "make:zz-sneaky-pulse invokes ['pulse.start'] but has no manifest row", findings
        )

    def test_unknown_declared_boundary_id_is_caught(self) -> None:
        bogus = SM.Boundary(
            boundary_id="make.zz-bogus",
            surface=SM.SURFACE_MAKE,
            key="zz-bogus",
            classification=SM.READ,
            entry_points=("make zz-bogus",),
            evidence=("synthetic",),
            delegates_to=("manage.does-not-exist",),
        )
        with mock.patch.object(SM, "MANIFEST", SM.MANIFEST + (bogus,)):
            findings = SM.detect_wrapper_bypass(ROOT_DIR)
        self.assertIn("make.zz-bogus declares unknown boundary id manage.does-not-exist", findings)


class VerifiedSubstrateTests(unittest.TestCase):
    """Pin the facts this inventory was built on, so they cannot rot silently.

    These read source text only. They execute nothing.
    """

    def _read(self, relative: str) -> str:
        return (ROOT_DIR / relative).read_text(encoding="utf-8")

    def test_locked_json_update_is_per_file_not_per_state_root(self) -> None:
        source = self._read(".env-manager/runtime_manager/_shared/fs.py")
        self.assertIn("def locked_json_update(", source)
        self.assertIn('lock_path = path.with_name(path.name + ".lock")', source)

    def test_locked_inventory_update_is_per_file_not_per_state_root(self) -> None:
        source = self._read("scripts/lib/opslib.py")
        self.assertIn("def locked_inventory_update(", source)
        self.assertIn('lock_path = target.with_name(target.name + ".lock")', source)

    def test_focus_and_pulse_use_different_sidecars(self) -> None:
        runtime_ops = self._read(".env-manager/runtime_manager/runtime_ops.py")
        pulse = self._read(".env-manager/pulse.py")
        workflows = self._read(".env-manager/runtime_manager/workflows.py")
        self.assertIn('FOCUS_STATE_REL = Path("workspace") / ".focus.json"', runtime_ops)
        self.assertIn("def pulse_state_path(root_dir: Path) -> Path:", pulse)
        # Both claim mutual serialization in comments; both are wrong.
        self.assertIn("the pulse-write", workflows)
        self.assertIn("focus writers", pulse)
        focus = SM.boundary("manage.focus")
        pulse_run = SM.boundary("pulse.run")
        self.assertNotEqual(focus.lock_owner, pulse_run.lock_owner)
        self.assertIn("PER-FILE ONLY", focus.lock_owner)
        self.assertIn("PER-FILE ONLY", pulse_run.lock_owner)

    def test_sessions_workers_drill_and_restore_write_outside_the_helpers(self) -> None:
        for relative in (
            ".env-manager/runtime_manager/_shared/session.py",
            ".env-manager/runtime_manager/_shared/worker.py",
            ".env-manager/runtime_manager/state_backup.py",
        ):
            with self.subTest(module=relative):
                source = self._read(relative)
                self.assertNotIn("locked_json_update", source)
                self.assertNotIn("locked_inventory_update", source)
        for boundary_id in (
            "manage.session-start",
            "manage.worker-submit",
            "manage.state-backup.drill",
            "manage.state-backup.restore",
        ):
            with self.subTest(boundary=boundary_id):
                self.assertIn("UNOWNED", SM.boundary(boundary_id).lock_owner)

    def test_state_root_resolvers_genuinely_disagree(self) -> None:
        """Resolvers disagree on the fallback. Recorded, not fixed.

        The original sweep labelled box.py and operator_mcp_server.py
        CWD-RELATIVE on the strength of their './.skillbox-state' default.
        Both actually follow it with 'if not base.is_absolute():
        base = REPO_ROOT / base', so both are REPO-relative. Corrected while
        implementing the lease (skillbox-duel-state-root-mutation-lease-2py0),
        which is why the counts below favour repo-relative.
        """
        cwd_relative = [
            name
            for name, text in SM.STATE_ROOT_SOURCES.items()
            if "CWD-RELATIVE" in text
        ]
        repo_relative = [
            name
            for name, text in SM.STATE_ROOT_SOURCES.items()
            if "REPO-RELATIVE" in text
        ]
        self.assertGreaterEqual(len(cwd_relative), 1)
        self.assertGreaterEqual(len(repo_relative), 3)
        # The disagreement itself is the point: both spellings coexist.
        self.assertTrue(cwd_relative and repo_relative)

    # test_no_locking_is_implemented_here was retired deliberately.
    #
    # It encoded this bead's own non-goal ("this module inventories, it never
    # locks"), which skillbox-duel-state-root-mutation-lease-2py0 was chartered
    # to overturn: that bead's binding write list puts the lease in this exact
    # module and requires LOCK_EX|LOCK_NB. The two contracts cannot both hold,
    # and the newer one supersedes.
    #
    # The guard is not replaced by a weaker substring check, because the
    # properties it was standing in for are now asserted directly and much
    # more strongly in tests/test_state_mutation_lock.py: no read boundary may
    # take a lease, the public namespace exposes no clear/steal/break/force/
    # unlink/reset/revoke verb, the lease source contains no unlink/remove/
    # rmtree, and flock unavailability fails closed rather than degrading.


class KnownRiskAssertionTests(unittest.TestCase):
    """Spot-check the classifications that are easiest to get wrong."""

    def test_pulse_status_is_not_read_only(self) -> None:
        entry = SM.boundary("pulse.status")
        self.assertEqual(entry.classification, SM.CONDITIONAL_MUTATION)
        self.assertIn("pid_path.unlink()", entry.dry_run_predicate)

    def test_state_backup_list_creates_the_backup_root(self) -> None:
        entry = SM.boundary("manage.state-backup.list")
        self.assertEqual(entry.classification, SM.CONDITIONAL_MUTATION)

    def test_state_backup_drill_always_writes_evidence(self) -> None:
        self.assertEqual(
            SM.boundary("manage.state-backup.drill").classification,
            SM.UNCONDITIONAL_MUTATION,
        )

    def test_worker_status_can_write(self) -> None:
        entry = SM.boundary("manage.worker-status")
        self.assertEqual(entry.classification, SM.CONDITIONAL_MUTATION)
        self.assertIn("observed-state predicate", entry.dry_run_predicate)
        self.assertIn(
            "OBSERVED RUN STATE", SM.boundary("manage.worker-artifacts").dry_run_predicate
        )

    def test_fleet_skill_default_dry_run_is_not_write_free(self) -> None:
        entry = SM.boundary("manage.skill.default")
        self.assertEqual(entry.classification, SM.CONDITIONAL_MUTATION)
        self.assertIn("STILL WRITES a review marker", entry.dry_run_predicate)

    def test_box_inventory_rebuild_has_no_preview(self) -> None:
        entry = SM.boundary("box.inventory-rebuild")
        self.assertEqual(entry.classification, SM.UNCONDITIONAL_MUTATION)
        self.assertIn("not a preview", entry.dry_run_predicate)

    def test_operator_compose_up_is_the_ungated_mcp_mutator(self) -> None:
        entry = SM.boundary("operator_mcp.operator_compose_up")
        self.assertEqual(entry.classification, SM.UNCONDITIONAL_MUTATION)
        self.assertIn("no dry_run parameter", entry.dry_run_predicate)

    def test_self_test_is_the_only_real_cross_process_lease(self) -> None:
        leased = [
            entry.boundary_id
            for entry in SM.mutations()
            if "flock ${SKILLBOX_STATE_ROOT}" in entry.lock_owner
        ]
        self.assertEqual(
            sorted(leased),
            ["make.self-test", "make.self-test-refresh", "make.self-test-worktree"],
        )

    def test_make_is_never_a_control_point(self) -> None:
        """Every Make target that merely forwards to a CLI inherits its blast
        radius verbatim — Make itself gates nothing."""
        forwarding = [
            entry
            for entry in SM.MANIFEST
            if entry.surface == SM.SURFACE_MAKE
            and entry.is_mutation
            and entry.lease_span.startswith("inherited from")
        ]
        self.assertGreaterEqual(len(forwarding), 15)
        for entry in forwarding:
            with self.subTest(boundary=entry.boundary_id):
                self.assertIn("Make adds no gating of its own", entry.dry_run_predicate)
                self.assertTrue(entry.delegates_to or entry.boundary_id == "make.box-ssh")


if __name__ == "__main__":
    unittest.main()
