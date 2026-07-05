from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import clipboard_bootstrap as CB  # noqa: E402


class ClipboardBootstrapTests(unittest.TestCase):
    def test_hosts_json_loads_profiles(self) -> None:
        hosts = CB.load_hosts(ROOT_DIR)
        self.assertIn("d3", hosts["profiles"])
        self.assertIn("conference1", hosts["profiles"])
        self.assertEqual(
            hosts["profiles"]["conference1"]["ssh_target"],
            "worker@conference1-wsl",
        )

    def test_normalize_tilde(self) -> None:
        self.assertEqual(CB.normalize_tilde("~", "/home/skillbox"), "/home/skillbox")
        self.assertEqual(
            CB.normalize_tilde("~/clipboard-images", "/home/skillbox"),
            "/home/skillbox/clipboard-images",
        )
        self.assertEqual(CB.normalize_tilde("/tmp/x", "/home/skillbox"), "/tmp/x")

    def test_resolve_profile_d3(self) -> None:
        resolved = CB.resolve_profile("d3", root=ROOT_DIR)
        self.assertEqual(resolved["ssh_target"], "skillbox@skillbox-portfolio-devbox")
        self.assertEqual(resolved["remote_home"], "/home/skillbox")

    def test_resolve_profile_generic_requires_target(self) -> None:
        with self.assertRaises(ValueError):
            CB.resolve_profile("generic", root=ROOT_DIR)
        resolved = CB.resolve_profile("generic", target="user@example", root=ROOT_DIR)
        self.assertEqual(resolved["ssh_target"], "user@example")

    def test_resolve_clipimg_alias_conference_prefers_wsl(self) -> None:
        profile = CB.resolve_clipimg_alias("c", root=ROOT_DIR)
        self.assertEqual(profile, "conference1")
        resolved = CB.resolve_profile(profile, root=ROOT_DIR)
        self.assertEqual(resolved["ssh_target"], "worker@conference1-wsl")

    def test_default_shell_probe_uses_subprocess(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(CB.default_shell_probe("ssh host true"))
            run.assert_called_once()

    def test_conference_plan_uses_routed_target(self) -> None:
        with (
            mock.patch.object(CB, "default_shell_probe", side_effect=[True, True]),
            mock.patch.object(CB, "select_conference_route", wraps=CB.select_conference_route) as route_fn,
        ):
            plan = CB.plan_remote_bootstrap("conference1", dry_run=True, root=ROOT_DIR)
        self.assertEqual(plan.ssh_target, "worker@conference1-wsl")

    def test_conference_route_direct_wsl_first(self) -> None:
        route = CB.select_conference_route(
            probe_reachable=lambda _cmd: True,
            probe_mosh=lambda _cmd: True,
            root=ROOT_DIR,
        )
        self.assertEqual(route.ssh_target, "worker@conference1-wsl")
        self.assertTrue(route.clipboard_capable)
        self.assertFalse(route.used_fallback)
        self.assertEqual(route.transport, "mosh")

    def test_conference_route_ssh_when_no_mosh(self) -> None:
        route = CB.select_conference_route(
            probe_reachable=lambda _cmd: True,
            probe_mosh=lambda _cmd: False,
            root=ROOT_DIR,
        )
        self.assertEqual(route.transport, "ssh")
        self.assertEqual(route.ssh_target, "worker@conference1-wsl")

    def test_conference_route_fallback_when_unreachable(self) -> None:
        route = CB.select_conference_route(
            probe_reachable=lambda _cmd: False,
            probe_mosh=lambda _cmd: False,
            root=ROOT_DIR,
        )
        self.assertTrue(route.used_fallback)
        self.assertFalse(route.clipboard_capable)
        self.assertEqual(route.ssh_target, "conference1-ssh")

    def test_tmux_fragment_markers(self) -> None:
        content = CB.read_tmux_fragment(ROOT_DIR)
        for marker in CB.expected_tmux_fragment_markers():
            self.assertIn(marker, content, msg=marker)

    def test_clipcopy_client_tty_markers(self) -> None:
        content = (CB.bundle_dir(ROOT_DIR) / "clipcopy").read_text(encoding="utf-8")
        for marker in CB.clipcopy_client_tty_markers():
            self.assertIn(marker, content, msg=marker)

    def test_installer_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            CB.install_local(home, dry_run=False, root=ROOT_DIR)
            issues_first = CB.verify_local_install(home)
            self.assertEqual(issues_first, [])
            self.assertTrue(CB.is_idempotent_reinstall(home, root=ROOT_DIR))
            CB.install_local(home, dry_run=False, root=ROOT_DIR)
            self.assertTrue(CB.is_idempotent_reinstall(home, root=ROOT_DIR))
            tmux_conf = home / ".tmux.conf"
            lines = [line for line in tmux_conf.read_text(encoding="utf-8").splitlines() if CB.TMUX_MARKER in line]
            self.assertEqual(len(lines), 1)

    def test_plan_remote_bootstrap_steps(self) -> None:
        plan = CB.plan_remote_bootstrap("d3", dry_run=True, root=ROOT_DIR)
        self.assertEqual(plan.ssh_target, "skillbox@skillbox-portfolio-devbox")
        joined = "\n".join(plan.steps)
        self.assertIn("xterm-ghostty", joined)
        self.assertIn("~/.local/bin", joined)
        self.assertIn("clipboard.tmux.conf", joined)

    def test_clipimg_put_conference_target_is_direct_wsl(self) -> None:
        content = (CB.bundle_dir(ROOT_DIR) / "clipimg-put").read_text(encoding="utf-8")
        self.assertIn("worker@conference1-wsl", content)
        self.assertIn("OSC52-capable", content)
        self.assertNotIn("conference1-ssh WSL Ubuntu", content)

    def test_bootstrap_cli_help_exits_zero(self) -> None:
        proc = subprocess.run(
            ["bash", str(ROOT_DIR / "scripts" / "clipboard-bootstrap"), "--help"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("d3", proc.stdout)
        self.assertIn("xterm-ghostty", proc.stdout)

    def test_bootstrap_cli_target_without_profile_uses_generic(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                str(ROOT_DIR / "scripts" / "clipboard-bootstrap"),
                "--target",
                "user@example.com",
                "--dry-run",
            ],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("user@example.com", proc.stdout)
        self.assertIn("generic", proc.stdout)

    def test_remote_install_script_terminfo_verification(self) -> None:
        script = CB.remote_install_script()
        self.assertIn("infocmp -x xterm-ghostty >/dev/null", script)
        self.assertIn(CB.TMUX_MARKER, script)
        self.assertIn(CB.SOURCE_LINE, script)
        self.assertIn("SKILLBOX_CLIPBOARD_BUNDLE_B64", script)

    def test_bootstrap_cli_dry_run_d3(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                str(ROOT_DIR / "scripts" / "clipboard-bootstrap"),
                "--profile",
                "d3",
                "--dry-run",
            ],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assertIn("skillbox-portfolio-devbox", proc.stdout)
        self.assertIn("xterm-ghostty", proc.stdout)

    def test_shell_syntax_clipboard_helpers(self) -> None:
        for name in ("clipcopy", "clippaste", "pbcopy", "clipimg-put"):
            path = CB.bundle_dir(ROOT_DIR) / name
            proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, msg=f"{name}: {proc.stderr}")

    def test_make_bundle_tar_contains_helpers(self) -> None:
        import io
        import tarfile

        archive = CB.make_bundle_tar(ROOT_DIR)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = {member.name for member in tar.getmembers()}
        self.assertTrue({"clipcopy", "clippaste", "pbcopy", "tmux.conf"}.issubset(names))


if __name__ == "__main__":
    unittest.main()