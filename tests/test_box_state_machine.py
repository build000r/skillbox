from __future__ import annotations

import subprocess
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"
BOX = SourceFileLoader(
    "skillbox_box_state_machine",
    str(BOX_SCRIPT.resolve()),
).load_module()


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["mock"], returncode, stdout=stdout, stderr=stderr)


def _storage() -> object:
    return BOX.BoxProfileStorage(
        provider="digitalocean",
        mount_path="/srv/skillbox",
        filesystem="ext4",
        required=True,
        min_free_gb=10,
    )


def _profile() -> object:
    return BOX.BoxProfile(id="dev-small", storage=_storage())


def _release() -> object:
    return BOX.DeployRelease(
        manifest_path=Path("/tmp/deploy.json"),
        client_id="box-1",
        source_commit="abc123def4567890",
        payload_tree_sha256="1" * 64,
        archive_path=Path("/tmp/skillbox.tar.gz"),
        archive_sha256="2" * 64,
        active_profiles=["core"],
    )


def _box(state: str, *, tailscale_ip: str | None = "100.64.0.8") -> object:
    return BOX.Box(
        id="box-1",
        profile="dev-small",
        state=state,
        droplet_id="123",
        droplet_ip="1.2.3.4",
        tailscale_hostname="skillbox-box-1",
        tailscale_ip=tailscale_ip,
        ssh_user="skillbox",
        state_root="/srv/skillbox",
        storage_provider="digitalocean",
        storage_filesystem="ext4",
        storage_required=True,
        storage_min_free_gb=10.0,
    )


def _resume_context(state: str = "ssh-ready") -> object:
    box = _box(state)
    return BOX._build_box_resume_context(
        existing=box,
        profile=_profile(),
        boxes=[box],
        is_json=True,
        deploy_release=_release(),
    )


class BoxStateTransitionTableTests(unittest.TestCase):
    def test_valid_transition_table_references_declared_states_only(self) -> None:
        states = set(BOX.STATES)

        self.assertEqual(len(BOX.STATES), len(states))
        self.assertLessEqual(set(BOX.VALID_TRANSITIONS), states)
        for from_state, targets in BOX.VALID_TRANSITIONS.items():
            with self.subTest(from_state=from_state):
                self.assertLessEqual(set(targets), states)

    def test_transition_validator_accepts_exactly_declared_pairs(self) -> None:
        valid_pairs = {
            (from_state, to_state)
            for from_state, targets in BOX.VALID_TRANSITIONS.items()
            for to_state in targets
        }

        for from_state in BOX.STATES:
            for to_state in BOX.STATES:
                with self.subTest(from_state=from_state, to_state=to_state):
                    if (from_state, to_state) in valid_pairs:
                        BOX.validate_box_state_transition(from_state, to_state)
                        self.assertTrue(BOX.box_state_transition_allowed(from_state, to_state))
                    else:
                        with self.assertRaises(BOX.BoxStateTransitionError) as raised:
                            BOX.validate_box_state_transition(from_state, to_state)
                        self.assertFalse(BOX.box_state_transition_allowed(from_state, to_state))
                        payload = raised.exception.payload
                        self.assertEqual(payload["error"]["type"], "invalid_state_transition")
                        self.assertEqual(payload["transition"]["from"], from_state)
                        self.assertEqual(payload["transition"]["to"], to_state)
                        self.assertEqual(
                            payload["transition"]["valid_next"],
                            BOX.VALID_TRANSITIONS.get(from_state, []),
                        )

    def test_transition_validator_rejects_unknown_states_structurally(self) -> None:
        with self.assertRaises(BOX.BoxStateTransitionError) as raised:
            BOX.validate_box_state_transition("ready", "missing")

        self.assertEqual(raised.exception.payload["error"]["type"], "invalid_state_transition")
        self.assertEqual(raised.exception.payload["transition"]["from"], "ready")
        self.assertEqual(raised.exception.payload["transition"]["to"], "missing")

    def test_update_box_can_opt_into_transition_validation(self) -> None:
        box = _box("ready")

        BOX.update_box(box, validate_transition=True, state="draining")
        self.assertEqual(box.state, "draining")

        with self.assertRaises(BOX.BoxStateTransitionError):
            BOX.update_box(box, validate_transition=True, state="creating")
        self.assertEqual(box.state, "draining")


