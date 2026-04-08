from __future__ import annotations

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
            mock.patch.object(BOX_MODULE, "_enroll_box_tailscale", side_effect=fake_enroll_box_tailscale),
            mock.patch.object(BOX_MODULE, "_deploy_box_runtime", side_effect=fake_deploy_box_runtime),
            mock.patch.object(
                BOX_MODULE,
                "_run_box_first_box",
                return_value={"client_id": "box-1", "active_profiles": ["core"], "status": "ok"},
            ),
            mock.patch.object(BOX_MODULE, "save_inventory"),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=payloads.append),
        ):
            result = BOX_MODULE.cmd_up(
                "box-1",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest="deploy.json",
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX_MODULE.EXIT_OK)
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["box_id"], "box-1")
        self.assertEqual([step["step"] for step in payload["steps"]], ["create", "storage", "bootstrap", "enroll", "deploy", "first-box"])
        self.assertTrue(all(step["status"] == "ok" for step in payload["steps"]))
        self.assertEqual(payload["droplet_ip"], "1.2.3.4")
        self.assertEqual(payload["tailscale_ip"], "100.64.0.8")

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
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX_MODULE.EXIT_ERROR)
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["error"]["type"], "bootstrap_failed")
        self.assertEqual([step["status"] for step in payload["steps"]], ["ok", "ok", "fail"])

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
