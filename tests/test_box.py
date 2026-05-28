from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"
BOX_MODULE = SourceFileLoader(
    "skillbox_box",
    str(BOX_SCRIPT.resolve()),
).load_module()


class BoxTests(unittest.TestCase):
    """Test box.py core logic: profiles, inventory, structured output, dry-run."""

    def test_workspace_image_installs_mandatory_spaps_cli(self) -> None:
        dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("spaps@0.7.7", dockerfile)

    def test_build_remote_env_command_preserves_literal_env_values(self) -> None:
        command = BOX_MODULE.build_remote_env_command(
            ["bash", "-s"],
            {"TAILSCALE_AUTHKEY": "tskey-abc'; touch /tmp/pwned #"},
        )

        self.assertEqual(
            shlex.split(command),
            ["env", "TAILSCALE_AUTHKEY=tskey-abc'; touch /tmp/pwned #", "bash", "-s"],
        )

    def test_build_first_box_command_preserves_literal_blueprint_and_set_args(self) -> None:
        blueprint = "/tmp/client blueprint.yaml"
        set_args = [
            "PRIMARY_REPO_URL=https://example.com/repo?a=1&b=2",
            "PROJECT_NAME=one; touch /tmp/pwned",
        ]

        command = BOX_MODULE.build_first_box_command(
            "client-box",
            repo_dir="/home/skillbox/skillbox",
            private_path="/home/skillbox/skillbox-config",
            active_profiles=["core", "ops"],
            blueprint=blueprint,
            set_args=set_args,
        )
        tokens = shlex.split(command)

        self.assertEqual(tokens[:3], ["cd", "/home/skillbox/skillbox", "&&"])
        self.assertEqual(
            tokens[3:],
            BOX_MODULE.build_first_box_manage_argv(
                "client-box",
                private_path="/home/skillbox/skillbox-config",
                active_profiles=["core", "ops"],
                blueprint=blueprint,
                set_args=set_args,
            ),
        )

    def test_augment_spaps_tailnet_set_args_adds_browser_visible_defaults(self) -> None:
        result = BOX_MODULE.augment_spaps_tailnet_set_args(
            ["PRIMARY_REPO_URL=https://example.com/app.git"],
            blueprint=BOX_MODULE.DEFAULT_FIRST_BOX_BLUEPRINT,
            tailscale_ip="100.76.6.41",
        )

        self.assertIn("SPAPS_AUTH_BASE_URL=http://100.76.6.41:5173", result)
        self.assertIn("SPAPS_FIXTURE_BASE_URL=http://100.76.6.41:5173", result)
        self.assertIn("SPAPS_BROWSER_API_URL=http://100.76.6.41:3301", result)
        self.assertIn(
            "SPAPS_CORS_ALLOW_ORIGINS=http://100.76.6.41:5173,http://localhost:5173,http://127.0.0.1:5173",
            result,
        )

    def test_augment_spaps_tailnet_set_args_preserves_explicit_values(self) -> None:
        result = BOX_MODULE.augment_spaps_tailnet_set_args(
            [
                "SERVICE_PORT=3000",
                "SPAPS_AUTH_PORT=4401",
                "SPAPS_BROWSER_API_URL=http://custom:4401",
            ],
            blueprint=BOX_MODULE.DEFAULT_FIRST_BOX_BLUEPRINT,
            tailscale_ip="100.76.6.41",
        )

        self.assertIn("SPAPS_BROWSER_API_URL=http://custom:4401", result)
        self.assertNotIn("SPAPS_BROWSER_API_URL=http://100.76.6.41:4401", result)
        self.assertIn("SPAPS_AUTH_BASE_URL=http://100.76.6.41:3000", result)

    def test_augment_spaps_tailnet_set_args_skips_plain_blueprint(self) -> None:
        result = BOX_MODULE.augment_spaps_tailnet_set_args(
            ["PRIMARY_REPO_URL=https://example.com/app.git"],
            blueprint="git-repo-http-service-bootstrap",
            tailscale_ip="100.76.6.41",
        )

        self.assertEqual(result, ["PRIMARY_REPO_URL=https://example.com/app.git"])

    def test_build_release_install_args_uses_offline_archive_and_skips_first_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive_path = root / "skillbox.tar.gz"
            archive_path.write_bytes(b"fixture release archive\n")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            manifest_path = root / "deploy.json"
            manifest_path.write_text(json.dumps({
                "client_id": "personal",
                "source_commit": "abc123def456",
                "payload_tree_sha256": "1" * 64,
                "archive": "skillbox.tar.gz",
                "archive_sha256": archive_sha256,
            }), encoding="utf-8")

            release = BOX_MODULE.load_deploy_manifest(manifest_path, expected_client_id="personal")
            args = BOX_MODULE.build_release_install_args(
                "personal",
                release,
                remote_archive_path="/home/skillbox/skillbox.tar.gz",
                repo_dir="/home/skillbox/skillbox",
                private_path="/home/skillbox/skillbox-config",
            )

            self.assertEqual(
                args,
                [
                    "--offline", "/home/skillbox/skillbox.tar.gz",
                    "--sha256", archive_sha256,
                    "--repo-dir", "/home/skillbox/skillbox",
                    "--private-path", "/home/skillbox/skillbox-config",
                    "--client", "personal",
                    "--skip-build",
                    "--skip-up",
                    "--skip-first-box",
                    "--no-gum",
                ],
            )

    def test_build_remote_workspace_launch_command_adds_swimmers_overlay_step(self) -> None:
        command = BOX_MODULE.build_remote_workspace_launch_command(
            ["core", "swimmers"],
            repo_dir="/home/skillbox/skillbox",
        )

        self.assertEqual(
            shlex.split(command),
            ["cd", "/home/skillbox/skillbox", "&&", "make", "build", "&&", "make", "up", "&&", "make", "swimmers-start"],
        )

    def test_remote_contract_command_rejects_multiline_env_values(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Invalid multiline env value"):
            BOX_MODULE.normalize_remote_env_updates(
                {"SKILLBOX_SWIMMERS_AUTH_TOKEN": "one\nTWO=2"}
            )

    def test_build_release_upgrade_args_carries_non_core_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive_path = root / "skillbox.tar.gz"
            archive_path.write_bytes(b"fixture release archive\n")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            manifest_path = root / "deploy.json"
            manifest_path.write_text(json.dumps({
                "client_id": "personal",
                "source_commit": "abc123def456",
                "payload_tree_sha256": "1" * 64,
                "active_profiles": ["connectors", "core"],
                "archive": "skillbox.tar.gz",
                "archive_sha256": archive_sha256,
            }), encoding="utf-8")

            release = BOX_MODULE.load_deploy_manifest(manifest_path, expected_client_id="personal")
            args = BOX_MODULE.build_release_upgrade_args(
                "personal",
                release,
                remote_archive_path="/home/skillbox/skillbox.tar.gz",
                repo_dir="/home/skillbox/skillbox",
            )

            self.assertEqual(
                args,
                [
                    "--archive", "/home/skillbox/skillbox.tar.gz",
                    "--sha256", archive_sha256,
                    "--repo-dir", "/home/skillbox/skillbox",
                    "--client", "personal",
                    "--profile", "connectors",
                ],
            )

    def test_build_deploy_command_keeps_branch_clone_for_legacy_profiles(self) -> None:
        profile = BOX_MODULE.load_profile("dev-small")
        command = BOX_MODULE.build_deploy_command(profile)
        tokens = shlex.split(command)

        self.assertIn("git", tokens)
        self.assertIn("clone", tokens)
        self.assertIn("--branch", tokens)
        self.assertIn(profile.skillbox_branch, tokens)
        self.assertIn(profile.skillbox_repo, tokens)

    def test_profiles_lists_available_profiles(self) -> None:
        result = self._run("profiles", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("profiles", payload)
        ids = [p["id"] for p in payload["profiles"]]
        self.assertIn("dev-small", ids)
        self.assertIn("dev-large", ids)

    def test_profiles_dev_small_has_expected_fields(self) -> None:
        result = self._run("profiles", "--format", "json")

        payload = json.loads(result.stdout)
        dev_small = next(p for p in payload["profiles"] if p["id"] == "dev-small")
        self.assertEqual(dev_small["provider"], "digitalocean")
        self.assertEqual(dev_small["region"], "nyc3")
        self.assertEqual(dev_small["size"], "s-2vcpu-4gb")
        self.assertEqual(dev_small["image"], "ubuntu-24-04-x64")
        self.assertEqual(dev_small["ssh_user"], "skillbox")

    def test_volume_filesystem_label_drops_state_prefix_for_ext4(self) -> None:
        self.assertEqual(
            BOX_MODULE.volume_filesystem_label("skillbox-state-jeremy", "ext4"),
            "skillbox-jeremy",
        )

    def test_volume_filesystem_label_respects_xfs_length_limit(self) -> None:
        label = BOX_MODULE.volume_filesystem_label("skillbox-state-averylongboxname", "xfs")

        self.assertLessEqual(len(label), 12)
        self.assertRegex(label, r"^[A-Za-z0-9_-]+$")
        self.assertTrue(label.endswith("name"))

    def test_extract_tailscale_ipv4_reads_marker_line(self) -> None:
        output = "\n".join([
            "some log line",
            "TAILSCALE_IPV4=100.101.102.103",
            "more log output",
        ])

        self.assertEqual(
            BOX_MODULE.extract_tailscale_ipv4(output),
            "100.101.102.103",
        )

    def test_extract_tailscale_ipv4_returns_none_without_marker(self) -> None:
        self.assertIsNone(BOX_MODULE.extract_tailscale_ipv4("no marker here"))

    def test_box_helpers_cover_env_probe_unregister_status_and_ssh_branches(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Required environment variable MISSING"):
                BOX_MODULE.require_env("MISSING")
            self.assertEqual(BOX_MODULE.optional_env("OPTIONAL", "fallback"), "fallback")

        with tempfile.TemporaryDirectory() as tmpdir:
            dotenv = Path(tmpdir) / ".env"
            dotenv.write_text("# comment\nFOO=file\nINVALID\nBAR= file-bar \n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FOO": "existing"}, clear=True):
                BOX_MODULE.load_dotenv(dotenv)
                self.assertEqual(os.environ["FOO"], "existing")
                self.assertEqual(os.environ["BAR"], "file-bar")
            BOX_MODULE.load_dotenv(Path(tmpdir) / "missing.env")

        self.assertTrue(BOX_MODULE.is_ipv4_address("1.2.3.4"))
        self.assertFalse(BOX_MODULE.is_ipv4_address("999.1.1.1"))
        self.assertTrue(BOX_MODULE.is_tailscale_ipv4("100.64.0.1"))
        self.assertFalse(BOX_MODULE.is_tailscale_ipv4("8.8.8.8"))
        self.assertEqual(BOX_MODULE.derive_box_id_from_host("skillbox-Team.Example.com"), "team")
        self.assertEqual(BOX_MODULE.derive_box_id_from_host(""), "shared-box")
        self.assertEqual(BOX_MODULE.seed_registered_box_fields("100.64.0.8"), {"tailscale_ip": "100.64.0.8"})
        self.assertEqual(BOX_MODULE.seed_registered_box_fields("8.8.8.8"), {"droplet_ip": "8.8.8.8"})
        self.assertEqual(
            BOX_MODULE.seed_registered_box_fields("skillbox-team"),
            {"tailscale_hostname": "skillbox-team"},
        )
        self.assertEqual(BOX_MODULE.seed_registered_box_fields(""), {})
        self.assertEqual(
            BOX_MODULE.parse_register_probe(
                "SKILLBOX_PROBE_TAILSCALE_IPV4=100.64.0.9\n"
                "SKILLBOX_PROBE_CONTAINER_RUNNING=yes\n"
            ),
            {"tailscale_ip": "100.64.0.9", "container_running": True},
        )

        external_box = BOX_MODULE.Box(
            id="external",
            profile="dev-small",
            state="ready",
            management_mode="external",
            tailscale_hostname="skillbox-external",
        )
        self.assertFalse(BOX_MODULE.probe_registered_box(external_box, enabled=False)["ssh_reachable"])
        with mock.patch.object(BOX_MODULE, "resolve_box_ssh_target", return_value=None):
            self.assertFalse(BOX_MODULE.probe_registered_box(external_box, enabled=True)["ssh_reachable"])
        with (
            mock.patch.object(BOX_MODULE, "resolve_box_ssh_target", return_value="100.64.0.8"),
            mock.patch.object(
                BOX_MODULE,
                "ssh_cmd",
                return_value=subprocess.CompletedProcess(["ssh"], 1, stdout="", stderr="nope"),
            ),
        ):
            probe = BOX_MODULE.probe_registered_box(external_box, enabled=True)
        self.assertTrue(probe["ssh_reachable"])
        self.assertFalse(probe["container_running"])
        with (
            mock.patch.object(BOX_MODULE, "resolve_box_ssh_target", return_value="100.64.0.8"),
            mock.patch.object(
                BOX_MODULE,
                "ssh_cmd",
                return_value=subprocess.CompletedProcess(
                    ["ssh"],
                    0,
                    stdout=(
                        "SKILLBOX_PROBE_TAILSCALE_IPV4=100.64.0.10\n"
                        "SKILLBOX_PROBE_CONTAINER_RUNNING=yes\n"
                    ),
                    stderr="",
                ),
            ),
        ):
            probe = BOX_MODULE.probe_registered_box(external_box, enabled=True)
        self.assertEqual(probe["tailscale_ip"], "100.64.0.10")
        self.assertTrue(probe["container_running"])

        emitted: list[dict[str, object]] = []
        managed_box = BOX_MODULE.Box(id="managed", profile="dev-small", state="ready")
        with (
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[]),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(BOX_MODULE.cmd_unregister("missing", fmt="json"), BOX_MODULE.EXIT_ERROR)
        self.assertEqual(emitted[-1]["error"]["type"], "not_found")

        with (
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[managed_box]),
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(BOX_MODULE.cmd_unregister("managed", fmt="json"), BOX_MODULE.EXIT_ERROR)
        self.assertEqual(emitted[-1]["error"]["type"], "invalid_state")

        with (
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[external_box]),
            mock.patch.object(BOX_MODULE, "save_inventory") as save_inventory,
            mock.patch.object(BOX_MODULE, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(BOX_MODULE.cmd_unregister("external", fmt="json"), BOX_MODULE.EXIT_OK)
        self.assertEqual(external_box.state, "destroyed")
        save_inventory.assert_called_once()
        self.assertTrue(emitted[-1]["unregistered"])

        status = {
            "id": "external",
            "state": "ready",
            "profile": "dev-small",
            "management_mode": "external",
            "droplet_id": "123",
            "droplet_ip": "1.2.3.4",
            "tailscale_hostname": "skillbox-external",
            "tailscale_ip": "100.64.0.8",
            "ssh_user": "skillbox",
            "ssh_reachable": True,
            "ssh_target": "100.64.0.8",
            "container_running": True,
            "state_root": "/state",
            "storage_filesystem": "ext4",
            "volume_name": "vol",
            "volume_size_gb": 50,
        }
        with redirect_stdout(io.StringIO()) as stdout:
            BOX_MODULE.print_box_status_text(status)
        self.assertIn("mode=external", stdout.getvalue())
        self.assertIn("connect: ssh skillbox@100.64.0.8", stdout.getvalue())

        with (
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[]),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(BOX_MODULE.cmd_ssh("missing"), BOX_MODULE.EXIT_ERROR)
        self.assertIn("not found", stderr.getvalue())
        with (
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[managed_box]),
            mock.patch.object(BOX_MODULE, "resolve_box_ssh_target", return_value=None),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(BOX_MODULE.cmd_ssh("managed"), BOX_MODULE.EXIT_ERROR)
        self.assertIn("no reachable address", stderr.getvalue())
        with (
            mock.patch.object(BOX_MODULE, "load_inventory", return_value=[managed_box]),
            mock.patch.object(BOX_MODULE, "resolve_box_ssh_target", return_value="1.2.3.4"),
            mock.patch.object(BOX_MODULE.os, "execvp") as execvp,
        ):
            self.assertEqual(BOX_MODULE.cmd_ssh("managed"), BOX_MODULE.EXIT_ERROR)
        execvp.assert_called_once()

    def test_wait_for_ssh_retries_after_timeout(self) -> None:
        original_ssh_cmd = BOX_MODULE.ssh_cmd
        calls = {"count": 0}

        def fake_ssh_cmd(user: str, host: str, command: str, *, timeout: int = 300):
            calls["count"] += 1
            if calls["count"] == 1:
                raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=timeout)
            return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="ok\n", stderr="")

        BOX_MODULE.ssh_cmd = fake_ssh_cmd
        try:
            self.assertTrue(BOX_MODULE.wait_for_ssh("example-host", user="skillbox", max_wait=1, interval=0))
        finally:
            BOX_MODULE.ssh_cmd = original_ssh_cmd

    def test_list_empty_when_no_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env_with_inventory(tmpdir)
            result = self._run("list", "--format", "json", env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["boxes"], [])

    def test_list_shows_active_boxes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = Path(tmpdir) / "workspace" / "boxes.json"
            inv_path.parent.mkdir(parents=True)
            inv_path.write_text(json.dumps({
                "boxes": [
                    {"id": "test-box", "profile": "dev-small", "state": "ready",
                     "droplet_id": "123", "droplet_ip": "1.2.3.4",
                     "tailscale_hostname": "skillbox-test-box", "tailscale_ip": "100.64.1.1",
                     "ssh_user": "skillbox", "created_at": "2026-01-01T00:00:00Z",
                     "updated_at": "2026-01-01T00:00:00Z", "region": "nyc3", "size": "s-2vcpu-4gb"},
                    {"id": "old-box", "profile": "dev-small", "state": "destroyed",
                     "droplet_id": "456", "droplet_ip": "1.2.3.5",
                     "ssh_user": "skillbox", "created_at": "2025-01-01T00:00:00Z",
                     "updated_at": "2025-06-01T00:00:00Z", "region": "nyc3", "size": "s-2vcpu-4gb"},
                ],
            }))
            env = self._env_with_inventory(tmpdir)
            result = self._run("list", "--format", "json", env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            # Only active boxes shown (not destroyed)
            self.assertEqual(len(payload["boxes"]), 1)
            self.assertEqual(payload["boxes"][0]["id"], "test-box")

    def test_status_returns_error_for_unknown_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env_with_inventory(tmpdir)
            result = self._run("status", "nonexistent", "--format", "json", env=env)

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("error", payload)
            self.assertEqual(payload["error"]["type"], "not_found")

    def test_up_dry_run_shows_planned_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env_with_inventory(tmpdir)
            env.update({
                "SKILLBOX_DO_TOKEN": "fake-token",
                "SKILLBOX_DO_SSH_KEY_ID": "12345",
                "SKILLBOX_TS_AUTHKEY": "tskey-fake",
            })

            result = self._run(
                "up", "dry-test", "--profile", "dev-small", "--dry-run", "--format", "json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["box_id"], "dry-test")
            self.assertTrue(payload["dry_run"])
            self.assertTrue(payload["credential_status"]["ready"])
            self.assertEqual(payload["credential_status"]["missing"], [])
            self.assertIn("steps", payload)
            step_names = [s["step"] for s in payload["steps"]]
            self.assertEqual(step_names, ["create", "storage", "bootstrap", "ssh-ready", "enroll", "deploy", "contract", "launch", "first-box", "verify"])
            for s in payload["steps"]:
                self.assertEqual(s["status"], "skip", f"step {s['step']} should be skip in dry-run")
            self.assertIn("profile", payload)
            self.assertEqual(payload["profile"]["region"], "nyc3")
            self.assertEqual(payload["volume"]["name"], "skillbox-state-dry-test")

    def test_resolve_existing_box_target_prefers_public_when_ssh_ready(self) -> None:
        box = BOX_MODULE.Box(
            id="test-box",
            profile="dev-small",
            state="ssh-ready",
            droplet_ip="1.2.3.4",
            tailscale_ip="100.64.0.10",
            tailscale_hostname="skillbox-test-box",
            ssh_user="skillbox",
        )
        original_wait_for_ssh = BOX_MODULE.wait_for_ssh
        calls: list[str] = []

        def fake_wait_for_ssh(host: str, user: str = "root", *, max_wait: int = 120, interval: int = 5) -> bool:
            calls.append(host)
            return host == "1.2.3.4"

        BOX_MODULE.wait_for_ssh = fake_wait_for_ssh
        try:
            self.assertEqual(BOX_MODULE._resolve_existing_box_target(box), "1.2.3.4")
        finally:
            BOX_MODULE.wait_for_ssh = original_wait_for_ssh

        self.assertEqual(calls[0], "1.2.3.4")

    def test_resolve_existing_box_target_falls_back_to_public(self) -> None:
        box = BOX_MODULE.Box(
            id="test-box",
            profile="dev-small",
            state="ready",
            droplet_ip="1.2.3.4",
            tailscale_ip="100.64.0.10",
            tailscale_hostname="skillbox-test-box",
            ssh_user="skillbox",
        )
        original_wait_for_ssh = BOX_MODULE.wait_for_ssh
        calls: list[str] = []

        def fake_wait_for_ssh(host: str, user: str = "root", *, max_wait: int = 120, interval: int = 5) -> bool:
            calls.append(host)
            return host == "1.2.3.4"

        BOX_MODULE.wait_for_ssh = fake_wait_for_ssh
        try:
            self.assertEqual(BOX_MODULE._resolve_existing_box_target(box), "1.2.3.4")
        finally:
            BOX_MODULE.wait_for_ssh = original_wait_for_ssh

        self.assertEqual(calls[:3], ["100.64.0.10", "skillbox-test-box", "1.2.3.4"])

    def test_up_fails_without_do_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive_path = root / "skillbox.tar.gz"
            archive_path.write_bytes(b"fixture release archive\n")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            manifest_path = root / "deploy.json"
            manifest_path.write_text(json.dumps({
                "client_id": "no-token",
                "source_commit": "abc123def456",
                "payload_tree_sha256": "1" * 64,
                "archive": "skillbox.tar.gz",
                "archive_sha256": archive_sha256,
            }), encoding="utf-8")
            env = self._env_with_inventory(tmpdir)

            result = self._run(
                "up", "no-token", "--profile", "dev-small", "--deploy-manifest", str(manifest_path), "--format", "json",
                env=env,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "provisioning_credentials_missing")
            self.assertIn("SKILLBOX_DO_TOKEN", payload["error"]["message"])
            self.assertEqual(
                payload["credential_status"]["missing"],
                ["SKILLBOX_DO_TOKEN", "SKILLBOX_DO_SSH_KEY_ID", "SKILLBOX_TS_AUTHKEY"],
            )
            self.assertTrue(any("Re-run:" in action for action in payload["next_actions"]))

    def test_up_dry_run_reports_missing_provisioning_credentials_before_real_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env_with_inventory(tmpdir)

            result = self._run(
                "up", "needs-creds", "--profile", "dev-small", "--dry-run", "--format", "json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["credential_status"]["ready"])
            self.assertEqual(
                payload["credential_status"]["missing"],
                ["SKILLBOX_DO_TOKEN", "SKILLBOX_DO_SSH_KEY_ID", "SKILLBOX_TS_AUTHKEY"],
            )
            self.assertIn("Create or update .env.box", " ".join(payload["next_actions"]))

    def test_up_rejects_existing_active_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = Path(tmpdir) / "workspace" / "boxes.json"
            inv_path.parent.mkdir(parents=True)
            inv_path.write_text(json.dumps({
                "boxes": [
                    {"id": "existing", "profile": "dev-small", "state": "ready",
                     "droplet_id": "999", "droplet_ip": "1.2.3.4",
                     "ssh_user": "skillbox", "created_at": "", "updated_at": "",
                     "region": "nyc3", "size": "s-2vcpu-4gb"},
                ],
            }))
            env = self._env_with_inventory(tmpdir)
            env.update({
                "SKILLBOX_DO_TOKEN": "fake-token",
                "SKILLBOX_DO_SSH_KEY_ID": "12345",
                "SKILLBOX_TS_AUTHKEY": "tskey-fake",
            })

            result = self._run(
                "up", "existing", "--profile", "dev-small", "--format", "json",
                env=env,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "conflict")

    def test_down_rejects_unknown_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env_with_inventory(tmpdir)

            result = self._run("down", "ghost", "--format", "json", env=env)

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "not_found")

    def test_down_dry_run_shows_planned_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = Path(tmpdir) / "workspace" / "boxes.json"
            inv_path.parent.mkdir(parents=True)
            inv_path.write_text(json.dumps({
                "boxes": [
                    {"id": "teardown", "profile": "dev-small", "state": "ready",
                     "droplet_id": "777", "droplet_ip": "1.2.3.4",
                     "tailscale_hostname": "skillbox-teardown",
                     "ssh_user": "skillbox", "created_at": "", "updated_at": "",
                     "region": "nyc3", "size": "s-2vcpu-4gb"},
                ],
            }))
            env = self._env_with_inventory(tmpdir)

            result = self._run("down", "teardown", "--dry-run", "--format", "json", env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["dry_run"])
            step_names = [s["step"] for s in payload["steps"]]
            self.assertEqual(step_names, ["drain", "remove", "destroy", "volume"])

    def test_upgrade_dry_run_shows_release_and_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inv_path = root / "workspace" / "boxes.json"
            inv_path.parent.mkdir(parents=True)
            inv_path.write_text(json.dumps({
                "boxes": [
                    {"id": "jeremy", "profile": "dev-small", "state": "ready",
                     "droplet_id": "321", "droplet_ip": "1.2.3.4",
                     "tailscale_hostname": "skillbox-jeremy", "tailscale_ip": "100.64.1.9",
                     "ssh_user": "skillbox", "created_at": "", "updated_at": "",
                     "region": "nyc3", "size": "s-2vcpu-4gb"},
                ],
            }))
            archive_path = root / "skillbox.tar.gz"
            archive_path.write_bytes(b"fixture release archive\n")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            manifest_path = root / "deploy.json"
            manifest_path.write_text(json.dumps({
                "client_id": "jeremy",
                "source_commit": "abc123def456",
                "payload_tree_sha256": "1" * 64,
                "active_profiles": ["connectors", "core"],
                "archive": "skillbox.tar.gz",
                "archive_sha256": archive_sha256,
            }), encoding="utf-8")

            env = self._env_with_inventory(tmpdir)
            result = self._run(
                "upgrade",
                "jeremy",
                "--deploy-manifest",
                str(manifest_path),
                "--dry-run",
                "--format",
                "json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["dry_run"])
            self.assertEqual([step["step"] for step in payload["steps"]], ["upload", "contract", "upgrade", "verify"])
            self.assertTrue(all(step["status"] == "skip" for step in payload["steps"]))
            self.assertEqual(payload["deploy_release"]["source_commit"], "abc123def456")
            self.assertEqual(payload["deploy_release"]["active_profiles"], ["connectors", "core"])

    def test_upgrade_rejects_non_ready_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inv_path = root / "workspace" / "boxes.json"
            inv_path.parent.mkdir(parents=True)
            inv_path.write_text(json.dumps({
                "boxes": [
                    {"id": "jeremy", "profile": "dev-small", "state": "deploying",
                     "droplet_id": "321", "droplet_ip": "1.2.3.4",
                     "tailscale_hostname": "skillbox-jeremy", "tailscale_ip": "100.64.1.9",
                     "ssh_user": "skillbox", "created_at": "", "updated_at": "",
                     "region": "nyc3", "size": "s-2vcpu-4gb"},
                ],
            }))
            archive_path = root / "skillbox.tar.gz"
            archive_path.write_bytes(b"fixture release archive\n")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            manifest_path = root / "deploy.json"
            manifest_path.write_text(json.dumps({
                "client_id": "jeremy",
                "source_commit": "abc123def456",
                "payload_tree_sha256": "1" * 64,
                "archive": "skillbox.tar.gz",
                "archive_sha256": archive_sha256,
            }), encoding="utf-8")

            env = self._env_with_inventory(tmpdir)
            result = self._run(
                "upgrade",
                "jeremy",
                "--deploy-manifest",
                str(manifest_path),
                "--format",
                "json",
                env=env,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "invalid_state")

    def test_upgrade_rejects_mismatched_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inv_path = root / "workspace" / "boxes.json"
            inv_path.parent.mkdir(parents=True)
            inv_path.write_text(json.dumps({
                "boxes": [
                    {"id": "jeremy", "profile": "dev-small", "state": "ready",
                     "droplet_id": "321", "droplet_ip": "1.2.3.4",
                     "tailscale_hostname": "skillbox-jeremy", "tailscale_ip": "100.64.1.9",
                     "ssh_user": "skillbox", "created_at": "", "updated_at": "",
                     "region": "nyc3", "size": "s-2vcpu-4gb"},
                ],
            }))
            archive_path = root / "skillbox.tar.gz"
            archive_path.write_bytes(b"fixture release archive\n")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            manifest_path = root / "deploy.json"
            manifest_path.write_text(json.dumps({
                "client_id": "someone-else",
                "source_commit": "abc123def456",
                "payload_tree_sha256": "1" * 64,
                "archive": "skillbox.tar.gz",
                "archive_sha256": archive_sha256,
            }), encoding="utf-8")

            env = self._env_with_inventory(tmpdir)
            result = self._run(
                "upgrade",
                "jeremy",
                "--deploy-manifest",
                str(manifest_path),
                "--format",
                "json",
                env=env,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "deploy_manifest_invalid")

    def test_inventory_round_trip(self) -> None:
        """Verify inventory serialization and deserialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = Path(tmpdir) / "workspace" / "boxes.json"
            inv_path.parent.mkdir(parents=True)

            original = {
                "boxes": [
                    {"id": "roundtrip", "profile": "dev-small", "state": "ready",
                     "droplet_id": "555", "droplet_ip": "10.0.0.1",
                     "tailscale_hostname": "skillbox-roundtrip", "tailscale_ip": "100.64.2.2",
                     "ssh_user": "skillbox", "created_at": "2026-03-31T00:00:00Z",
                     "updated_at": "2026-03-31T00:00:00Z", "region": "sfo3", "size": "s-4vcpu-8gb"},
                ],
            }
            inv_path.write_text(json.dumps(original))

            env = self._env_with_inventory(tmpdir)
            result = self._run("list", "--format", "json", env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            box = payload["boxes"][0]
            self.assertEqual(box["id"], "roundtrip")
            self.assertEqual(box["region"], "sfo3")
            self.assertEqual(box["tailscale_ip"], "100.64.2.2")

    def test_register_no_probe_creates_external_inventory_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env_with_inventory(tmpdir)
            result = self._run(
                "register",
                "shared-pal",
                "--host",
                "100.64.1.9",
                "--ssh-user",
                "sandbox",
                "--no-probe",
                "--format",
                "json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["registered"])
            self.assertEqual(payload["management_mode"], "external")
            self.assertEqual(payload["tailscale_ip"], "100.64.1.9")

            listed = self._run("list", "--format", "json", env=env)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            listed_payload = json.loads(listed.stdout)
            self.assertEqual(len(listed_payload["boxes"]), 1)
            self.assertEqual(listed_payload["boxes"][0]["management_mode"], "external")

    def test_unregister_hides_registered_box_from_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = Path(tmpdir) / "workspace" / "boxes.json"
            inv_path.parent.mkdir(parents=True)
            inv_path.write_text(json.dumps({
                "boxes": [
                    {
                        "id": "shared-pal",
                        "profile": "shared",
                        "state": "ready",
                        "management_mode": "external",
                        "tailscale_hostname": "skillbox-shared-pal.tailnet.ts.net",
                        "ssh_user": "sandbox",
                        "created_at": "2026-04-15T00:00:00Z",
                        "updated_at": "2026-04-15T00:00:00Z",
                    }
                ]
            }), encoding="utf-8")
            env = self._env_with_inventory(tmpdir)

            unregister = self._run("unregister", "shared-pal", "--format", "json", env=env)
            self.assertEqual(unregister.returncode, 0, unregister.stderr)
            payload = json.loads(unregister.stdout)
            self.assertTrue(payload["unregistered"])

            listed = self._run("list", "--format", "json", env=env)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            listed_payload = json.loads(listed.stdout)
            self.assertEqual(listed_payload["boxes"], [])

    def test_agent_contract_commands_are_deterministic_and_safe_by_default(self) -> None:
        first = self._run("capabilities", "--json")
        second = self._run("capabilities", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["tool"], "skillbox-box")
        self.assertIn("stdout_stderr_contract", payload)
        self.assertIn("--jsno", payload["agent_surfaces"]["json_aliases"])
        self.assertIn(
            "python3 scripts/box.py down <box-id> --dry-run --format json",
            payload["safety"]["dry_run_first"],
        )
        down = next(command for command in payload["commands"] if command["name"] == "down")
        self.assertTrue(down["destructive"])
        self.assertTrue(down["dry_run"])
        register = next(command for command in payload["commands"] if command["name"] == "register")
        self.assertEqual(register["safe_first_try"], "python3 scripts/box.py profiles --format json")
        self.assertIn("register <box-id>", register["mutation_command"])

        docs = self._run("robot-docs", "guide")
        self.assertEqual(docs.returncode, 0, docs.stderr)
        self.assertIn("Skillbox box agent guide", docs.stdout)
        self.assertIn("operator_box_exec", docs.stdout)

        triage = self._run("--robot-triage")
        self.assertEqual(triage.returncode, 0, triage.stderr)
        self.assertEqual(json.loads(triage.stdout)["tool"], "skillbox-box")

    def test_box_json_alias_keeps_stdout_parseable_and_warns_on_stderr(self) -> None:
        result = self._run("profiles", "--jsno")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("profiles", payload)
        self.assertIn("Interpreting --jsno as --format json", result.stderr)

    def test_box_unknown_command_suggests_exact_agent_command(self) -> None:
        result = self._run("statuz", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Did you mean: `box.py status`?", result.stderr)
        self.assertIn("box.py capabilities --json", result.stderr)

    def test_box_id_arguments_reject_path_and_flag_like_values(self) -> None:
        cases = [
            ("up", "../bad", "--dry-run", "--format", "json"),
            ("down", "bad/id", "--dry-run", "--format", "json"),
            ("upgrade", "bad\\id", "--deploy-manifest", "/tmp/deploy.json", "--dry-run", "--format", "json"),
            ("status", "../bad", "--format", "json"),
            ("ssh", "--", "-bad"),
            ("register", "../bad", "--host", "localhost", "--format", "json"),
            ("import", "bad/id", "--host", "localhost", "--format", "json"),
            ("unregister", "../bad", "--format", "json"),
        ]

        for args in cases:
            with self.subTest(args=args):
                result = self._run(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("invalid box_id", result.stderr)

    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        return subprocess.run(
            ["python3", str(BOX_SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
            env=run_env,
        )

    def _env_with_inventory(self, tmpdir: str) -> dict[str, str]:
        """Create an env dict that redirects inventory to a temp directory."""
        # We patch by setting the env var that box.py uses for REPO_ROOT
        # Since box.py derives INVENTORY_PATH from REPO_ROOT, we need a different approach.
        # The simplest: create the workspace dir structure in tmpdir and set it as working dir.
        inv_dir = Path(tmpdir) / "workspace"
        inv_dir.mkdir(parents=True, exist_ok=True)

        # Create a wrapper that overrides INVENTORY_PATH
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "SKILLBOX_BOX_INVENTORY": str(inv_dir / "boxes.json"),
            "SKILLBOX_DO_TOKEN": "",
            "SKILLBOX_DO_SSH_KEY_ID": "",
            "SKILLBOX_TS_AUTHKEY": "",
        }


class BoxArgvHardeningTests(unittest.TestCase):
    """Security guards for ssh argv injection and the swimmers expose gate."""

    def test_validate_ssh_user_rejects_leading_dash(self) -> None:
        with self.assertRaises(ValueError):
            BOX_MODULE._validate_ssh_user("-oProxyCommand=evil")

    def test_validate_ssh_user_rejects_special_chars(self) -> None:
        for bad in ("user;evil", "user space", "user$x", "user@evil", ""):
            with self.assertRaises(ValueError, msg=f"accepted bad ssh user: {bad!r}"):
                BOX_MODULE._validate_ssh_user(bad)

    def test_validate_ssh_user_accepts_standard_unix_names(self) -> None:
        for good in ("skillbox", "root", "deploy_user", "ci-bot", "_svc"):
            self.assertEqual(BOX_MODULE._validate_ssh_user(good), good)

    def test_validate_host_rejects_leading_dash(self) -> None:
        with self.assertRaises(ValueError):
            BOX_MODULE._validate_host("-oProxyCommand=evil")

    def test_validate_host_accepts_dns_and_ipv4(self) -> None:
        for good in ("box.example.com", "10.0.0.1", "host01", "node-1.tailnet"):
            self.assertEqual(BOX_MODULE._validate_host(good), good)

    def test_ssh_cmd_inserts_double_dash_before_destination(self) -> None:
        """ssh_cmd argv must contain '--' before user@host so a malformed user
        can never be parsed as an ssh option."""
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(BOX_MODULE, "run", side_effect=fake_run):
            BOX_MODULE.ssh_cmd("skillbox", "box.example.com", "echo ok")

        argv = captured["argv"]
        self.assertIn("--", argv)
        dash_idx = argv.index("--")
        self.assertEqual(argv[dash_idx + 1], "skillbox@box.example.com")

    def test_scp_file_inserts_double_dash_before_paths(self) -> None:
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(BOX_MODULE, "run", side_effect=fake_run):
            BOX_MODULE.scp_file(Path("/tmp/x"), "skillbox", "box.example.com", "/remote")

        argv = captured["argv"]
        self.assertIn("--", argv)

    def test_swimmers_expose_gate_raises_without_explicit_optin(self) -> None:
        """remote_box_contract_payload must refuse to silently rewrite the
        swimmers publish host to 0.0.0.0 unless SKILLBOX_SWIMMERS_EXPOSE=1 and
        an auth token are both present."""
        # Build a minimal context whose deploy_release activates 'swimmers' and
        # leaves SKILLBOX_SWIMMERS_PUBLISH_HOST empty (i.e. loopback by helper).
        context = _swimmers_context_for_test()
        env = {
            "SKILLBOX_SWIMMERS_PUBLISH_HOST": "",
            # Note: no SKILLBOX_SWIMMERS_EXPOSE set
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(BOX_MODULE, "local_swimmers_auth_token", return_value=("", None)):
            with self.assertRaises(RuntimeError) as cm:
                BOX_MODULE.remote_box_contract_payload(context)
            self.assertIn("SKILLBOX_SWIMMERS_EXPOSE", str(cm.exception))

    def test_swimmers_expose_gate_accepts_explicit_optin_with_token(self) -> None:
        context = _swimmers_context_for_test()
        env = {
            "SKILLBOX_SWIMMERS_PUBLISH_HOST": "127.0.0.1",
            "SKILLBOX_SWIMMERS_EXPOSE": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(BOX_MODULE, "local_swimmers_auth_token", return_value=("tok-abc", "local")):
            payload = BOX_MODULE.remote_box_contract_payload(context)
        self.assertEqual(payload["env_updates"]["SKILLBOX_SWIMMERS_PUBLISH_HOST"], "0.0.0.0")


def _swimmers_context_for_test():
    """Minimal stand-in for BoxUpContext that activates the 'swimmers' profile."""
    from types import SimpleNamespace
    deploy_release = SimpleNamespace(active_profiles=["swimmers"])
    profile = SimpleNamespace(storage=None)
    box = SimpleNamespace(
        state_root="",
        storage_filesystem="",
        storage_min_free_gb=None,
        storage_provider="",
        tailscale_ip="",
    )
    return SimpleNamespace(
        box_id="test-box",
        box=box,
        profile=profile,
        deploy_release=deploy_release,
        ts_hostname="test-box.tailnet",
    )


if __name__ == "__main__":
    unittest.main()
