from __future__ import annotations

import os
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

    def test_conference_plan_dry_run_uses_static_target(self) -> None:
        plan = CB.plan_remote_bootstrap("conference1", dry_run=True, root=ROOT_DIR, live_probe=False)
        self.assertEqual(plan.ssh_target, "worker@conference1-wsl")

    def test_conference_plan_live_probe_uses_routed_target(self) -> None:
        with mock.patch.object(CB, "default_shell_probe", side_effect=[True, True]):
            plan = CB.plan_remote_bootstrap(
                "conference1",
                dry_run=False,
                root=ROOT_DIR,
                live_probe=True,
            )
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
        self.assertEqual(proc.stderr, "")
        self.assertIn("d3", proc.stdout)
        self.assertIn("worker@conference1-wsl", proc.stdout)
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
        self.assertIn(CB.TERMINFO_BUNDLE_NAME, script)
        self.assertIn("tic -x", script)
        self.assertIn(CB.TMUX_MARKER, script)
        self.assertIn(CB.SOURCE_LINE, script)
        self.assertIn("SKILLBOX_CLIPBOARD_BUNDLE_B64", script)

    def test_run_remote_install_provisions_helpers_and_terminfo(self) -> None:
        if not shutil_which("tic"):
            self.skipTest("tic not available")
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            env = {"TERMINFO": str(home / ".terminfo")}
            result = CB.run_remote_install(home, root=ROOT_DIR, env=env)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((home / ".local" / "bin" / "clipcopy").is_file())
            self.assertTrue((home / ".config" / "skillbox" / "clipboard.tmux.conf").is_file())
            verify = subprocess.run(
                ["infocmp", "-x", "xterm-ghostty"],
                env={**os.environ, "HOME": str(home), "TERMINFO": str(home / ".terminfo")},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, msg=verify.stderr)

    def test_apply_remote_via_ssh_invokes_ssh_runner(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=b"skillbox clipboard bootstrap: ok\n", stderr=b"")

        proc = CB.apply_remote_via_ssh("skillbox@example", root=ROOT_DIR, runner=fake_runner)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("ssh", calls[0])
        self.assertIn("skillbox@example", calls[0])
        self.assertTrue(any(arg.startswith("SKILLBOX_CLIPBOARD_BUNDLE_B64=") for arg in calls[0]))

    def test_apply_remote_via_ssh_wsl_transport(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=b"ok\n", stderr=b"")

        CB.apply_remote_via_ssh("conference1-ssh", root=ROOT_DIR, transport="wsl", runner=fake_runner)
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("wsl -d" in arg for arg in calls[0]))
        self.assertTrue(any("SKILLBOX_CLIPBOARD_BUNDLE_B64=" in arg for arg in calls[0]))

    def test_run_remote_install_writes_valid_tmux_source_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            result = CB.run_remote_install(home, root=ROOT_DIR)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            tmux_conf = home / ".tmux.conf"
            content = tmux_conf.read_text(encoding="utf-8")
            self.assertIn(CB.SOURCE_LINE, content)
            source_lines = [
                line
                for line in content.splitlines()
                if "if-shell" in line and "clipboard.tmux.conf" in line
            ]
            self.assertEqual(len(source_lines), 1)

    def test_run_remote_install_repairs_malformed_tmux_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            tmux_conf = home / ".tmux.conf"
            tmux_conf.write_text(
                "\n".join(
                    [
                        "set -g mouse on",
                        "",
                        "# Skillbox clipboard integration: OSC52 across local tmux, SSH, mosh, and nested tmux.",
                        "if-shell [",
                        "-r",
                        '"$HOME/.config/skillbox/clipboard.tmux.conf"',
                        "]",
                        "'source-file",
                        '"$HOME/.config/skillbox/clipboard.tmux.conf"',
                        "'",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            result = CB.run_remote_install(home, root=ROOT_DIR)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            content = tmux_conf.read_text(encoding="utf-8")
            self.assertIn(CB.SOURCE_LINE, content)
            source_lines = [
                line
                for line in content.splitlines()
                if "if-shell" in line and "clipboard.tmux.conf" in line
            ]
            self.assertEqual(len(source_lines), 1)
            self.assertNotIn("if-shell [", content)

    def test_run_remote_install_repairs_malformed_block_preserves_following_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            tmux_conf = home / ".tmux.conf"
            tmux_conf.write_text(
                "\n".join(
                    [
                        "set -g mouse on",
                        "# Skillbox clipboard integration: OSC52 across local tmux, SSH, mosh, and nested tmux.",
                        "if-shell [",
                        "-r",
                        '"$HOME/.config/skillbox/clipboard.tmux.conf"',
                        "]",
                        "'source-file",
                        '"$HOME/.config/skillbox/clipboard.tmux.conf"',
                        "'",
                        "setw -g mode-keys vi",
                    ]
                ),
                encoding="utf-8",
            )
            result = CB.run_remote_install(home, root=ROOT_DIR)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            content = tmux_conf.read_text(encoding="utf-8")
            self.assertIn("set -g mouse on", content)
            self.assertIn("setw -g mode-keys vi", content)
            self.assertIn(CB.SOURCE_LINE, content)

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
        self.assertTrue({"clipcopy", "clippaste", "pbcopy", "tmux.conf", CB.TERMINFO_BUNDLE_NAME}.issubset(names))


def shutil_which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)


if __name__ == "__main__":
    unittest.main()