class BoxUpResumeStateTests(unittest.TestCase):
    def test_every_resumable_up_state_uses_resume_path_without_prior_stage_reruns(self) -> None:
        expected_prior_stages = ["create", "storage", "bootstrap"]
        for state in sorted(BOX.RESUMABLE_UP_STATES):
            with self.subTest(state=state):
                box = _box(state, tailscale_ip="100.64.0.8")
                payloads: list[dict[str, object]] = []

                def fake_resolve(context: object) -> str:
                    context.ssh_target = "100.64.0.8"
                    return "100.64.0.8"

                with (
                    mock.patch.object(BOX, "load_profile", return_value=_profile()),
                    mock.patch.object(BOX, "load_inventory", return_value=[box]),
                    mock.patch.object(BOX, "load_deploy_manifest", return_value=_release()),
                    mock.patch.object(BOX, "_create_box_droplet") as create_droplet,
                    mock.patch.object(BOX, "_ensure_box_storage") as ensure_storage,
                    mock.patch.object(BOX, "_bootstrap_box_host") as bootstrap_host,
                    mock.patch.object(BOX, "_resolve_deploy_target", side_effect=fake_resolve),
                    mock.patch.object(BOX, "_deploy_box_runtime", return_value="deployed"),
                    mock.patch.object(BOX, "_patch_remote_runtime_contract", return_value={"env_updates": []}),
                    mock.patch.object(BOX, "_launch_remote_workspace", return_value={"targets": ["build", "up"]}),
                    mock.patch.object(
                        BOX,
                        "_run_box_first_box",
                        return_value={"client_id": "box-1", "active_profiles": ["core"]},
                    ),
                    mock.patch.object(BOX, "_verify_operator_swimmers_surface", return_value={"skipped": "no swimmers"}),
                    mock.patch.object(BOX, "save_inventory"),
                    mock.patch.object(BOX, "emit_json", side_effect=payloads.append),
                ):
                    result = BOX.cmd_up(
                        "box-1",
                        profile_name="dev-small",
                        blueprint=None,
                        set_args=[],
                        deploy_manifest="/tmp/deploy.json",
                        resume=True,
                        dry_run=False,
                        fmt="json",
                    )

                self.assertEqual(result, BOX.EXIT_OK)
                create_droplet.assert_not_called()
                ensure_storage.assert_not_called()
                bootstrap_host.assert_not_called()
                payload = payloads[-1]
                self.assertTrue(payload["resumed"])
                steps = payload["steps"]
                self.assertEqual([step["step"] for step in steps[:3]], expected_prior_stages)
                self.assertEqual([step["status"] for step in steps[:3]], ["skip", "skip", "skip"])
                first_executed = next(step for step in steps if step["status"] != "skip")
                self.assertEqual(first_executed["step"], "ssh-ready")

    def test_resume_rejects_every_non_resumable_up_state(self) -> None:
        rejected_states = set(BOX.STATES) - set(BOX.RESUMABLE_UP_STATES) - {"destroyed"}
        for state in sorted(rejected_states):
            with self.subTest(state=state):
                box = _box(state)
                payloads: list[dict[str, object]] = []
                with (
                    mock.patch.object(BOX, "load_profile", return_value=_profile()),
                    mock.patch.object(BOX, "load_inventory", return_value=[box]),
                    mock.patch.object(BOX, "emit_json", side_effect=payloads.append),
                ):
                    result = BOX.cmd_up(
                        "box-1",
                        profile_name="dev-small",
                        blueprint=None,
                        set_args=[],
                        deploy_manifest="/tmp/deploy.json",
                        resume=True,
                        dry_run=False,
                        fmt="json",
                    )

                self.assertEqual(result, BOX.EXIT_ERROR)
                self.assertEqual(payloads[-1]["error"]["type"], "invalid_state")


