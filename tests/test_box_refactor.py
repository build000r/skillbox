from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"
BOX = SourceFileLoader(
    "skillbox_box_refactor",
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
        min_free_gb=20,
    )


def _release(client_id: str = "alpha") -> object:
    return BOX.DeployRelease(
        manifest_path=Path("/tmp/deploy.json"),
        client_id=client_id,
        source_commit="1234567890ab1234567890ab1234567890ab1234",
        payload_tree_sha256="a" * 64,
        archive_path=Path("/tmp/skillbox.tar.gz"),
        archive_sha256="b" * 64,
        active_profiles=["core", "ops"],
    )


class BoxRefactorTests(unittest.TestCase):
    def test_load_profile_reads_yaml_variants_and_validates_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profiles_dir = Path(tmpdir)
            (profiles_dir / "custom").write_text(
                "provider: local\n"
                "region: sfo3\n"
                "size: s-4vcpu-8gb\n"
                "storage:\n"
                "  provider: local\n"
                "  mount_path: /srv/skillbox\n"
                "  filesystem: ext4\n"
                "  required: true\n"
                "  min_free_gb: 20\n",
                encoding="utf-8",
            )
            (profiles_dir / "broken.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
            (profiles_dir / "known.yaml").write_text("region: nyc3\n", encoding="utf-8")

            with mock.patch.object(BOX, "PROFILES_DIR", profiles_dir):
                profile = BOX.load_profile("custom")
                self.assertEqual(profile.provider, "local")
                self.assertEqual(profile.region, "sfo3")
                self.assertEqual(profile.storage.mount_path, "/srv/skillbox")

                with self.assertRaisesRegex(RuntimeError, "Expected a YAML mapping"):
                    BOX.load_profile("broken")

                with self.assertRaisesRegex(RuntimeError, r"Available: .*known"):
                    BOX.load_profile("missing")

    def test_load_dotenv_only_sets_missing_variables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "# comment\nFOO=from-file\nBAR=from-file\nBAZ=from-file\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"FOO": "existing"}, clear=True):
                BOX.load_dotenv(env_path)
                self.assertEqual(os.environ["FOO"], "existing")
                self.assertEqual(os.environ["BAR"], "from-file")
                self.assertEqual(os.environ["BAZ"], "from-file")

            env_path.write_text("INVALID_LINE\nQUX=loaded\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                BOX.load_dotenv(env_path)
                self.assertEqual(os.environ["QUX"], "loaded")

    def test_cmd_up_success_reports_ready_payload(self) -> None:
        profile = BOX.BoxProfile(id="dev-small", storage=_storage())
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_profile", return_value=profile), \
            mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "load_deploy_manifest", return_value=_release()), \
            mock.patch.object(BOX, "require_env", side_effect=["do-token", "ssh-key", "ts-auth"]), \
            mock.patch.object(BOX, "do_create_droplet", return_value={"id": 42}), \
            mock.patch.object(BOX, "do_droplet_public_ip", return_value="1.2.3.4"), \
            mock.patch.object(BOX, "do_find_volume_by_name", return_value=None), \
            mock.patch.object(BOX, "do_create_volume", return_value={"id": "vol-1", "size_gigabytes": 20}), \
            mock.patch.object(BOX, "do_attach_volume"), \
            mock.patch.object(BOX, "do_get_volume", return_value={"id": "vol-1", "size_gigabytes": 20, "droplet_ids": [42]}), \
            mock.patch.object(BOX, "wait_for_ssh", side_effect=[True, True, True]), \
            mock.patch.object(
                BOX,
                "ssh_script",
                side_effect=[_completed(), _completed(), _completed()],
            ), \
            mock.patch.object(BOX, "scp_file", return_value=_completed()), \
            mock.patch.object(
                BOX,
                "ssh_cmd",
                side_effect=[
                    _completed(stdout="100.64.0.1\n"),
                    _completed(stdout='{"client_id":"alpha","active_profiles":["core","ops"],"created_client":true}\n'),
                ],
            ), \
            mock.patch.object(BOX, "save_inventory"), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_up(
                "alpha",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest="/tmp/deploy.json",
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX.EXIT_OK)
        payload = payloads[-1]
        self.assertEqual(payload["box_id"], "alpha")
        self.assertEqual(payload["tailscale_ip"], "100.64.0.1")
        self.assertEqual(payload["storage"]["mount_path"], "/srv/skillbox")
        self.assertEqual(payload["volume"]["name"], "skillbox-state-alpha")
        self.assertEqual([step["status"] for step in payload["steps"]], ["ok", "ok", "ok", "ok", "ok", "ok", "ok"])

    def test_cmd_up_returns_structured_error_when_deploy_fails(self) -> None:
        profile = BOX.BoxProfile(id="dev-small", storage=_storage())
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_profile", return_value=profile), \
            mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "load_deploy_manifest", return_value=_release()), \
            mock.patch.object(BOX, "require_env", side_effect=["do-token", "ssh-key", "ts-auth"]), \
            mock.patch.object(BOX, "do_create_droplet", return_value={"id": 42}), \
            mock.patch.object(BOX, "do_droplet_public_ip", return_value="1.2.3.4"), \
            mock.patch.object(BOX, "do_find_volume_by_name", return_value=None), \
            mock.patch.object(BOX, "do_create_volume", return_value={"id": "vol-1", "size_gigabytes": 20}), \
            mock.patch.object(BOX, "do_attach_volume"), \
            mock.patch.object(BOX, "do_get_volume", return_value={"id": "vol-1", "size_gigabytes": 20, "droplet_ids": [42]}), \
            mock.patch.object(BOX, "wait_for_ssh", side_effect=[True, True, True]), \
            mock.patch.object(
                BOX,
                "ssh_script",
                side_effect=[_completed(), _completed(), _completed(returncode=1, stderr="deploy failed")],
            ), \
            mock.patch.object(BOX, "scp_file", return_value=_completed()), \
            mock.patch.object(BOX, "ssh_cmd", side_effect=[_completed(stdout="100.64.0.1\n")]), \
            mock.patch.object(BOX, "save_inventory"), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_up(
                "alpha",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest="/tmp/deploy.json",
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX.EXIT_ERROR)
        payload = payloads[-1]
        self.assertEqual(payload["error"]["type"], "deploy_failed")
        self.assertEqual(payload["steps"][-1]["step"], "deploy")
        self.assertEqual(payload["steps"][-1]["status"], "fail")

    def test_cmd_up_dry_run_reports_skip_steps(self) -> None:
        profile = BOX.BoxProfile(id="dev-small")
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_profile", return_value=profile), \
            mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_up(
                "dry-box",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest=None,
                dry_run=True,
                fmt="json",
            )

        self.assertEqual(result, BOX.EXIT_OK)
        self.assertTrue(payloads[-1]["dry_run"])
        self.assertEqual(
            [step["status"] for step in payloads[-1]["steps"]],
            ["skip", "skip", "skip", "skip", "skip", "skip"],
        )

    def test_cmd_up_returns_conflict_for_existing_active_box(self) -> None:
        existing = BOX.Box(id="alpha", profile="dev-small", state="ready")
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_profile", return_value=BOX.BoxProfile(id="dev-small")), \
            mock.patch.object(BOX, "load_inventory", return_value=[existing]), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_up(
                "alpha",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest=None,
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "conflict")

    def test_cmd_up_requires_deploy_manifest_for_non_dry_run(self) -> None:
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_profile", return_value=BOX.BoxProfile(id="dev-small", storage=_storage())), \
            mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_up(
                "alpha",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest=None,
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "deploy_manifest_required")

    def test_cmd_up_requires_storage_layout_for_digitalocean_profile(self) -> None:
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_profile", return_value=BOX.BoxProfile(id="dev-small")), \
            mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "load_deploy_manifest", return_value=_release()), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_up(
                "alpha",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest="/tmp/deploy.json",
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "storage_layout_missing")

    def test_cmd_up_returns_first_box_failure_payload(self) -> None:
        profile = BOX.BoxProfile(id="dev-small", storage=_storage())
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_profile", return_value=profile), \
            mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "load_deploy_manifest", return_value=_release()), \
            mock.patch.object(BOX, "require_env", side_effect=["do-token", "ssh-key", "ts-auth"]), \
            mock.patch.object(BOX, "do_create_droplet", return_value={"id": 42}), \
            mock.patch.object(BOX, "do_droplet_public_ip", return_value="1.2.3.4"), \
            mock.patch.object(BOX, "do_find_volume_by_name", return_value=None), \
            mock.patch.object(BOX, "do_create_volume", return_value={"id": "vol-1", "size_gigabytes": 20}), \
            mock.patch.object(BOX, "do_attach_volume"), \
            mock.patch.object(BOX, "do_get_volume", return_value={"id": "vol-1", "size_gigabytes": 20, "droplet_ids": [42]}), \
            mock.patch.object(BOX, "wait_for_ssh", side_effect=[True, True, True]), \
            mock.patch.object(BOX, "ssh_script", side_effect=[_completed(), _completed(), _completed()]), \
            mock.patch.object(BOX, "scp_file", return_value=_completed()), \
            mock.patch.object(
                BOX,
                "ssh_cmd",
                side_effect=[
                    _completed(stdout="100.64.0.1\n"),
                    _completed(returncode=1, stderr="first-box failed"),
                ],
            ), \
            mock.patch.object(BOX, "save_inventory"), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_up(
                "alpha",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest="/tmp/deploy.json",
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "first_box_failed")
        self.assertEqual(payloads[-1]["steps"][-1]["step"], "first-box")
        self.assertEqual(payloads[-1]["steps"][-1]["status"], "fail")

    def test_cmd_down_marks_box_destroyed_and_reports_steps(self) -> None:
        box = BOX.Box(
            id="teardown",
            profile="dev-small",
            state="ready",
            droplet_id="77",
            droplet_ip="1.2.3.4",
            tailscale_hostname="skillbox-teardown",
            ssh_user="skillbox",
        )
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_inventory", return_value=[box]), \
            mock.patch.object(BOX, "optional_env", return_value=""), \
            mock.patch.object(BOX, "resolve_box_ssh_target", return_value="1.2.3.4"), \
            mock.patch.object(BOX, "ssh_cmd", side_effect=[_completed(returncode=1), _completed()]), \
            mock.patch.object(BOX, "do_delete_droplet", return_value=True), \
            mock.patch.object(BOX, "save_inventory"), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_down("teardown", dry_run=False, fmt="json")

        self.assertEqual(result, BOX.EXIT_OK)
        self.assertEqual(box.state, "destroyed")
        payload = payloads[-1]
        self.assertEqual([step["status"] for step in payload["steps"]], ["warn", "ok", "ok"])

    def test_cmd_down_returns_destroy_failed_and_preserves_state(self) -> None:
        box = BOX.Box(
            id="teardown",
            profile="dev-small",
            state="ready",
            droplet_id="77",
            droplet_ip="1.2.3.4",
            tailscale_hostname="skillbox-teardown",
            ssh_user="skillbox",
        )
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_inventory", return_value=[box]), \
            mock.patch.object(BOX, "optional_env", return_value=""), \
            mock.patch.object(BOX, "resolve_box_ssh_target", return_value="1.2.3.4"), \
            mock.patch.object(BOX, "ssh_cmd", side_effect=[_completed(), _completed()]), \
            mock.patch.object(BOX, "do_delete_droplet", return_value=False), \
            mock.patch.object(BOX, "save_inventory"), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_down("teardown", dry_run=False, fmt="json")

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertNotEqual(box.state, "destroyed")
        payload = payloads[-1]
        self.assertEqual(payload["error"]["type"], "destroy_failed")
        self.assertEqual(payload["steps"][-1]["step"], "destroy")
        self.assertEqual(payload["steps"][-1]["status"], "fail")

    def test_cmd_down_returns_not_found_payload(self) -> None:
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_down("ghost", dry_run=False, fmt="json")

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "not_found")

    def test_cmd_down_dry_run_skips_all_steps(self) -> None:
        box = BOX.Box(id="teardown", profile="dev-small", state="ready", droplet_id="77")
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_inventory", return_value=[box]), \
            mock.patch.object(BOX, "optional_env", return_value=""), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_down("teardown", dry_run=True, fmt="json")

        self.assertEqual(result, BOX.EXIT_OK)
        self.assertEqual(
            [step["status"] for step in payloads[-1]["steps"]],
            ["skip", "skip", "skip"],
        )

    def test_cmd_down_skips_unreachable_cleanup_paths(self) -> None:
        box = BOX.Box(id="teardown", profile="dev-small", state="creating", droplet_id=None, droplet_ip=None)
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_inventory", return_value=[box]), \
            mock.patch.object(BOX, "optional_env", return_value=""), \
            mock.patch.object(BOX, "resolve_box_ssh_target", return_value=None), \
            mock.patch.object(BOX, "save_inventory"), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_down("teardown", dry_run=False, fmt="json")

        self.assertEqual(result, BOX.EXIT_OK)
        self.assertEqual(
            [step["status"] for step in payloads[-1]["steps"]],
            ["skip", "skip", "skip"],
        )

    def test_cmd_down_text_mode_covers_warning_and_failure_branches(self) -> None:
        box = BOX.Box(
            id="teardown",
            profile="dev-small",
            state="ready",
            droplet_id="77",
            droplet_ip="1.2.3.4",
            tailscale_hostname="skillbox-teardown",
            ssh_user="skillbox",
        )

        with mock.patch.object(BOX, "load_inventory", return_value=[box]), \
            mock.patch.object(BOX, "optional_env", return_value="token"), \
            mock.patch.object(BOX, "resolve_box_ssh_target", return_value="1.2.3.4"), \
            mock.patch.object(
                BOX,
                "ssh_cmd",
                side_effect=[
                    _completed(returncode=1),
                    RuntimeError("logout failed"),
                ],
            ), \
            mock.patch.object(BOX, "do_delete_droplet", side_effect=RuntimeError("delete failed")), \
            mock.patch.object(BOX, "save_inventory"), \
            mock.patch("builtins.print") as print_mock:
            result = BOX.cmd_down("teardown", dry_run=False, fmt="text")

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertNotEqual(box.state, "destroyed")
        self.assertTrue(print_mock.called)

    def test_cmd_status_lists_only_active_boxes(self) -> None:
        active = BOX.Box(id="active", profile="dev-small", state="ready")
        destroyed = BOX.Box(id="gone", profile="dev-small", state="destroyed")
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_inventory", return_value=[active, destroyed]), \
            mock.patch.object(BOX, "box_health", return_value={"id": "active", "state": "ready"}), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_status(None, fmt="json")

        self.assertEqual(result, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["boxes"], [{"id": "active", "state": "ready"}])
        self.assertEqual(payloads[-1]["next_actions"], [])

    def test_cmd_status_unknown_box_returns_error_payload(self) -> None:
        payloads: list[dict[str, object]] = []

        with mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_status("missing", fmt="json")

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "not_found")

    def test_box_health_prefers_down_when_ssh_is_unreachable(self) -> None:
        box = BOX.Box(
            id="broken",
            profile="dev-small",
            state="ready",
            droplet_ip="1.2.3.4",
            tailscale_hostname="skillbox-broken",
            ssh_user="skillbox",
        )

        with mock.patch.object(BOX, "ssh_cmd", return_value=_completed(returncode=1)):
            status = BOX.box_health(box)

        self.assertFalse(status["ssh_reachable"])
        self.assertEqual(status["next_actions"], ["box down broken"])

    def test_cmd_list_reports_empty_and_text_modes(self) -> None:
        payloads: list[dict[str, object]] = []
        active = BOX.Box(id="box-1", profile="dev-small", state="ready", droplet_ip="1.2.3.4")

        with mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_list(fmt="json")

        self.assertEqual(result, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["next_actions"], ["box up <id> --profile <name>"])

        with mock.patch.object(BOX, "load_inventory", return_value=[active]), \
            mock.patch("builtins.print") as print_mock:
            result = BOX.cmd_list(fmt="text")

        self.assertEqual(result, BOX.EXIT_OK)
        print_mock.assert_called()

    def test_main_dispatches_commands_and_wraps_errors(self) -> None:
        dispatch_cases = [
            (["up", "alpha"], "cmd_up"),
            (["down", "alpha"], "cmd_down"),
            (["status", "alpha"], "cmd_status"),
            (["ssh", "alpha"], "cmd_ssh"),
            (["list"], "cmd_list"),
            (["profiles"], "cmd_profiles"),
        ]

        for argv, handler_name in dispatch_cases:
            with self.subTest(command=argv[0]):
                emit_json = mock.Mock()
                with mock.patch.object(sys, "argv", ["box.py", *argv]), \
                    mock.patch.object(BOX, "load_dotenv"), \
                    mock.patch.object(BOX, "emit_json", emit_json), \
                    mock.patch.object(BOX, "cmd_up", return_value=11), \
                    mock.patch.object(BOX, "cmd_down", return_value=12), \
                    mock.patch.object(BOX, "cmd_status", return_value=13), \
                    mock.patch.object(BOX, "cmd_ssh", return_value=14), \
                    mock.patch.object(BOX, "cmd_list", return_value=15), \
                    mock.patch.object(BOX, "cmd_profiles", return_value=16):
                    result = BOX.main()

                expected = {
                    "cmd_up": 11,
                    "cmd_down": 12,
                    "cmd_status": 13,
                    "cmd_ssh": 14,
                    "cmd_list": 15,
                    "cmd_profiles": 16,
                }[handler_name]
                self.assertEqual(result, expected)
                emit_json.assert_not_called()

        error_payloads: list[dict[str, object]] = []
        with mock.patch.object(sys, "argv", ["box.py", "list"]), \
            mock.patch.object(BOX, "load_dotenv"), \
            mock.patch.object(BOX, "cmd_list", side_effect=RuntimeError("boom")), \
            mock.patch.object(BOX, "emit_json", side_effect=error_payloads.append):
            result = BOX.main()

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertEqual(error_payloads[-1]["error"]["message"], "boom")

        error_payloads.clear()
        with mock.patch.object(sys, "argv", ["box.py", "list"]), \
            mock.patch.object(BOX, "load_dotenv"), \
            mock.patch.object(
                BOX,
                "cmd_list",
                side_effect=subprocess.TimeoutExpired(cmd=["list"], timeout=5),
            ), \
            mock.patch.object(BOX, "emit_json", side_effect=error_payloads.append):
            result = BOX.main()

        self.assertEqual(result, BOX.EXIT_ERROR)
        self.assertEqual(error_payloads[-1]["error"]["type"], "timeout")


if __name__ == "__main__":
    unittest.main()
