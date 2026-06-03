from __future__ import annotations

import os
import subprocess
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"
BOX_MODULE = SourceFileLoader(
    "skillbox_box_lifecycle",
    str(BOX_SCRIPT.resolve()),
).load_module()

FAKE_PROVISIONING_ENV = {
    "SKILLBOX_DO_TOKEN": "do-token",
    "SKILLBOX_DO_SSH_KEY_ID": "ssh-key",
    "SKILLBOX_TS_AUTHKEY": "ts-auth",
}


class BoxLifecycleTests(unittest.TestCase):
    def test_cmd_up_requires_deploy_manifest_for_non_dry_run(self) -> None:
        profile = BOX_MODULE.BoxProfile(id="dev-small")
        payloads: list[dict[str, object]] = []

        with (
            mock.patch.object(BOX_MODULE, "load_profile", return_value=profile),
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[]),
            mock.patch.object(BOX_MODULE, "load_deploy_manifest") as load_deploy_manifest,
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=payloads.append),
        ):
            result = BOX_MODULE.cmd_up(
                "box-1",
                profile_name="dev-small",
                blueprint=None,
                set_args=[],
                deploy_manifest=None,
                resume=False,
                dry_run=False,
                fmt="json",
            )

        self.assertEqual(result, BOX_MODULE.EXIT_ERROR)
        load_deploy_manifest.assert_not_called()
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["error"]["type"], "deploy_manifest_required")
        self.assertIn("--deploy-manifest <path>", payload["next_actions"][0])

    def test_cmd_upgrade_covers_dry_run_success_and_failure_branches(self) -> None:
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
        box = BOX_MODULE.Box(
            id="box-1",
            profile="dev-small",
            state="ready",
            droplet_ip="1.2.3.4",
            tailscale_ip="100.64.0.8",
            ssh_user="skillbox",
            storage_provider="digitalocean",
            state_root="/skillbox-state",
            storage_filesystem="ext4",
            storage_required=True,
            storage_min_free_gb=10.0,
        )
        release = BOX_MODULE.DeployRelease(
            manifest_path=Path("/deploy.json"),
            client_id="box-1",
            source_commit="abc123def4567890",
            payload_tree_sha256="1" * 64,
            archive_path=Path("/tmp/skillbox.tar.gz"),
            archive_sha256="2" * 64,
            active_profiles=["core", "swimmers"],
        )

        emitted: list[dict[str, object]] = []
        with (
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[box]),
            mock.patch.object(BOX_MODULE, "load_deploy_manifest", return_value=release),
            mock.patch.object(BOX_MODULE, "load_profile", return_value=profile),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(
                BOX_MODULE.cmd_upgrade(
                    "box-1",
                    deploy_manifest="/deploy.json",
                    dry_run=True,
                    fmt="json",
                ),
                BOX_MODULE.EXIT_OK,
            )
        self.assertTrue(emitted[-1]["dry_run"])
        self.assertEqual([step["status"] for step in emitted[-1]["steps"]], ["skip", "skip", "skip", "skip"])

        with (
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[box]),
            mock.patch.object(BOX_MODULE, "load_deploy_manifest", return_value=release),
            mock.patch.object(BOX_MODULE, "load_profile", return_value=profile),
            mock.patch.object(BOX_MODULE, "_resolve_existing_box_target", return_value="100.64.0.8"),
            mock.patch.object(
                BOX_MODULE,
                "scp_file",
                return_value=subprocess.CompletedProcess(["scp"], 0, stdout="", stderr=""),
            ) as scp_file,
            mock.patch.object(BOX_MODULE, "_patch_remote_runtime_contract", return_value={"env_updates": ["A"]}),
            mock.patch.object(
                BOX_MODULE,
                "ssh_script",
                return_value=subprocess.CompletedProcess(["ssh"], 0, stdout="upgraded", stderr=""),
            ) as ssh_script,
            mock.patch.object(
                BOX_MODULE,
                "ssh_cmd",
                return_value=subprocess.CompletedProcess(["ssh"], 0, stdout='{"Service":"workspace"}', stderr=""),
            ),
            mock.patch.object(BOX_MODULE, "save_inventory") as save_inventory,
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(
                BOX_MODULE.cmd_upgrade(
                    "box-1",
                    deploy_manifest="/deploy.json",
                    dry_run=False,
                    fmt="json",
                ),
                BOX_MODULE.EXIT_OK,
            )
        scp_file.assert_called_once()
        self.assertIn("--archive", ssh_script.call_args.kwargs["script_args"])
        save_inventory.assert_called_once()
        self.assertFalse(emitted[-1]["dry_run"])
        self.assertEqual(emitted[-1]["steps"][-1]["status"], "ok")

        failure_cases = [
            (
                "missing",
                {"load_inventory": []},
                "not_found",
            ),
            (
                "bad-state",
                {"load_inventory": [BOX_MODULE.Box(id="box-1", profile="dev-small", state="creating")]},
                "invalid_state",
            ),
            (
                "bad-manifest",
                {"load_deploy_manifest": RuntimeError("bad manifest")},
                "deploy_manifest_invalid",
            ),
            (
                "bad-profile",
                {"load_profile": RuntimeError("bad profile")},
                "profile_not_found",
            ),
            (
                "ssh-missing",
                {"resolve_target": RuntimeError("no ssh")},
                "ssh_unreachable",
            ),
            (
                "upload-failed",
                {"scp_file": subprocess.CompletedProcess(["scp"], 1, stdout="", stderr="no upload")},
                "upload_failed",
            ),
            (
                "contract-failed",
                {"patch_contract": RuntimeError("contract bad")},
                "remote_contract_failed",
            ),
            (
                "upgrade-failed",
                {"ssh_script": subprocess.CompletedProcess(["ssh"], 1, stdout="", stderr="upgrade bad")},
                "upgrade_failed",
            ),
            (
                "verify-failed",
                {"ssh_cmd": subprocess.CompletedProcess(["ssh"], 0, stdout='{"Service":"api"}', stderr="")},
                "verify_failed",
            ),
        ]

        for _label, overrides, error_type in failure_cases:
            emitted.clear()
            inventory = overrides.get("load_inventory", [box])
            load_deploy_manifest = overrides.get("load_deploy_manifest", release)
            load_profile = overrides.get("load_profile", profile)
            resolve_target = overrides.get("resolve_target", "100.64.0.8")
            scp_result = overrides.get("scp_file", subprocess.CompletedProcess(["scp"], 0, stdout="", stderr=""))
            patch_contract = overrides.get("patch_contract", {"env_updates": ["A"]})
            ssh_script_result = overrides.get("ssh_script", subprocess.CompletedProcess(["ssh"], 0, stdout="ok", stderr=""))
            ssh_cmd_result = overrides.get("ssh_cmd", subprocess.CompletedProcess(["ssh"], 0, stdout='{"Service":"workspace"}', stderr=""))
            with (
                mock.patch.object(BOX_MODULE, "load_inventory", return_value=inventory),
                mock.patch.object(
                    BOX_MODULE,
                    "load_deploy_manifest",
                    side_effect=load_deploy_manifest if isinstance(load_deploy_manifest, RuntimeError) else None,
                    return_value=None if isinstance(load_deploy_manifest, RuntimeError) else load_deploy_manifest,
                ),
                mock.patch.object(
                    BOX_MODULE,
                    "load_profile",
                    side_effect=load_profile if isinstance(load_profile, RuntimeError) else None,
                    return_value=None if isinstance(load_profile, RuntimeError) else load_profile,
                ),
                mock.patch.object(
                    BOX_MODULE,
                    "_resolve_existing_box_target",
                    side_effect=resolve_target if isinstance(resolve_target, RuntimeError) else None,
                    return_value=None if isinstance(resolve_target, RuntimeError) else resolve_target,
                ),
                mock.patch.object(BOX_MODULE, "scp_file", return_value=scp_result),
                mock.patch.object(
                    BOX_MODULE,
                    "_patch_remote_runtime_contract",
                    side_effect=patch_contract if isinstance(patch_contract, RuntimeError) else None,
                    return_value=None if isinstance(patch_contract, RuntimeError) else patch_contract,
                ),
                mock.patch.object(BOX_MODULE, "ssh_script", return_value=ssh_script_result),
                mock.patch.object(BOX_MODULE, "ssh_cmd", return_value=ssh_cmd_result),
                mock.patch.object(BOX_MODULE, "emit_json", side_effect=emitted.append),
            ):
                self.assertEqual(
                    BOX_MODULE.cmd_upgrade(
                        "box-1",
                        deploy_manifest="/deploy.json",
                        dry_run=False,
                        fmt="json",
                    ),
                    BOX_MODULE.EXIT_ERROR,
                )
            self.assertEqual(emitted[-1]["error"]["type"], error_type)

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
            mock.patch.dict(os.environ, FAKE_PROVISIONING_ENV),
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
            mock.patch.dict(os.environ, FAKE_PROVISIONING_ENV),
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
                "SKILLBOX_SWIMMERS_EXPOSE": "1",
                "SWIMMERS_SPAPS_WEBSITE_AUTH_TOKEN": "secret-token",
            },
            clear=False,
        ):
            payload = BOX_MODULE.remote_box_contract_payload(context)

        env_updates = payload["env_updates"]
        self.assertEqual(env_updates["SKILLBOX_STATE_ROOT"], "/srv/skillbox")
        self.assertEqual(env_updates["SKILLBOX_CLIENTS_HOST_ROOT"], "/srv/skillbox/clients")
        self.assertEqual(env_updates["SKILLBOX_MONOSERVER_HOST_ROOT"], "/srv/skillbox/repos")
        self.assertEqual(env_updates["SKILLBOX_BOX_ID"], "spaps-website")
        self.assertEqual(env_updates["SKILLBOX_BOX_SELF"], "true")
        self.assertEqual(env_updates["SKILLBOX_BOX_TAILSCALE_HOSTNAME"], "skillbox-spaps-website")
        self.assertEqual(env_updates["SKILLBOX_SWIMMERS_PUBLISH_HOST"], "0.0.0.0")
        self.assertEqual(env_updates["SKILLBOX_SWIMMERS_AUTH_MODE"], "token")
        self.assertEqual(env_updates["SKILLBOX_SWIMMERS_AUTH_TOKEN"], "secret-token")
        self.assertEqual(payload["swimmers_auth_token_env"], "SWIMMERS_SPAPS_WEBSITE_AUTH_TOKEN")

    def test_remote_box_contract_payload_rejects_swimmers_token_without_expose_opt_in(self) -> None:
        profile = BOX_MODULE.BoxProfile(id="dev-small")
        box = BOX_MODULE.Box(
            id="spaps-website",
            profile="dev-small",
            state="acceptance",
            ssh_user="skillbox",
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
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "SKILLBOX_SWIMMERS_EXPOSE=1"):
                BOX_MODULE.remote_box_contract_payload(context)

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
        self.assertEqual([step["status"] for step in payloads[0]["steps"]], ["ok", "ok", "ok", "skip"])

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
                subprocess.CompletedProcess([], 0, "ok\n", ""),
                subprocess.CompletedProcess([], 0, '{"Service":"workspace"}\n', ""),
                subprocess.CompletedProcess([], 0, "ok\n", ""),
            ],
        ), mock.patch.object(
            BOX_MODULE, "run", return_value=subprocess.CompletedProcess([], 0, "pong\n", ""),
        ), mock.patch.object(
            BOX_MODULE.socket, "gethostbyname", return_value="100.64.0.8",
        ), mock.patch.object(
            BOX_MODULE.socket, "create_connection",
        ) as create_connection:
            create_connection.return_value.__enter__.return_value = object()
            status = BOX_MODULE.box_health(box)

        self.assertTrue(status["ssh_reachable"])
        self.assertTrue(status["container_running"])
        self.assertTrue(status["network_checks"]["public_ssh"]["ok"])
        self.assertTrue(status["network_checks"]["tailnet_ping"]["ok"])
        self.assertEqual(status["magicdns_url"], "http://skillbox-box-1:3210/")
        self.assertEqual(status["next_actions"], ["box ssh box-1"])

    def test_print_box_status_text_surfaces_phone_url_without_indentation(self) -> None:
        status = {
            "id": "box-1",
            "state": "ready",
            "profile": "dev-small",
            "management_mode": "managed",
            "droplet_id": "123",
            "droplet_ip": "1.2.3.4",
            "tailscale_hostname": "skillbox-box-1",
            "tailscale_ip": "100.64.0.8",
            "ssh_user": "skillbox",
            "state_root": "",
            "volume_name": "",
            "ssh_reachable": True,
            "container_running": True,
            "ssh_target": "100.64.0.8",
            "phone_url": "http://100.64.0.8:3210/",
            "magicdns_url": "http://skillbox-box-1:3210/",
            "network_checks": {},
        }
        stdout = StringIO()

        with redirect_stdout(stdout):
            BOX_MODULE.print_box_status_text(status)

        self.assertIn("\nOpen this on phone: http://100.64.0.8:3210/\n", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
