from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"
BOX_MODULE = SourceFileLoader(
    "skillbox_box",
    str(BOX_SCRIPT.resolve()),
).load_module()


class BoxTests(unittest.TestCase):
    """Test box.py core logic: profiles, inventory, structured output, dry-run."""

    def test_build_remote_env_command_preserves_literal_env_values(self) -> None:
        command = BOX_MODULE.build_remote_env_command(
            ["bash", "-s"],
            {"TAILSCALE_AUTHKEY": "tskey-abc'; touch /tmp/pwned #"},
        )

        self.assertEqual(
            shlex.split(command),
            ["env", "TAILSCALE_AUTHKEY=tskey-abc'; touch /tmp/pwned #", "bash", "-s"],
        )

    def test_build_onboard_command_preserves_literal_blueprint_and_set_args(self) -> None:
        blueprint = "/tmp/client blueprint.yaml"
        set_args = [
            "PRIMARY_REPO_URL=https://example.com/repo?a=1&b=2",
            "PROJECT_NAME=one; touch /tmp/pwned",
        ]

        command = BOX_MODULE.build_onboard_command("client-box", blueprint, set_args)
        tokens = shlex.split(command)

        self.assertEqual(tokens[:5], ["cd", "&&", "cd", "skillbox", "&&"])
        self.assertEqual(
            tokens[5:],
            BOX_MODULE.build_onboard_manage_argv("client-box", blueprint, set_args),
        )

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
            self.assertIn("steps", payload)
            step_names = [s["step"] for s in payload["steps"]]
            self.assertEqual(step_names, ["create", "bootstrap", "enroll", "deploy", "onboard", "verify"])
            for s in payload["steps"]:
                self.assertEqual(s["status"], "skip", f"step {s['step']} should be skip in dry-run")
            self.assertIn("profile", payload)
            self.assertEqual(payload["profile"]["region"], "nyc3")

    def test_up_fails_without_do_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env_with_inventory(tmpdir)
            # Deliberately omit DO token

            result = self._run(
                "up", "no-token", "--profile", "dev-small", "--format", "json",
                env=env,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("SKILLBOX_DO_TOKEN", payload["error"]["message"])

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
            self.assertEqual(step_names, ["drain", "remove", "destroy"])

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
        }


if __name__ == "__main__":
    unittest.main()