class BoxUpStageFailureStateTests(unittest.TestCase):
    def test_every_box_up_stage_failure_lands_on_declared_failure_state(self) -> None:
        stage_table = BOX._new_box_up_stages(_resume_context(), ssh_key_id="ssh-key", ts_authkey="ts-auth")
        self.assertTrue(stage_table)

        for stage in stage_table:
            with self.subTest(stage=stage.name, failure_state=stage.failure_state):
                context = _resume_context()

                def fail() -> None:
                    raise RuntimeError(f"{stage.name} failed")

                with (
                    mock.patch.object(BOX, "save_inventory") as save_inventory,
                    mock.patch.object(BOX, "_emit_box_up_failure", return_value=BOX.EXIT_ERROR) as emit_failure,
                ):
                    ok = BOX._run_box_up_stage(
                        context,
                        stage_name=stage.name,
                        error_type=stage.error_type,
                        action=fail,
                        failure_state=stage.failure_state,
                        next_actions=stage.next_actions,
                    )

                self.assertFalse(ok)
                self.assertEqual(context.steps[-1], {"step": stage.name, "status": "fail", "detail": f"{stage.name} failed"})
                emit_failure.assert_called_once()
                if stage.failure_state is None:
                    self.assertEqual(context.box.state, "ssh-ready")
                    save_inventory.assert_not_called()
                else:
                    self.assertEqual(context.box.state, stage.failure_state)
                    save_inventory.assert_called_once_with(context.boxes)


class BoxDownIntermediateStateTests(unittest.TestCase):
    def test_resumable_down_states_are_declared_teardown_intermediates(self) -> None:
        self.assertEqual(BOX.RESUMABLE_DOWN_STATES, {"destroy-pending", "volume-cleanup-failed"})
        self.assertLessEqual(BOX.RESUMABLE_DOWN_STATES, set(BOX.STATES))

    def test_destroy_pending_resume_confirms_absence_without_redelete(self) -> None:
        box = _box("destroy-pending")
        payloads: list[dict[str, object]] = []
        with (
            mock.patch.object(BOX, "load_inventory", return_value=[box]),
            mock.patch.object(BOX, "confirm_droplet_absent", return_value=True) as confirm_absent,
            mock.patch.object(BOX, "_destroy_box_droplet") as destroy_droplet,
            mock.patch.object(BOX, "save_inventory"),
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append),
        ):
            result = BOX.cmd_down("box-1", dry_run=False, fmt="json", confirmed=True)

        self.assertEqual(result, BOX.EXIT_OK)
        confirm_absent.assert_called_once_with("123")
        destroy_droplet.assert_not_called()
        self.assertEqual(box.state, "destroyed")
        self.assertEqual(
            [step["step"] for step in payloads[-1]["steps"]],
            ["drain", "remove", "firewall", "confirm", "volume"],
        )
        self.assertEqual([step["status"] for step in payloads[-1]["steps"][:3]], ["skip", "skip", "skip"])

    def test_destroy_pending_resume_stays_pending_when_still_listed(self) -> None:
        box = _box("destroy-pending")
        payloads: list[dict[str, object]] = []
        with (
            mock.patch.object(BOX, "load_inventory", return_value=[box]),
            mock.patch.object(BOX, "confirm_droplet_absent", return_value=False),
            mock.patch.object(BOX, "_destroy_box_droplet") as destroy_droplet,
            mock.patch.object(BOX, "save_inventory"),
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append),
        ):
            result = BOX.cmd_down("box-1", dry_run=False, fmt="json", confirmed=True)

        self.assertEqual(result, BOX.EXIT_ERROR)
        destroy_droplet.assert_not_called()
        self.assertEqual(box.state, "destroy-pending")
        self.assertEqual(payloads[-1]["error"]["type"], "destroy_pending")
        self.assertEqual(payloads[-1]["steps"][-1]["step"], "confirm")

    def test_volume_cleanup_failed_resume_skips_destroy_and_converges(self) -> None:
        box = _box("volume-cleanup-failed")
        payloads: list[dict[str, object]] = []
        with (
            mock.patch.object(BOX, "load_inventory", return_value=[box]),
            mock.patch.object(BOX, "_destroy_box_droplet") as destroy_droplet,
            mock.patch.object(BOX, "confirm_droplet_absent") as confirm_absent,
            mock.patch.object(BOX, "save_inventory"),
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append),
        ):
            result = BOX.cmd_down("box-1", dry_run=False, fmt="json", confirmed=True)

        self.assertEqual(result, BOX.EXIT_OK)
        destroy_droplet.assert_not_called()
        confirm_absent.assert_not_called()
        self.assertEqual(box.state, "destroyed")
        self.assertEqual([step["step"] for step in payloads[-1]["steps"]], ["destroy", "volume"])
        self.assertEqual([step["status"] for step in payloads[-1]["steps"]], ["skip", "skip"])


if __name__ == "__main__":
    unittest.main()
