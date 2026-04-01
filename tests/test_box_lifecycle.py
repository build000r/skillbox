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
        profile = BOX_MODULE.BoxProfile(id="dev-small")
        droplet = {
            "id": 123,
            "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]},
        }
        payloads: list[dict[str, object]] = []

        def fake_ssh_cmd(user: str, host: str, command: str, *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
            if command == "tailscale ip -4":
                return subprocess.CompletedProcess([], 0, "100.64.0.8\n", "")
            if "doctor --format json" in command:
                return subprocess.CompletedProcess([], 0, "{}", "")
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            mock.patch.object(BOX_MODULE, "load_profile", return_value=profile),
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[]),
            mock.patch.object(BOX_MODULE, "require_env", side_effect=["do-token", "ssh-key", "ts-auth"]),
            mock.patch.object(BOX_MODULE, "do_create_droplet", return_value=droplet),
            mock.patch.object(BOX_MODULE, "wait_for_ssh", side_effect=[True, True]),
            mock.patch.object(
                BOX_MODULE,
                "ssh_script",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                ],
            ),
            mock.patch.object(BOX_MODULE, "ssh_cmd", side_effect=fake_ssh_cmd),
            mock.patch.object(BOX_MODULE, "save_inventory"),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=payloads.append),
        ):
            result = BOX_MODULE.cmd_up(
                "box-1",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX_MODULE.EXIT_OK)
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["box_id"], "box-1")
        self.assertEqual([step["step"] for step in payload["steps"]], ["create", "bootstrap", "enroll", "deploy", "onboard", "verify"])
        self.assertTrue(all(step["status"] == "ok" for step in payload["steps"]))
        self.assertEqual(payload["droplet_ip"], "1.2.3.4")
        self.assertEqual(payload["tailscale_ip"], "100.64.0.8")

    def test_cmd_up_bootstrap_failure_returns_structured_error(self) -> None:
        profile = BOX_MODULE.BoxProfile(id="dev-small")
        droplet = {
            "id": 123,
            "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]},
        }
        payloads: list[dict[str, object]] = []

        with (
            mock.patch.object(BOX_MODULE, "load_profile", return_value=profile),
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[]),
            mock.patch.object(BOX_MODULE, "require_env", side_effect=["do-token", "ssh-key", "ts-auth"]),
            mock.patch.object(BOX_MODULE, "do_create_droplet", return_value=droplet),
            mock.patch.object(BOX_MODULE, "wait_for_ssh", return_value=True),
            mock.patch.object(
                BOX_MODULE,
                "ssh_script",
                return_value=subprocess.CompletedProcess([], 1, "", "bootstrap exploded"),
            ),
            mock.patch.object(BOX_MODULE, "save_inventory"),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=payloads.append),
        ):
            result = BOX_MODULE.cmd_up(
                "box-1",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX_MODULE.EXIT_ERROR)
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["error"]["type"], "bootstrap_failed")
        self.assertEqual([step["status"] for step in payload["steps"]], ["ok", "fail"])

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
