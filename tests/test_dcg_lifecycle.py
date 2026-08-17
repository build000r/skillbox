"""Contract tests for the ONE DCG lifecycle API every entrypoint calls.

The reconcile leaf (atomicity, merge safety, backups, the ledger) is proved in
``tests/test_dcg_reconcile.py``. This module proves the layer above it:

1. one API        -- install, first-box, onboard, runtime-sync, and box deploy
                     all converge through ``dcg_lifecycle.converge``
2. explicit scope -- scope and home are required arguments, never inferred, and
                     this module never reads the real ``$HOME``
3. no optional    -- runtime-sync emits a ``dcg-reconcile`` action instead of
   skip              the dcg-bin optional-binary skip, and a reconciler failure
                     makes the caller nonzero
4. the trust gate -- a prepared-but-untrusted Codex hook is
                     needs-operator-action with a nonzero exit, never healthy;
                     a rerun after Codex persists trust becomes healthy and
                     unchanged; the bypass flag is refused
5. removal        -- an explicit relinquish path exists and is safe to run twice

Every home here is a disposable temp tree materialized from
``tests/fixtures/dcg_reconcile``.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
SCRIPTS_DIR = ROOT_DIR / "scripts"
for _path in (ENV_MANAGER_DIR, SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from runtime_manager import dcg_lifecycle as DL  # noqa: E402
from runtime_manager import dcg_reconcile as DR  # noqa: E402
from runtime_manager import runtime_ops as RO  # noqa: E402
from runtime_manager.errors import RuntimeLifecycleError, ValidationError  # noqa: E402

FIXTURES = ROOT_DIR / "tests" / "fixtures" / "dcg_reconcile"
BIN_TOKEN = "@DCG_BIN@"


class _LifecycleCase(unittest.TestCase):
    """Materializes a disposable fixture home; never touches the real $HOME."""

    def materialize(self, case: str = "container_home") -> tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="dcg-lifecycle-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "home"
        source = FIXTURES / case / "home"
        if source.is_dir():
            shutil.copytree(source, home, symlinks=True)
        else:
            home.mkdir(parents=True)
        binary = home / DR.DEFAULT_BINARY_RELPATH
        for path in sorted(home.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if BIN_TOKEN in text:
                path.write_text(text.replace(BIN_TOKEN, str(binary)), encoding="utf-8")
        if binary.is_file():
            binary.chmod(0o755)
        return home, binary

    def persist_codex_trust(self, home: Path, value: str = "b" * 64) -> None:
        """Write the trust Codex itself would persist after its review modal.

        Skillbox never writes this: the reconciler only ever reads it. The test
        writes it to stand in for the operator having trusted the hook.
        """
        config = home / DR.CODEX_CONFIG_RELPATH
        config.parent.mkdir(parents=True, exist_ok=True)
        base = config.read_text(encoding="utf-8") if config.is_file() else 'model = "gpt-5.6-sol"\n'
        base = base.split("[hooks.state.")[0].rstrip("\n")
        config.write_text(
            base + f'\n\n[hooks.state."user:PreToolUse:0"]\nenabled = true\ntrusted_hash = "{value}"\n',
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# One API, explicitly scoped
# ---------------------------------------------------------------------------


class LifecycleContractTests(_LifecycleCase):
    def test_every_setup_and_deploy_entrypoint_is_registered(self) -> None:
        # The bead contract is that all five converge through this module. If a
        # sixth entrypoint appears it has to be declared here, which is what
        # makes "every entrypoint calls the same contract" checkable.
        self.assertEqual(
            set(DL.ENTRYPOINTS),
            {"install", "first-box", "onboard", "runtime-sync", "box-deploy"},
        )

    def test_scope_is_required_and_never_guessed(self) -> None:
        home, binary = self.materialize()
        for bad_scope in ("", "guess", "HOST", None):
            with self.subTest(scope=bad_scope):
                with self.assertRaises(ValidationError) as raised:
                    DL.converge(
                        entrypoint=DL.ENTRYPOINT_INSTALL,
                        scope=bad_scope,  # type: ignore[arg-type]
                        home=home,
                        binary=binary,
                        action=DL.ACTION_VERIFY,
                    )
                self.assertEqual(
                    raised.exception.code, DL.DCG_LIFECYCLE_UNKNOWN_SCOPE
                )

    def test_host_and_container_are_both_accepted_scopes(self) -> None:
        home, binary = self.materialize()
        for scope in (DL.SCOPE_HOST, DL.SCOPE_CONTAINER):
            with self.subTest(scope=scope):
                payload = DL.converge(
                    entrypoint=DL.ENTRYPOINT_ONBOARD,
                    scope=scope,
                    home=home,
                    binary=binary,
                    action=DL.ACTION_VERIFY,
                )
                self.assertEqual(payload["scope"], scope)

    def test_home_is_never_inferred(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            DL.converge(
                entrypoint=DL.ENTRYPOINT_INSTALL,
                scope=DL.SCOPE_HOST,
                home="",
                action=DL.ACTION_VERIFY,
            )
        self.assertEqual(raised.exception.code, DL.DCG_LIFECYCLE_HOME_REQUIRED)

    def test_unknown_entrypoint_and_action_are_refused(self) -> None:
        home, _ = self.materialize()
        with self.assertRaises(ValidationError) as raised:
            DL.converge(
                entrypoint="ad-hoc-script",
                scope=DL.SCOPE_HOST,
                home=home,
                action=DL.ACTION_VERIFY,
            )
        self.assertEqual(raised.exception.code, DL.DCG_LIFECYCLE_UNKNOWN_ENTRYPOINT)

        with self.assertRaises(ValidationError) as raised:
            DL.converge(
                entrypoint=DL.ENTRYPOINT_INSTALL,
                scope=DL.SCOPE_HOST,
                home=home,
                action="rollback",
            )
        self.assertEqual(raised.exception.code, DL.DCG_LIFECYCLE_UNKNOWN_ACTION)

    def test_payload_always_carries_the_caller_contract_fields(self) -> None:
        home, binary = self.materialize()
        payload = DL.converge(
            entrypoint=DL.ENTRYPOINT_BOX_DEPLOY,
            scope=DL.SCOPE_HOST,
            home=home,
            binary=binary,
            action=DL.ACTION_VERIFY,
        )
        for key in ("entrypoint", "scope", "marker", "exit_code", "ok", "status"):
            self.assertIn(key, payload)

    def test_a_reconciler_error_becomes_a_failed_payload_not_an_exception(self) -> None:
        # Callers record a step and then exit nonzero; they should not each need
        # their own try/except around the reconciler.
        home, binary = self.materialize()
        with mock.patch.object(
            DR, "verify", side_effect=ValidationError("DCG_X", "boom")
        ):
            payload = DL.converge(
                entrypoint=DL.ENTRYPOINT_INSTALL,
                scope=DL.SCOPE_HOST,
                home=home,
                binary=binary,
                action=DL.ACTION_VERIFY,
            )
        self.assertEqual(payload["status"], DL.STATE_FAILED)
        self.assertEqual(payload["marker"], DL.MARKER_FAILED)
        self.assertEqual(payload["exit_code"], DL.EXIT_FAILED)
        self.assertFalse(payload["ok"])


# ---------------------------------------------------------------------------
# The Codex trust gate
# ---------------------------------------------------------------------------


class CodexTrustGateTests(_LifecycleCase):
    def test_fresh_setup_awaiting_trust_is_needs_operator_action_never_healthy(
        self,
    ) -> None:
        home, binary = self.materialize()
        payload = DL.converge(
            entrypoint=DL.ENTRYPOINT_INSTALL,
            scope=DL.SCOPE_HOST,
            home=home,
            binary=binary,
        )
        self.assertEqual(payload["status"], DL.STATE_NEEDS_OPERATOR)
        self.assertNotEqual(payload["status"], DL.STATE_HEALTHY)
        self.assertEqual(payload["marker"], DL.MARKER_NEEDS_OPERATOR_ACTION)
        self.assertFalse(DL.is_healthy(payload))
        # Nonzero, so install --verify and every `&&` chain fails.
        self.assertNotEqual(payload["exit_code"], DL.EXIT_OK)
        self.assertEqual(payload["exit_code"], DL.EXIT_NEEDS_OPERATOR)
        self.assertTrue(payload["operator_actions"])

    def test_rerun_after_matching_trust_is_healthy_and_unchanged(self) -> None:
        home, binary = self.materialize()
        first = DL.converge(
            entrypoint=DL.ENTRYPOINT_FIRST_BOX,
            scope=DL.SCOPE_HOST,
            home=home,
            binary=binary,
        )
        self.assertEqual(first["status"], DL.STATE_NEEDS_OPERATOR)

        self.persist_codex_trust(home)

        second = DL.converge(
            entrypoint=DL.ENTRYPOINT_FIRST_BOX,
            scope=DL.SCOPE_HOST,
            home=home,
            binary=binary,
        )
        self.assertEqual(second["status"], DL.STATE_HEALTHY)
        self.assertEqual(second["marker"], DL.MARKER_HEALTHY)
        self.assertEqual(second["exit_code"], DL.EXIT_OK)
        self.assertTrue(second["ok"])
        # "become no-ops on rerun": the second pass rewrites nothing.
        self.assertEqual(second["result"], DR.RESULT_UNCHANGED)

    def test_a_third_run_is_still_an_unchanged_no_op(self) -> None:
        home, binary = self.materialize()
        DL.converge(
            entrypoint=DL.ENTRYPOINT_ONBOARD, scope=DL.SCOPE_HOST, home=home, binary=binary
        )
        self.persist_codex_trust(home)
        for _ in range(2):
            payload = DL.converge(
                entrypoint=DL.ENTRYPOINT_ONBOARD,
                scope=DL.SCOPE_HOST,
                home=home,
                binary=binary,
            )
            self.assertEqual(payload["result"], DR.RESULT_UNCHANGED)
            self.assertEqual(payload["status"], DL.STATE_HEALTHY)

    def test_verified_state_survives_a_container_replacement(self) -> None:
        """A replaced container must not silently look fresh and untrusted.

        The leaf-level proof (and the compose mount that makes it true) lives in
        ``tests/test_dcg_reconcile.py``. This is the same property asserted
        through the lifecycle API, because that is the surface box deploy and
        runtime-sync actually call: carry only the persisted subtrees across to
        a brand-new home and the next converge must still be healthy/unchanged,
        not a fresh needs-operator-action.
        """
        home, binary = self.materialize()
        DL.converge(
            entrypoint=DL.ENTRYPOINT_BOX_DEPLOY,
            scope=DL.SCOPE_CONTAINER,
            home=home,
            binary=binary,
        )
        self.persist_codex_trust(home)
        healthy = DL.converge(
            entrypoint=DL.ENTRYPOINT_BOX_DEPLOY,
            scope=DL.SCOPE_CONTAINER,
            home=home,
            binary=binary,
        )
        self.assertEqual(healthy["status"], DL.STATE_HEALTHY)

        # Replace the container: the home PATH is unchanged (/home/sandbox in
        # production), the mounted subtrees persist, and everything else in the
        # container filesystem is gone. Anything not on a mount must not be
        # required for the next converge to come back healthy.
        unmounted = home / ".cache" / "scratch.txt"
        unmounted.parent.mkdir(parents=True, exist_ok=True)
        unmounted.write_text("ephemeral container state\n", encoding="utf-8")

        mounted = {".claude", ".codex", ".grok", ".local", ".config"}
        for child in list(home.iterdir()):
            if child.name in mounted:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        self.assertFalse(unmounted.exists())

        after = DL.converge(
            entrypoint=DL.ENTRYPOINT_BOX_DEPLOY,
            scope=DL.SCOPE_CONTAINER,
            home=home,
            binary=binary,
        )
        self.assertEqual(after["status"], DL.STATE_HEALTHY)
        self.assertEqual(after["result"], DR.RESULT_UNCHANGED)
        self.assertEqual(after["codex_trust"], DR.CODEX_TRUST_TRUSTED)

    def test_bypass_flag_is_rejected_by_the_lifecycle_layer(self) -> None:
        home, _ = self.materialize()
        with self.assertRaises(ValidationError) as raised:
            DL.converge(
                entrypoint=DL.ENTRYPOINT_INSTALL,
                scope=DL.SCOPE_HOST,
                home=home,
                binary=f"/tmp/dcg {DR.BYPASS_FLAG}",
            )
        self.assertEqual(raised.exception.code, DL.DCG_LIFECYCLE_BYPASS_FORBIDDEN)

    def test_bypass_flag_is_rejected_by_the_lifecycle_cli_before_parsing(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = DL.main([DR.BYPASS_FLAG])
        self.assertEqual(code, DL.EXIT_FAILED)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["code"], DL.DCG_LIFECYCLE_BYPASS_FORBIDDEN)

    def test_reject_bypass_scans_every_value(self) -> None:
        with self.assertRaises(ValidationError):
            DL.reject_bypass(["--home", "/x", f"--binary=/y{DR.BYPASS_FLAG}"])
        DL.reject_bypass(["--home", "/x", "--scope", "host"])  # no raise


# ---------------------------------------------------------------------------
# Markers and exit codes
# ---------------------------------------------------------------------------


class MarkerTests(unittest.TestCase):
    def test_each_status_maps_to_its_stable_marker_and_exit_code(self) -> None:
        cases = (
            (DR.STATE_HEALTHY, DL.MARKER_HEALTHY, DL.EXIT_OK),
            (DR.STATE_CHANGED, DL.MARKER_CHANGED, DL.EXIT_OK),
            (DR.STATE_NEEDS_OPERATOR, DL.MARKER_NEEDS_OPERATOR_ACTION, DL.EXIT_NEEDS_OPERATOR),
            (DR.STATE_UNSUPPORTED, DL.MARKER_UNSUPPORTED, DL.EXIT_UNSUPPORTED),
            (DR.STATE_FAILED, DL.MARKER_FAILED, DL.EXIT_FAILED),
        )
        for status, expected_marker, expected_exit in cases:
            with self.subTest(status=status):
                payload = {"status": status, "result": DR.RESULT_UNCHANGED}
                self.assertEqual(DL.marker(payload), expected_marker)
                self.assertEqual(DL.exit_code(payload), expected_exit)

    def test_the_install_log_contract_markers_are_greppable(self) -> None:
        # install.sh's acceptance greps DCG_(CHANGED|NEEDS_OPERATOR_ACTION|HEALTHY);
        # renaming a marker silently breaks that contract.
        self.assertEqual(DL.MARKER_HEALTHY, "DCG_HEALTHY")
        self.assertEqual(DL.MARKER_CHANGED, "DCG_CHANGED")
        self.assertEqual(DL.MARKER_NEEDS_OPERATOR_ACTION, "DCG_NEEDS_OPERATOR_ACTION")


# ---------------------------------------------------------------------------
# Explicit removal
# ---------------------------------------------------------------------------


class RelinquishTests(_LifecycleCase):
    def test_relinquish_removes_dcg_state_and_is_safe_to_run_twice(self) -> None:
        home, binary = self.materialize()
        DL.converge(
            entrypoint=DL.ENTRYPOINT_BOX_DEPLOY,
            scope=DL.SCOPE_HOST,
            home=home,
            binary=binary,
        )

        first = DL.relinquish(
            entrypoint=DL.ENTRYPOINT_BOX_DEPLOY,
            scope=DL.SCOPE_HOST,
            home=home,
            binary=binary,
        )
        self.assertEqual(first["result"], DR.RESULT_REMOVED)
        self.assertEqual(first["marker"], DL.MARKER_REMOVED)

        second = DL.relinquish(
            entrypoint=DL.ENTRYPOINT_BOX_DEPLOY,
            scope=DL.SCOPE_HOST,
            home=home,
            binary=binary,
        )
        # Idempotent: the second pass finds nothing DCG-owned left to remove and
        # is NOT an error.
        self.assertNotEqual(second["status"], DL.STATE_FAILED)
        self.assertEqual(second["exit_code"], DL.EXIT_OK)

    def test_relinquish_is_reachable_as_an_action_and_via_the_helper(self) -> None:
        home, binary = self.materialize()
        DL.converge(
            entrypoint=DL.ENTRYPOINT_INSTALL, scope=DL.SCOPE_HOST, home=home, binary=binary
        )
        payload = DL.converge(
            entrypoint=DL.ENTRYPOINT_INSTALL,
            scope=DL.SCOPE_HOST,
            home=home,
            binary=binary,
            action=DL.ACTION_RELINQUISH,
        )
        self.assertEqual(payload["action"], "relinquish")


# ---------------------------------------------------------------------------
# The lifecycle CLI
# ---------------------------------------------------------------------------


class LifecycleCliTests(_LifecycleCase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = DL.main(argv)
        return code, buffer.getvalue()

    def test_verify_on_an_untrusted_home_exits_nonzero_with_the_marker(self) -> None:
        home, binary = self.materialize()
        code, output = self._run(
            [
                "verify",
                "--entrypoint", "install",
                "--scope", "host",
                "--home", str(home),
                "--binary", str(binary),
            ]
        )
        self.assertEqual(code, DL.EXIT_NEEDS_OPERATOR)
        self.assertIn(DL.MARKER_NEEDS_OPERATOR_ACTION, output)

    def test_json_format_emits_the_contract_fields(self) -> None:
        home, binary = self.materialize()
        code, output = self._run(
            [
                "verify",
                "--entrypoint", "box-deploy",
                "--scope", "container",
                "--home", str(home),
                "--binary", str(binary),
                "--format", "json",
            ]
        )
        payload = json.loads(output)
        self.assertEqual(payload["entrypoint"], "box-deploy")
        self.assertEqual(payload["scope"], "container")
        self.assertEqual(payload["marker"], DL.MARKER_NEEDS_OPERATOR_ACTION)
        self.assertEqual(code, payload["exit_code"])

    def test_home_or_from_model_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                DL.main(["verify", "--entrypoint", "install", "--scope", "host"])

    def test_home_and_from_model_are_mutually_exclusive(self) -> None:
        home, _ = self.materialize()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                DL.main(
                    [
                        "verify",
                        "--entrypoint", "install",
                        "--scope", "host",
                        "--home", str(home),
                        "--from-model", str(ROOT_DIR),
                    ]
                )


# ---------------------------------------------------------------------------
# runtime-sync wiring
# ---------------------------------------------------------------------------


class RuntimeSyncWiringTests(_LifecycleCase):
    def _model(self, home: Path, binary: Path) -> dict:
        return {
            "root_dir": str(home.parent),
            "repos": [],
            "artifacts": [{"id": RO.DCG_ARTIFACT_ID, "host_path": str(binary)}],
            "env_files": [],
            "logs": [],
        }

    def test_target_resolves_home_from_the_declared_binary(self) -> None:
        home, binary = self.materialize()
        resolved = RO.dcg_lifecycle_target(self._model(home, binary))
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved, (home, binary))

    def test_target_is_none_when_the_model_declares_no_dcg_binary(self) -> None:
        self.assertIsNone(RO.dcg_lifecycle_target({"artifacts": []}))

    def test_target_is_none_for_a_binary_outside_a_managed_home(self) -> None:
        # /usr/bin/dcg is not `<home>/.local/bin/dcg`, so there is no home to
        # converge and the lifecycle must not invent one.
        model = {"artifacts": [{"id": RO.DCG_ARTIFACT_ID, "host_path": "/usr/bin/dcg"}]}
        self.assertIsNone(RO.dcg_lifecycle_target(model))

    def test_sync_emits_a_dcg_reconcile_action(self) -> None:
        home, binary = self.materialize()
        lines = RO.sync_dcg_reconcile(self._model(home, binary), dry_run=True)
        self.assertTrue(lines)
        self.assertTrue(lines[0].startswith("dcg-reconcile:"))
        self.assertIn(str(home), lines[0])
        self.assertIn("host scope", lines[0])

    def test_sync_never_emits_an_optional_binary_skip_for_dcg(self) -> None:
        home, binary = self.materialize()
        lines = RO.sync_dcg_reconcile(self._model(home, binary), dry_run=True)
        joined = "\n".join(lines)
        self.assertNotIn("artifact source url missing", joined)
        self.assertNotIn("sync mode manual", joined)

    def test_a_reconciler_failure_makes_sync_raise(self) -> None:
        # failure_probe: injecting a reconciler failure must make the caller
        # nonzero rather than log a skip and continue.
        home, binary = self.materialize()
        with mock.patch.object(
            DR, "apply", side_effect=ValidationError("DCG_X", "injected failure")
        ):
            with self.assertRaises(RuntimeLifecycleError):
                RO.sync_dcg_reconcile(self._model(home, binary), dry_run=False)

    def test_manual_mode_marks_dcg_bin_as_reconciler_owned(self) -> None:
        # sync.mode manual is runtime.yaml saying "the generic URL downloader
        # cannot verify this artifact's signature". That is the declaration
        # whose generic output is the optional-binary skip, so it -- and only
        # it -- is suppressed in favour of the lifecycle record.
        self.assertTrue(
            RO._dcg_artifact_is_reconciler_owned(
                {"id": RO.DCG_ARTIFACT_ID, "sync": {"mode": "manual"}}
            )
        )

    def test_a_syncable_dcg_bin_still_goes_through_the_normal_syncer(self) -> None:
        # No optional skip to suppress here, so suppressing the artifact record
        # would silently stop installing a binary the runtime CAN fetch.
        self.assertFalse(
            RO._dcg_artifact_is_reconciler_owned(
                {"id": RO.DCG_ARTIFACT_ID, "sync": {"mode": "copy-if-missing"}}
            )
        )
        self.assertFalse(RO._dcg_artifact_is_reconciler_owned({"id": RO.DCG_ARTIFACT_ID}))

    def test_other_artifacts_are_never_reconciler_owned(self) -> None:
        self.assertFalse(
            RO._dcg_artifact_is_reconciler_owned(
                {"id": "swimmers-bin", "sync": {"mode": "manual"}}
            )
        )

    def test_sync_records_replace_the_manual_skip_with_the_lifecycle_action(self) -> None:
        home, binary = self.materialize()
        model = {
            "root_dir": str(home.parent),
            "repos": [],
            "artifacts": [
                {
                    "id": RO.DCG_ARTIFACT_ID,
                    "host_path": str(binary),
                    "sync": {"mode": "manual"},
                }
            ],
            "env_files": [],
            "logs": [],
        }
        with (
            mock.patch.object(RO, "sync_port_contracts", return_value=[]),
            mock.patch.object(RO, "sync_skill_repo_sets", return_value=[]),
            mock.patch.object(RO, "_sync_distributor_sources", return_value=[]),
            mock.patch.object(RO, "sync_skill_sets", return_value=[]),
            mock.patch.object(RO, "sync_dcg_config", return_value=[]),
            mock.patch.object(RO, "sync_ingress_artifacts", return_value=[]),
        ):
            records = RO.sync_runtime_records(model, dry_run=True)

        ids = [record["id"] for record in records]
        self.assertIn("dcg-reconcile", ids)
        self.assertNotIn(RO.DCG_ARTIFACT_ID, ids)
        joined = "\n".join(record["text"] for record in records)
        self.assertNotIn("sync mode manual", joined)
        self.assertNotIn("artifact source url missing", joined)

    def test_sync_reports_operator_actions_instead_of_swallowing_them(self) -> None:
        home, binary = self.materialize()
        lines = RO.sync_dcg_reconcile(self._model(home, binary), dry_run=True)
        self.assertTrue(any(line.startswith("operator-action:") for line in lines))


# ---------------------------------------------------------------------------
# Entrypoint wiring: workflows and box deploy
# ---------------------------------------------------------------------------


class EntrypointWiringTests(_LifecycleCase):
    def test_workflows_helper_delegates_with_its_own_entrypoint_label(self) -> None:
        from runtime_manager import workflows

        home, binary = self.materialize()
        model = {"artifacts": [{"id": RO.DCG_ARTIFACT_ID, "host_path": str(binary)}]}
        payload = workflows.converge_dcg_for_model(
            model, entrypoint=DL.ENTRYPOINT_FIRST_BOX, dry_run=True
        )
        assert payload is not None
        self.assertEqual(payload["entrypoint"], DL.ENTRYPOINT_FIRST_BOX)
        self.assertEqual(payload["scope"], DL.SCOPE_HOST)
        self.assertEqual(Path(payload["home"]), home)

    def test_workflows_helper_returns_none_without_a_declared_binary(self) -> None:
        from runtime_manager import workflows

        self.assertIsNone(
            workflows.converge_dcg_for_model(
                {"artifacts": []}, entrypoint=DL.ENTRYPOINT_ONBOARD, dry_run=True
            )
        )

    def test_workflow_status_maps_the_trust_gate_to_a_failed_step(self) -> None:
        self.assertEqual(DL.workflow_status({"ok": True}), "ok")
        self.assertEqual(DL.workflow_status({"ok": False}), "fail")

    def test_step_detail_is_compact_and_omits_the_full_agent_payload(self) -> None:
        home, binary = self.materialize()
        payload = DL.converge(
            entrypoint=DL.ENTRYPOINT_ONBOARD,
            scope=DL.SCOPE_HOST,
            home=home,
            binary=binary,
            action=DL.ACTION_VERIFY,
        )
        detail = DL.step_detail(payload)
        self.assertEqual(detail["entrypoint"], DL.ENTRYPOINT_ONBOARD)
        self.assertEqual(detail["marker"], DL.MARKER_NEEDS_OPERATOR_ACTION)
        self.assertNotIn("agents", detail)

    # Box deploy's own wiring assertion lives in tests/test_box.py, next to the
    # existing build_deploy_command coverage.


if __name__ == "__main__":
    unittest.main()
