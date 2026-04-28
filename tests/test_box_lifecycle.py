from __future__ import annotations

import os
import subprocess
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"
BOX_MODULE = SourceFileLoader(
    "skillbox_box_lifecycle",
    str(BOX_SCRIPT.resolve()),
).load_module()


class BoxLifecycleTests(unittest.TestCase):
    def test_cmd_up_successful_run_records_steps(self) -> None:
        profile = BOX_MODULE.BoxProfile(
            id="dev-small",
            storage=BOX_MODULE.BoxProfileStorage(
                provider="digitalocean",
                mount_path="/skillbox-state",
                filesystem="ext4",
                required=True,
                min_free_gb=10.0,
            ),
        )
        payloads: list[dict[str, object]] = []

        def fake_create_box_droplet(context: BOX_MODULE.BoxUpContext, *, ssh_key_id: str) -> str:
            del ssh_key_id
            BOX_MODULE.update_box(context.box, droplet_id="123", droplet_ip="1.2.3.4", state="bootstrapping")
            context.boxes.append(context.box)
            return "droplet 123 at 1.2.3.4"

        def fake_ensure_box_storage(context: BOX_MODULE.BoxUpContext) -> str:
            BOX_MODULE.update_box(context.box, volume_id="vol-123", volume_name="skillbox-box-1", state="bootstrapping")
            return "attached volume skillbox-box-1 (100GiB) at /skillbox-state"

        def fake_enroll_box_tailscale(context: BOX_MODULE.BoxUpContext, *, ts_authkey: str) -> str:
            del ts_authkey
            BOX_MODULE.update_box(context.box, tailscale_ip="100.64.0.8", state="deploying")
            return "tailscale skillbox-box-1 at 100.64.0.8"

        def fake_mark_box_ssh_ready(context: BOX_MODULE.BoxUpContext) -> str:
            context.ssh_target = "1.2.3.4"
            BOX_MODULE.update_box(context.box, state="ssh-ready")
            return "ssh skillbox@1.2.3.4"

        def fake_deploy_box_runtime(context: BOX_MODULE.BoxUpContext) -> str:
            context.ssh_target = context.ts_hostname
            BOX_MODULE.update_box(context.box, state="acceptance")
            return "container running"

        with (
            mock.patch.object(BOX_MODULE, "load_profile", return_value=profile),
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[]),
            mock.patch.object(BOX_MODULE, "require_env", side_effect=["do-token", "ssh-key", "ts-auth"]),
            mock.patch.object(BOX_MODULE, "load_deploy_manifest", return_value=None),
            mock.patch.object(BOX_MODULE, "_create_box_droplet", side_effect=fake_create_box_droplet),
            mock.patch.object(BOX_MODULE, "_ensure_box_storage", side_effect=fake_ensure_box_storage),
            mock.patch.object(BOX_MODULE, "_bootstrap_box_host", return_value="bootstrap ok"),
            mock.patch.object(BOX_MODULE, "_mark_box_ssh_ready", side_effect=fake_mark_box_ssh_ready),
            mock.patch.object(BOX_MODULE, "_enroll_box_tailscale", side_effect=fake_enroll_box_tailscale),
            mock.patch.object(BOX_MODULE, "_deploy_box_runtime", side_effect=fake_deploy_box_runtime),
            mock.patch.object(BOX_MODULE, "_patch_remote_runtime_contract", return_value={"env_updates": ["SKILLBOX_STATE_ROOT"]}),
            mock.patch.object(BOX_MODULE, "_launch_remote_workspace", return_value={"targets": ["build", "up"]}),
            mock.patch.object(
                BOX_MODULE,
                "_run_box_first_box",
                return_value={"client_id": "box-1", "active_profiles": ["core"], "status": "ok"},
            ) as run_first_box,
            mock.patch.object(BOX_MODULE, "_verify_operator_swimmers_surface", return_value={"skipped": "no swimmers profile"}),
            mock.patch.object(BOX_MODULE, "save_inventory"),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=payloads.append),
        ):
            result = BOX_MODULE.cmd_up(
                "box-1",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest="deploy.json",
                resume=False,
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX_MODULE.EXIT_OK)
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["box_id"], "box-1")
        self.assertEqual(
            [step["step"] for step in payload["steps"]],
            ["create", "storage", "bootstrap", "ssh-ready", "enroll", "deploy", "contract", "launch", "first-box", "verify"],
        )
        self.assertTrue(all(step["status"] == "ok" for step in payload["steps"]))
        self.assertEqual(payload["droplet_ip"], "1.2.3.4")
        self.assertEqual(payload["tailscale_ip"], "100.64.0.8")
        run_first_box.assert_called_once_with(
            mock.ANY,
            blueprint=BOX_MODULE.DEFAULT_FIRST_BOX_BLUEPRINT,
            set_args=[],
        )

    def test_cmd_up_bootstrap_failure_returns_structured_error(self) -> None:
        profile = BOX_MODULE.BoxProfile(
            id="dev-small",
            storage=BOX_MODULE.BoxProfileStorage(
                provider="digitalocean",
                mount_path="/skillbox-state",
                filesystem="ext4",
                required=True,
                min_free_gb=10.0,
            ),
        )
        payloads: list[dict[str, object]] = []

        def fake_create_box_droplet(context: BOX_MODULE.BoxUpContext, *, ssh_key_id: str) -> str:
            del ssh_key_id
            BOX_MODULE.update_box(context.box, droplet_id="123", droplet_ip="1.2.3.4", state="bootstrapping")
            context.boxes.append(context.box)
            return "droplet 123 at 1.2.3.4"

        def fake_ensure_box_storage(context: BOX_MODULE.BoxUpContext) -> str:
            BOX_MODULE.update_box(context.box, volume_id="vol-123", volume_name="skillbox-box-1", state="bootstrapping")
            return "attached volume skillbox-box-1 (100GiB) at /skillbox-state"

        with (
            mock.patch.object(BOX_MODULE, "load_profile", return_value=profile),
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[]),
            mock.patch.object(BOX_MODULE, "require_env", side_effect=["do-token", "ssh-key", "ts-auth"]),
            mock.patch.object(BOX_MODULE, "load_deploy_manifest", return_value=None),
            mock.patch.object(BOX_MODULE, "_create_box_droplet", side_effect=fake_create_box_droplet),
            mock.patch.object(BOX_MODULE, "_ensure_box_storage", side_effect=fake_ensure_box_storage),
            mock.patch.object(BOX_MODULE, "_bootstrap_box_host", side_effect=RuntimeError("bootstrap exploded")),
            mock.patch.object(BOX_MODULE, "save_inventory"),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=payloads.append),
        ):
            result = BOX_MODULE.cmd_up(
                "box-1",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest="deploy.json",
                resume=False,
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX_MODULE.EXIT_ERROR)
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["error"]["type"], "bootstrap_failed")
        self.assertEqual([step["status"] for step in payload["steps"]], ["ok", "ok", "fail"])

    def test_cmd_up_resume_closes_out_partial_ssh_ready_box(self) -> None:
        profile = BOX_MODULE.BoxProfile(
            id="dev-small",
            storage=BOX_MODULE.BoxProfileStorage(
                provider="digitalocean",
                mount_path="/srv/skillbox",
                filesystem="ext4",
                required=True,
                min_free_gb=10.0,
            ),
        )
        box = BOX_MODULE.Box(
            id="box-1",
            profile="dev-small",
            state="ssh-ready",
            droplet_id="123",
            droplet_ip="1.2.3.4",
            tailscale_hostname="skillbox-box-1",
            tailscale_ip="100.64.0.8",
            ssh_user="skillbox",
            state_root="/srv/skillbox",
            storage_provider="digitalocean",
            storage_filesystem="ext4",
            storage_required=True,
            storage_min_free_gb=10.0,
        )
        release = BOX_MODULE.DeployRelease(
            manifest_path=Path("deploy.json"),
            client_id="box-1",
            source_commit="abc123def456",
            payload_tree_sha256="1" * 64,
            archive_path=Path("skillbox.tar.gz"),
            archive_sha256="2" * 64,
            active_profiles=["core"],
        )
        payloads: list[dict[str, object]] = []

        def fake_resolve(context: BOX_MODULE.BoxUpContext) -> str:
            context.ssh_target = "100.64.0.8"
            return "100.64.0.8"

        with (
            mock.patch.object(BOX_MODULE, "load_profile", return_value=profile),
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[box]),
            mock.patch.object(BOX_MODULE, "load_deploy_manifest", return_value=release),
            mock.patch.object(BOX_MODULE, "_resolve_deploy_target", side_effect=fake_resolve),
            mock.patch.object(BOX_MODULE, "_deploy_box_runtime", return_value="installed release abc123def456"),
            mock.patch.object(BOX_MODULE, "_patch_remote_runtime_contract", return_value={"env_updates": ["SKILLBOX_STATE_ROOT"]}),
            mock.patch.object(BOX_MODULE, "_launch_remote_workspace", return_value={"targets": ["build", "up"]}),
            mock.patch.object(
                BOX_MODULE,
                "_run_box_first_box",
                return_value={"client_id": "box-1", "active_profiles": ["core"], "status": "ok"},
            ) as run_first_box,
            mock.patch.object(BOX_MODULE, "_verify_operator_swimmers_surface", return_value={"skipped": "no swimmers profile"}),
            mock.patch.object(BOX_MODULE, "save_inventory"),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=payloads.append),
        ):
            result = BOX_MODULE.cmd_up(
                "box-1",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest="deploy.json",
                resume=True,
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX_MODULE.EXIT_OK)
        self.assertEqual(box.state, "ready")
        self.assertTrue(payloads[0]["resumed"])
        self.assertEqual(
            [step["step"] for step in payloads[0]["steps"]],
            ["create", "storage", "bootstrap", "ssh-ready", "enroll", "deploy", "contract", "launch", "first-box", "verify"],
        )
        self.assertEqual([step["status"] for step in payloads[0]["steps"]], ["skip", "skip", "skip", "ok", "skip", "ok", "ok", "ok", "ok", "ok"])
        run_first_box.assert_called_once_with(
            mock.ANY,
            blueprint=BOX_MODULE.DEFAULT_FIRST_BOX_BLUEPRINT,
            set_args=[],
        )

    def test_remote_box_contract_payload_uses_state_root_and_derived_swimmers_token(self) -> None:
        profile = BOX_MODULE.BoxProfile(
            id="dev-small",
            storage=BOX_MODULE.BoxProfileStorage(
                provider="digitalocean",
                mount_path="/srv/skillbox",
                filesystem="ext4",
                required=True,
                min_free_gb=10.0,
            ),
        )
        box = BOX_MODULE.Box(
            id="spaps-website",
            profile="dev-small",
            state="acceptance",
            ssh_user="skillbox",
            state_root="/srv/skillbox",
            storage_provider="digitalocean",
            storage_filesystem="ext4",
            storage_required=True,
            storage_min_free_gb=10.0,
        )
        release = BOX_MODULE.DeployRelease(
            manifest_path=Path("deploy.json"),
            client_id="spaps-website",
            source_commit="abc123def456",
            payload_tree_sha256="1" * 64,
            archive_path=Path("skillbox.tar.gz"),
            archive_sha256="2" * 64,
            active_profiles=["swimmers"],
        )
        context = BOX_MODULE.BoxUpContext(
            box_id="spaps-website",
            profile_name="dev-small",
            profile=profile,
            box=box,
            boxes=[box],
            ts_hostname="skillbox-spaps-website",
            is_json=True,
            deploy_release=release,
        )

        with mock.patch.dict(
            os.environ,
            {
                "SKILLBOX_SWIMMERS_PUBLISH_HOST": "127.0.0.1",
                "SWIMMERS_SPAPS_WEBSITE_AUTH_TOKEN": "secret-token",
            },
            clear=False,
        ):
            payload = BOX_MODULE.remote_box_contract_payload(context)

        env_updates = payload["env_updates"]
        self.assertEqual(env_updates["SKILLBOX_STATE_ROOT"], "/srv/skillbox")
        self.assertEqual(env_updates["SKILLBOX_CLIENTS_HOST_ROOT"], "/srv/skillbox/clients")
        self.assertEqual(env_updates["SKILLBOX_MONOSERVER_HOST_ROOT"], "/srv/skillbox/monoserver")
        self.assertEqual(env_updates["SKILLBOX_SWIMMERS_PUBLISH_HOST"], "0.0.0.0")
        self.assertEqual(env_updates["SKILLBOX_SWIMMERS_AUTH_MODE"], "token")
        self.assertEqual(env_updates["SKILLBOX_SWIMMERS_AUTH_TOKEN"], "secret-token")
        self.assertEqual(payload["swimmers_auth_token_env"], "SWIMMERS_SPAPS_WEBSITE_AUTH_TOKEN")

    def test_cmd_down_success_marks_box_destroyed(self) -> None:
        box = BOX_MODULE.Box(
            id="box-1",
            profile="dev-small",
            state="ready",
            droplet_id="123",
            droplet_ip="1.2.3.4",
            tailscale_hostname="skillbox-box-1",
            ssh_user="skillbox",
        )
        payloads: list[dict[str, object]] = []

        with (
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[box]),
            mock.patch.object(BOX_MODULE, "optional_env", return_value=""),
            mock.patch.object(BOX_MODULE, "resolve_box_ssh_target", return_value="1.2.3.4"),
            mock.patch.object(BOX_MODULE, "ssh_cmd", return_value=subprocess.CompletedProcess([], 0, "", "")),
            mock.patch.object(BOX_MODULE, "do_delete_droplet", return_value=True),
            mock.patch.object(BOX_MODULE, "save_inventory"),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=payloads.append),
        ):
            result = BOX_MODULE.cmd_down("box-1", dry_run=False, fmt="json")

        self.assertEqual(result, BOX_MODULE.EXIT_OK)
        self.assertEqual(box.state, "destroyed")
        self.assertEqual([step["status"] for step in payloads[0]["steps"]], ["ok", "ok", "ok"])

    def test_box_health_reports_reachable_container(self) -> None:
        box = BOX_MODULE.Box(
            id="box-1",
            profile="dev-small",
            state="ready",
            droplet_id="123",
            droplet_ip="1.2.3.4",
            tailscale_hostname="skillbox-box-1",
            ssh_user="skillbox",
        )

        with mock.patch.object(
            BOX_MODULE,
            "ssh_cmd",
            side_effect=[
                subprocess.CompletedProcess([], 0, "ok\n", ""),
                subprocess.CompletedProcess([], 0, '{"Service":"workspace"}\n', ""),
            ],
        ):
            status = BOX_MODULE.box_health(box)

        self.assertTrue(status["ssh_reachable"])
        self.assertTrue(status["container_running"])
        self.assertEqual(status["next_actions"], ["box ssh box-1"])


if __name__ == "__main__":
    unittest.main()
