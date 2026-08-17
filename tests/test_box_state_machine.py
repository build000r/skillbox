from __future__ import annotations

import contextlib
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


def _provider_observation(outcome: object, droplet_id: str = "123") -> object:
    return BOX.ProviderObservation(
        outcome=BOX.ProviderOutcome(outcome),
        operation="digitalocean.droplet.get",
        resource_id=droplet_id,
        resource={"id": droplet_id} if outcome == BOX.ProviderOutcome.FOUND else None,
    )


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


def _box(state: str, *, tailscale_ip: str | None = "100.100.0.8") -> object:
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


def _advance_to_deploying(box: object):
    """Stub lockdown: do what the real stage does to state, nothing else."""

    def _stub(_context: object) -> str:
        box.state = "deploying"
        return "stubbed lockdown"

    return _stub


class BoxUpResumeStateTests(unittest.TestCase):
    def test_every_resumable_up_state_uses_resume_path_without_prior_stage_reruns(self) -> None:
        expected_prior_stages = ["create", "storage", "bootstrap"]
        for state in sorted(BOX.RESUMABLE_UP_STATES):
            with self.subTest(state=state):
                box = _box(state, tailscale_ip="100.100.0.8")
                payloads: list[dict[str, object]] = []

                def fake_resolve(context: object) -> str:
                    context.ssh_target = "100.100.0.8"
                    return "100.100.0.8"

                with (
                    mock.patch.object(BOX, "load_profile", return_value=_profile()),
                    mock.patch.object(BOX, "load_inventory", return_value=[box]),
                    mock.patch.object(BOX, "load_deploy_manifest", return_value=_release()),
                    mock.patch.object(BOX, "_create_box_droplet") as create_droplet,
                    mock.patch.object(BOX, "_ensure_box_storage") as ensure_storage,
                    mock.patch.object(BOX, "_bootstrap_box_host") as bootstrap_host,
                    mock.patch.object(BOX, "_resolve_deploy_target", side_effect=fake_resolve),
                    # Lockdown is proved for real in CloudFirewallFailClosedTests;
                    # here it is stubbed so the assertion stays on resume dispatch.
                    mock.patch.object(BOX, "_lock_down_box_network", side_effect=_advance_to_deploying(box)),
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


class CloudFirewallFailClosedTests(unittest.TestCase):
    """tailnet_only lockdown must fail closed: a doctl/API failure, a missing
    cloud_firewall_id, or an unverified lockdown may never advance the box past
    the enroll stage while port 22 could still be world-reachable."""

    LOCKED_DOWN_FIREWALL = {
        "id": "fw-1",
        "inbound_rules": [
            {"protocol": "udp", "ports": "41641", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
        ],
    }
    SSH_OPEN_FIREWALL = {
        "id": "fw-1",
        "inbound_rules": [
            {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
            {"protocol": "udp", "ports": "41641", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
        ],
    }

    def _lockdown_context(self, *, cloud_firewall_id: str | None) -> object:
        # Lockdown is now its own stage, entered only after enrollment has
        # produced a validated Tailnet identity — so the context starts there.
        context = _resume_context("lockdown")
        context.box.tailscale_ip = "100.100.0.9"
        context.box.cloud_firewall_id = cloud_firewall_id
        return context

    def _run_lockdown_stage(self, context: object) -> bool:
        stage = BOX._box_lockdown_stage(context)
        with mock.patch.object(BOX, "_emit_box_up_failure", return_value=BOX.EXIT_ERROR):
            return BOX._run_box_up_stage(
                context,
                stage_name=stage.name,
                error_type=stage.error_type,
                action=stage.action,
                failure_state=stage.failure_state,
                next_actions=stage.next_actions,
            )

    @contextlib.contextmanager
    def _lockdown_mocks(self):
        with (
            mock.patch.object(BOX, "ssh_cmd", return_value=_completed(0, stdout="Status: active")),
            mock.patch.object(BOX, "save_inventory"),
        ):
            yield

    def test_lockdown_doctl_failure_fails_stage_and_state_does_not_advance(self) -> None:
        context = self._lockdown_context(cloud_firewall_id="fw-1")
        with (
            self._lockdown_mocks(),
            mock.patch.object(BOX, "do_update_firewall_lockdown", side_effect=RuntimeError("doctl 500")),
            mock.patch.object(BOX, "do_get_firewall", return_value=self.SSH_OPEN_FIREWALL),
        ):
            ok = self._run_lockdown_stage(context)

        self.assertFalse(ok)
        self.assertEqual(context.box.state, "lockdown")
        failure = context.steps[-1]
        self.assertEqual(failure["step"], "lockdown")
        self.assertEqual(failure["status"], "fail")
        self.assertIn("refusing to advance", failure["detail"])
        lockdown_steps = [step for step in context.steps if step.get("stage") == "cloud_firewall_lockdown"]
        self.assertEqual(
            lockdown_steps,
            [{
                "stage": "cloud_firewall_lockdown",
                "error": "doctl 500",
                "posture": "tailnet_only",
                "outcome": BOX.LockdownOutcome.UPDATE_FAILED.value,
            }],
        )

    def test_missing_cloud_firewall_id_is_fatal_under_tailnet_only(self) -> None:
        context = self._lockdown_context(cloud_firewall_id=None)
        with (
            self._lockdown_mocks(),
            mock.patch.object(BOX, "do_update_firewall_lockdown") as update_lockdown,
        ):
            ok = self._run_lockdown_stage(context)

        self.assertFalse(ok)
        update_lockdown.assert_not_called()
        self.assertEqual(context.box.state, "lockdown")
        self.assertIn("No cloud firewall", context.steps[-1]["detail"])

    def test_lockdown_reread_still_open_ssh_fails_stage(self) -> None:
        context = self._lockdown_context(cloud_firewall_id="fw-1")
        with (
            self._lockdown_mocks(),
            mock.patch.object(BOX, "do_update_firewall_lockdown", return_value=self.LOCKED_DOWN_FIREWALL),
            mock.patch.object(BOX, "do_get_firewall", return_value=self.SSH_OPEN_FIREWALL),
        ):
            ok = self._run_lockdown_stage(context)

        self.assertFalse(ok)
        self.assertEqual(context.box.state, "lockdown")
        self.assertIn("still allows inbound SSH", context.steps[-1]["detail"])

    def test_lockdown_reread_failure_fails_stage(self) -> None:
        context = self._lockdown_context(cloud_firewall_id="fw-1")
        with (
            self._lockdown_mocks(),
            mock.patch.object(BOX, "do_update_firewall_lockdown", return_value=self.LOCKED_DOWN_FIREWALL),
            mock.patch.object(BOX, "do_get_firewall", return_value=None),
        ):
            ok = self._run_lockdown_stage(context)

        self.assertFalse(ok)
        self.assertEqual(context.box.state, "lockdown")
        self.assertIn("could not be re-read", context.steps[-1]["detail"])

    def test_lockdown_reread_for_a_different_firewall_id_fails_stage(self) -> None:
        """A read that describes some other firewall is not evidence about this box."""
        context = self._lockdown_context(cloud_firewall_id="fw-1")
        other = dict(self.LOCKED_DOWN_FIREWALL, id="fw-999")
        with (
            self._lockdown_mocks(),
            mock.patch.object(BOX, "do_update_firewall_lockdown", return_value=self.LOCKED_DOWN_FIREWALL),
            mock.patch.object(BOX, "do_get_firewall", side_effect=[self.SSH_OPEN_FIREWALL, other]),
        ):
            ok = self._run_lockdown_stage(context)

        self.assertFalse(ok)
        self.assertEqual(context.box.state, "lockdown")
        self.assertIn("does not describe this box", context.steps[-1]["detail"])

    def test_lockdown_success_verifies_reread_and_advances(self) -> None:
        context = self._lockdown_context(cloud_firewall_id="fw-1")
        with (
            self._lockdown_mocks(),
            mock.patch.object(BOX, "do_update_firewall_lockdown", return_value=self.LOCKED_DOWN_FIREWALL),
            # Pre-read sees the bootstrap rules (SSH open), post-read proves closed.
            mock.patch.object(
                BOX,
                "do_get_firewall",
                side_effect=[self.SSH_OPEN_FIREWALL, self.LOCKED_DOWN_FIREWALL],
            ) as get_firewall,
        ):
            ok = self._run_lockdown_stage(context)

        self.assertTrue(ok)
        self.assertEqual(get_firewall.call_args_list, [mock.call("fw-1"), mock.call("fw-1")])
        self.assertEqual(context.box.state, "deploying")
        lockdown_steps = [step for step in context.steps if step.get("stage") == "cloud_firewall_lockdown"]
        self.assertEqual(
            lockdown_steps,
            [{
                "stage": "cloud_firewall_lockdown",
                "firewall_id": "fw-1",
                "posture": "tailnet_only",
                "outcome": BOX.LockdownOutcome.LOCKED_DOWN.value,
                "verified": True,
            }],
        )

    def test_rerunning_lockdown_reproves_by_read_without_remutating(self) -> None:
        """A resume re-proves lockdown; it does not blind-retry the mutation."""
        context = self._lockdown_context(cloud_firewall_id="fw-1")
        with (
            self._lockdown_mocks(),
            mock.patch.object(BOX, "do_update_firewall_lockdown") as update_lockdown,
            mock.patch.object(BOX, "do_get_firewall", return_value=self.LOCKED_DOWN_FIREWALL),
        ):
            ok = self._run_lockdown_stage(context)

        self.assertTrue(ok)
        update_lockdown.assert_not_called()
        self.assertEqual(context.box.state, "deploying")
        lockdown_steps = [step for step in context.steps if step.get("stage") == "cloud_firewall_lockdown"]
        self.assertEqual(lockdown_steps[0]["outcome"], BOX.LockdownOutcome.ALREADY_LOCKED_DOWN.value)

    def test_unreachable_tailnet_refuses_before_touching_the_firewall(self) -> None:
        """Public SSH stays open when the Tailnet path is unproven — the recoverable side."""
        context = self._lockdown_context(cloud_firewall_id="fw-1")
        with (
            mock.patch.object(BOX, "ssh_cmd", return_value=_completed(255, stdout="")),
            mock.patch.object(BOX, "save_inventory"),
            mock.patch.object(BOX, "do_update_firewall_lockdown") as update_lockdown,
            mock.patch.object(BOX, "do_get_firewall") as get_firewall,
        ):
            ok = self._run_lockdown_stage(context)

        self.assertFalse(ok)
        update_lockdown.assert_not_called()
        get_firewall.assert_not_called()
        self.assertEqual(context.box.state, "lockdown")
        self.assertIn("refusing to close public SSH", context.steps[-1]["detail"])
        reachability = [step for step in context.steps if step.get("stage") == "tailnet_reachability"]
        self.assertEqual(reachability, [{
            "stage": "tailnet_reachability",
            "tailscale_ip": "100.100.0.9",
            "reachable": False,
        }])

    def test_lockdown_without_a_valid_tailnet_identity_refuses(self) -> None:
        context = self._lockdown_context(cloud_firewall_id="fw-1")
        context.box.tailscale_ip = "10.0.0.5"
        with (
            self._lockdown_mocks(),
            mock.patch.object(BOX, "do_update_firewall_lockdown") as update_lockdown,
        ):
            ok = self._run_lockdown_stage(context)

        self.assertFalse(ok)
        update_lockdown.assert_not_called()
        self.assertEqual(context.box.state, "lockdown")
        self.assertIn("requires a proven Tailnet identity", context.steps[-1]["detail"])

    def test_create_droplet_fails_closed_when_firewall_create_fails(self) -> None:
        context = self._lockdown_context(cloud_firewall_id=None)
        context.boxes = []
        saved: list[object] = []
        with (
            mock.patch.object(BOX, "do_create_droplet", return_value={"id": 123}),
            mock.patch.object(BOX, "do_droplet_public_ip", return_value="1.2.3.4"),
            mock.patch.object(BOX, "do_create_firewall", side_effect=RuntimeError("doctl timeout")),
            mock.patch.object(BOX, "save_inventory", side_effect=saved.append),
        ):
            with self.assertRaises(RuntimeError) as raised:
                BOX._create_box_droplet(context, ssh_key_id="ssh-key")

        self.assertIn("Cloud firewall bootstrap failed", str(raised.exception))
        self.assertIn("doctl timeout", str(raised.exception))
        # The droplet is still recorded in inventory so teardown can find it.
        self.assertEqual(saved, [[context.box]])
        self.assertEqual(context.box.droplet_id, "123")
        self.assertIsNone(context.box.cloud_firewall_id)

    def test_firewall_allows_public_ssh_covers_port_ranges_and_all(self) -> None:
        any_sources = {"addresses": ["0.0.0.0/0"]}
        cases = [
            ({"inbound_rules": [{"protocol": "tcp", "ports": "22", "sources": any_sources}]}, True),
            ({"inbound_rules": [{"protocol": "tcp", "ports": "all", "sources": any_sources}]}, True),
            ({"inbound_rules": [{"protocol": "tcp", "ports": "0", "sources": any_sources}]}, True),
            ({"inbound_rules": [{"protocol": "tcp", "ports": "20-25", "sources": any_sources}]}, True),
            ({"inbound_rules": [{"protocol": "tcp", "ports": "garbage", "sources": any_sources}]}, True),
            ({"inbound_rules": [{"protocol": "tcp", "ports": "80", "sources": any_sources}]}, False),
            ({"inbound_rules": [{"protocol": "udp", "ports": "41641", "sources": any_sources}]}, False),
            ({"inbound_rules": [{"protocol": "tcp", "ports": "22", "sources": {"addresses": ["100.64.0.0/10"]}}]}, False),
            ({"inbound_rules": []}, False),
        ]
        for firewall, expected in cases:
            with self.subTest(firewall=firewall):
                self.assertEqual(BOX.firewall_allows_public_ssh(firewall), expected)


class BoxDownIntermediateStateTests(unittest.TestCase):
    def test_resumable_down_states_are_declared_teardown_intermediates(self) -> None:
        self.assertEqual(BOX.RESUMABLE_DOWN_STATES, {"destroy-pending", "volume-cleanup-failed"})
        self.assertLessEqual(BOX.RESUMABLE_DOWN_STATES, set(BOX.STATES))

    def test_destroy_pending_resume_confirms_absence_without_redelete(self) -> None:
        box = _box("destroy-pending")
        payloads: list[dict[str, object]] = []
        with (
            mock.patch.object(BOX, "load_inventory", return_value=[box]),
            mock.patch.object(
                BOX,
                "confirm_droplet_absent",
                return_value=_provider_observation(BOX.ProviderOutcome.CONFIRMED_NOT_FOUND),
            ) as confirm_absent,
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
            mock.patch.object(
                BOX,
                "confirm_droplet_absent",
                return_value=_provider_observation(BOX.ProviderOutcome.FOUND),
            ),
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


class EnrollmentEvidenceTests(unittest.TestCase):
    """Enrollment may only hand off to lockdown on evidence it produced itself."""

    def _enroll(self, context: object) -> str:
        return BOX._enroll_box_tailscale(context, ts_authkey="ts-auth")

    def test_enrollment_stops_at_lockdown_and_touches_no_firewall(self) -> None:
        context = _resume_context("ssh-ready")
        context.ip = "1.2.3.4"
        with (
            mock.patch.object(BOX, "ssh_script", return_value=_completed(0, stdout="TAILSCALE_IPV4=100.100.0.9\n")),
            mock.patch.object(BOX, "save_inventory"),
            mock.patch.object(BOX, "do_update_firewall_lockdown") as update_lockdown,
            mock.patch.object(BOX, "do_get_firewall") as get_firewall,
        ):
            detail = self._enroll(context)

        self.assertIn("100.100.0.9", detail)
        self.assertEqual(context.box.state, "lockdown")
        self.assertEqual(context.box.tailscale_ip, "100.100.0.9")
        update_lockdown.assert_not_called()
        get_firewall.assert_not_called()

    def test_enrollment_without_the_ipv4_marker_cannot_advance(self) -> None:
        context = _resume_context("ssh-ready")
        context.ip = "1.2.3.4"
        with (
            mock.patch.object(BOX, "ssh_script", return_value=_completed(0, stdout="all good\n")),
            mock.patch.object(BOX, "save_inventory"),
            mock.patch.object(BOX, "ssh_cmd") as ssh_cmd,
        ):
            with self.assertRaises(BOX.BoxLockdownError) as raised:
                self._enroll(context)

        # No `tailscale ip -4` second opinion: the marker is the only evidence.
        ssh_cmd.assert_not_called()
        self.assertEqual(raised.exception.outcome, BOX.LockdownOutcome.TAILNET_IDENTITY_MISSING)
        self.assertEqual(raised.exception.error_type, "tailnet_identity_missing")
        # Still mid-enrollment; the stage runner is what applies a failure state.
        self.assertEqual(context.box.state, "enrolling")
        self.assertNotIn(context.box.state, BOX.ENROLLMENT_PROVEN_STATES)
        # The stale IP is cleared, so a later resume cannot read it as proof.
        self.assertIsNone(context.box.tailscale_ip)

    def test_enrollment_with_a_non_tailnet_address_cannot_advance(self) -> None:
        context = _resume_context("ssh-ready")
        context.ip = "1.2.3.4"
        with (
            mock.patch.object(BOX, "ssh_script", return_value=_completed(0, stdout="TAILSCALE_IPV4=10.0.0.5\n")),
            mock.patch.object(BOX, "save_inventory"),
        ):
            with self.assertRaises(BOX.BoxLockdownError) as raised:
                self._enroll(context)

        self.assertEqual(raised.exception.outcome, BOX.LockdownOutcome.TAILNET_IDENTITY_MISSING)
        self.assertNotIn(context.box.state, BOX.ENROLLMENT_PROVEN_STATES)
        self.assertIsNone(context.box.tailscale_ip)

    def test_nonzero_enrollment_cannot_advance(self) -> None:
        context = _resume_context("ssh-ready")
        context.ip = "1.2.3.4"
        with (
            mock.patch.object(BOX, "ssh_script", return_value=_completed(7, stderr="tailscale up refused")),
            mock.patch.object(BOX, "save_inventory"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                self._enroll(context)

        self.assertIn("exit 7", str(raised.exception))
        self.assertEqual(context.box.state, "enrolling")


class LockdownRecoveryTests(unittest.TestCase):
    """End-to-end `box up --resume` behaviour, driven by durable state."""

    def _run_resume(self, box: object, **overrides: object) -> tuple[int, list[dict[str, object]], dict[str, mock.Mock]]:
        payloads: list[dict[str, object]] = []
        spies: dict[str, mock.Mock] = {}

        def fake_resolve(context: object) -> str:
            context.ssh_target = "100.100.0.8"
            return "100.100.0.8"

        lockdown = overrides.get("lockdown") or _advance_to_deploying(box)
        with (
            mock.patch.object(BOX, "load_profile", return_value=_profile()),
            mock.patch.object(BOX, "load_inventory", return_value=[box]),
            mock.patch.object(BOX, "load_deploy_manifest", return_value=_release()),
            mock.patch.object(BOX, "_create_box_droplet") as create_droplet,
            mock.patch.object(BOX, "_resolve_deploy_target", side_effect=fake_resolve),
            mock.patch.object(BOX, "_enroll_box_tailscale") as enroll,
            mock.patch.object(BOX, "_lock_down_box_network", side_effect=lockdown) as lock_down,
            mock.patch.object(BOX, "_deploy_box_runtime", return_value="deployed") as deploy,
            mock.patch.object(BOX, "_patch_remote_runtime_contract", return_value={"env_updates": []}),
            mock.patch.object(BOX, "_launch_remote_workspace", return_value={"targets": ["build", "up"]}),
            mock.patch.object(BOX, "_run_box_first_box", return_value={"client_id": "box-1", "active_profiles": []}),
            mock.patch.object(BOX, "_verify_operator_swimmers_surface", return_value={"skipped": "none"}),
            mock.patch.object(BOX, "save_inventory"),
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append),
        ):
            spies = {"create": create_droplet, "enroll": enroll, "lockdown": lock_down, "deploy": deploy}
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
        return result, payloads, spies

    @staticmethod
    def _refuse_lockdown(_context: object) -> str:
        raise BOX.BoxLockdownError(
            BOX.LockdownOutcome.PUBLIC_SSH_OPEN,
            "Cloud firewall fw-1 still allows inbound SSH after lockdown",
        )

    def test_legacy_ssh_ready_with_a_tailnet_ip_still_has_to_pass_lockdown(self) -> None:
        """The historical bypass: ssh-ready + a stored IP used to jump to deploy."""
        box = _box("ssh-ready", tailscale_ip="100.100.0.8")
        result, payloads, spies = self._run_resume(box, lockdown=self._refuse_lockdown)

        self.assertEqual(result, BOX.EXIT_ERROR)
        spies["deploy"].assert_not_called()
        spies["lockdown"].assert_called_once()
        self.assertEqual(box.state, "lockdown")
        self.assertEqual(payloads[-1]["error"]["type"], "cloud_firewall_public_ssh_open")
        steps = {step["step"]: step for step in payloads[-1]["steps"]}
        self.assertEqual(steps["enroll"]["status"], "ok")
        self.assertEqual(steps["lockdown"]["status"], "fail")

    def test_failed_lockdown_resume_reruns_lockdown_and_never_deploys(self) -> None:
        box = _box("lockdown", tailscale_ip="100.100.0.8")
        result, payloads, spies = self._run_resume(box, lockdown=self._refuse_lockdown)

        self.assertEqual(result, BOX.EXIT_ERROR)
        spies["enroll"].assert_not_called()
        spies["lockdown"].assert_called_once()
        spies["deploy"].assert_not_called()
        # Still `lockdown`, never reset to the state that used to skip the proof.
        self.assertEqual(box.state, "lockdown")
        self.assertNotEqual(box.state, "ssh-ready")
        self.assertEqual(payloads[-1]["error"]["type"], "cloud_firewall_public_ssh_open")

    def test_successful_lockdown_resume_advances_without_recreate_or_reenroll(self) -> None:
        box = _box("lockdown", tailscale_ip="100.100.0.8")
        result, payloads, spies = self._run_resume(box)

        self.assertEqual(result, BOX.EXIT_OK)
        spies["create"].assert_not_called()
        spies["enroll"].assert_not_called()
        spies["lockdown"].assert_called_once()
        spies["deploy"].assert_called_once()
        steps = {step["step"]: step for step in payloads[-1]["steps"]}
        self.assertEqual(steps["enroll"]["status"], "skip")
        self.assertEqual(steps["lockdown"]["status"], "ok")

    def test_resume_past_lockdown_skips_the_completed_mutation(self) -> None:
        box = _box("deploying", tailscale_ip="100.100.0.8")
        result, payloads, spies = self._run_resume(box)

        self.assertEqual(result, BOX.EXIT_OK)
        spies["enroll"].assert_not_called()
        spies["lockdown"].assert_not_called()
        steps = {step["step"]: step for step in payloads[-1]["steps"]}
        self.assertEqual(steps["lockdown"]["status"], "skip")
        self.assertIn("already proven", steps["lockdown"]["detail"])

    def test_ssh_ready_without_a_valid_tailnet_ip_reenrolls(self) -> None:
        box = _box("ssh-ready", tailscale_ip=None)
        with mock.patch.object(BOX, "require_env", return_value="ts-auth"):
            result, _payloads, spies = self._run_resume(box)

        self.assertEqual(result, BOX.EXIT_OK)
        spies["enroll"].assert_called_once()
        spies["lockdown"].assert_called_once()


class LockdownStageOrderTests(unittest.TestCase):
    """Preview and real run must agree on the stage order they advertise."""

    EXPECTED_TAIL = ["ssh-ready", "enroll", "lockdown", "deploy"]

    def _preview_steps(self, *, resume: bool, state: str) -> list[str]:
        payloads: list[dict[str, object]] = []
        boxes = [_box(state)] if resume else []
        with (
            mock.patch.object(BOX, "load_profile", return_value=_profile()),
            mock.patch.object(BOX, "load_inventory", return_value=boxes),
            mock.patch.object(BOX, "load_deploy_manifest", return_value=_release()),
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append),
        ):
            result = BOX.cmd_up(
                "box-1",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest="/tmp/deploy.json",
                resume=resume,
                dry_run=True,
                fmt="json",
            )
        self.assertEqual(result, BOX.EXIT_OK)
        return [step["step"] for step in payloads[-1]["steps"]]

    def test_new_box_dry_run_lists_enroll_lockdown_deploy_in_order(self) -> None:
        steps = self._preview_steps(resume=False, state="ssh-ready")
        self.assertEqual(steps[3:7], self.EXPECTED_TAIL)

    def test_resumed_dry_run_lists_enroll_lockdown_deploy_in_order(self) -> None:
        steps = self._preview_steps(resume=True, state="lockdown")
        self.assertEqual(steps[3:7], self.EXPECTED_TAIL)

    def test_real_new_box_stage_order_matches_the_preview(self) -> None:
        context = _resume_context("ssh-ready")
        stages = BOX._new_box_up_stages(context, ssh_key_id="ssh-key", ts_authkey="ts-auth")
        self.assertEqual([stage.name for stage in stages][3:7], self.EXPECTED_TAIL)

    def test_lockdown_failure_state_is_never_a_bypass_state(self) -> None:
        context = _resume_context("lockdown")
        stage = BOX._box_lockdown_stage(context)
        self.assertEqual(stage.failure_state, "lockdown")
        self.assertNotIn(stage.failure_state, BOX.LOCKDOWN_PROVEN_STATES)

    def test_every_failing_lockdown_outcome_has_one_stable_error_type(self) -> None:
        failing = set(BOX.LockdownOutcome) - set(BOX.LOCKDOWN_PROVEN_OUTCOMES)
        self.assertEqual(failing, set(BOX.LOCKDOWN_ERROR_TYPES))
        error_types = list(BOX.LOCKDOWN_ERROR_TYPES.values())
        self.assertEqual(len(error_types), len(set(error_types)))
        for outcome in failing:
            with self.subTest(outcome=outcome.value):
                self.assertEqual(
                    BOX.BoxLockdownError(outcome, "x").error_type,
                    BOX.LOCKDOWN_ERROR_TYPES[outcome],
                )


class TeardownRecoveryHintTests(unittest.TestCase):
    """Recovery hints must be runnable, not merely plausible.

    Teardown became identity-bound, which silently invalidated every hint still
    printing a bare `box down <id>`: the operator (or agent) pastes it and gets
    `confirmation_required`. A hint that offers teardown as one option among
    several is different and stays bare on purpose — see the last test.
    """

    def _hint_lines(self, payload: dict) -> list[str]:
        lines = list(payload.get("next_actions") or [])
        error = payload.get("error")
        if isinstance(error, dict):
            lines += list(error.get("next_actions") or [])
        # Hints appear both bare (`box down x`) and fully qualified
        # (`python3 scripts/box.py down x --format json`); match either.
        return [line for line in lines if " down " in f" {line} " or line.startswith("box down ")]

    def assert_runnable(self, payload: dict, box_id: str) -> None:
        hints = self._hint_lines(payload)
        self.assertTrue(hints, f"expected a teardown hint in {payload.get('next_actions')}")
        for hint in hints:
            with self.subTest(hint=hint):
                self.assertTrue(
                    f"--confirm {box_id}" in hint or "--dry-run" in hint,
                    f"{hint!r} would refuse with confirmation_required",
                )

    def _emit(self, fn, *args, **kwargs) -> dict:
        payloads: list[dict] = []
        with (
            mock.patch.object(BOX, "save_inventory"),
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append),
        ):
            fn(*args, **kwargs)
        return payloads[-1]

    def test_dry_run_preview_hands_back_the_real_command(self) -> None:
        box = _box("ready")
        payload = self._emit(BOX._emit_box_down_dry_run, box, "box-1", [], is_json=True)
        self.assert_runnable(payload, "box-1")

    def test_destroy_pending_retry_is_identity_bound(self) -> None:
        box = _box("draining")
        payload = self._emit(
            BOX._emit_box_down_destroy_pending, [box], box, "box-1", [], is_json=True
        )
        self.assert_runnable(payload, "box-1")

    def test_destroy_failure_retry_is_identity_bound(self) -> None:
        box = _box("draining")
        payload = self._emit(BOX._emit_box_down_destroy_failure, box, "box-1", [], is_json=True)
        self.assert_runnable(payload, "box-1")

    def test_volume_cleanup_failure_retry_is_identity_bound(self) -> None:
        box = _box("draining")
        payload = self._emit(
            BOX._emit_box_down_volume_failure, [box], box, "box-1", [], is_json=True
        )
        self.assert_runnable(payload, "box-1")

    def test_box_list_teardown_hints_are_identity_bound(self) -> None:
        for state in sorted(BOX.RESUMABLE_DOWN_STATES):
            with self.subTest(state=state):
                hint = BOX._teardown_pending_hint(_box(state))
                self.assertIsNotNone(hint)
                self.assertEqual(hint["next_action"], "box down box-1 --confirm box-1")

    def test_suggesting_teardown_as_an_option_stays_unconfirmed(self) -> None:
        """`box up` failure hints point at teardown; they do not pre-authorize it.

        Handing an agent a paste-ready destructive command as a routine failure
        hint is worse than making it opt in, so these deliberately stay bare and
        the confirmation gate does its job.
        """
        context = _resume_context("ssh-ready")
        stages = BOX._new_box_up_stages(context, ssh_key_id="k", ts_authkey="t")
        suggestions = [
            action
            for stage in stages
            for action in (stage.next_actions or [])
            if action.startswith("box down ")
        ]
        self.assertTrue(suggestions)
        for action in suggestions:
            with self.subTest(action=action):
                self.assertNotIn("--confirm", action)


if __name__ == "__main__":
    unittest.main()
