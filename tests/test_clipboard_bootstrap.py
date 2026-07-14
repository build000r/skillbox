from __future__ import annotations

import contextlib
import io
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
        with mock.patch.object(
            subprocess, "run", return_value=mock.Mock(returncode=0)
        ) as run:
            self.assertTrue(CB.default_shell_probe("ssh host true"))
            run.assert_called_once()

    def test_conference_plan_dry_run_uses_static_target(self) -> None:
        plan = CB.plan_remote_bootstrap(
            "conference1", dry_run=True, root=ROOT_DIR, live_probe=False
        )
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

    def test_unregistered_tmux_pane_never_reads_image_clipboard(self) -> None:
        content = CB.read_tmux_fragment(ROOT_DIR)
        user199 = next(
            line for line in content.splitlines() if line.startswith("bind-key -n User199")
        )
        user198 = next(
            line for line in content.splitlines() if line.startswith("bind-key -n User198")
        )
        self.assertNotIn("else clipboard-smart-paste", user199)
        self.assertNotIn("else clipboard-smart-paste", user198)
        self.assertIn('else tmux send-keys -t "#{pane_id}" -H 16', user198)

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
            lines = [
                line
                for line in tmux_conf.read_text(encoding="utf-8").splitlines()
                if CB.TMUX_MARKER in line
            ]
            self.assertEqual(len(lines), 1)

    def test_local_install_never_reloads_a_running_tmux_server_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            with mock.patch.object(CB.shutil, "which", return_value="/usr/bin/tmux"):
                runner = mock.Mock()
                plan = CB.install_local(
                    home,
                    root=ROOT_DIR,
                    tmux_runner=runner,
                )
            runner.assert_not_called()
            self.assertIn(
                "leave every running local tmux server untouched",
                plan.steps,
            )

    def test_local_tmux_reload_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            with mock.patch.object(CB.shutil, "which", return_value="/usr/bin/tmux"):
                runner = mock.Mock(
                    return_value=subprocess.CompletedProcess([], 0, b"", b"")
                )
                plan = CB.install_local(
                    home,
                    root=ROOT_DIR,
                    reload_current_tmux=True,
                    tmux_runner=runner,
                )
            runner.assert_called_once_with(
                ["tmux", "source-file", str(home / ".tmux.conf")],
                capture_output=True,
                check=False,
            )
            self.assertTrue(
                any("affects all sessions" in step for step in plan.steps)
            )

    def test_unsupported_operator_install_is_no_write_but_dry_run_explains(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            with mock.patch.object(CB.platform, "system", return_value="Windows"):
                plan = CB.install_local(home, dry_run=True, root=ROOT_DIR)
                self.assertEqual(len(plan.steps), 1)
                self.assertIn("no local or remote changes", plan.steps[0])
                with self.assertRaises(CB.UnsupportedOperatorPlatform):
                    CB.install_local(home, root=ROOT_DIR)
            self.assertEqual(list(home.iterdir()), [])

    def test_cli_rejects_unsupported_apply_before_remote_resolution(self) -> None:
        from lib import clipboard_bootstrap_cli as CLI

        with (
            mock.patch.object(CLI, "operator_platform_supported", return_value=False),
            mock.patch.object(
                CLI,
                "unsupported_operator_message",
                return_value="unsupported fixture; no local or remote changes were made",
            ),
            mock.patch.object(CLI, "_resolve_remote_target") as resolve_remote,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            code = CLI.main(["--profile", "d3", "--apply-remote"])
        self.assertEqual(code, 2)
        self.assertIn("no local or remote changes", stderr.getvalue())
        resolve_remote.assert_not_called()

    def test_remote_apply_failure_is_redacted_and_has_exact_resume(self) -> None:
        from lib import clipboard_bootstrap_cli as CLI

        secret = "/home/remote/private/bootstrap-token"
        failed = subprocess.CompletedProcess(
            [], 255, stdout=b"hostile stdout secret", stderr=f"Permission denied {secret}".encode()
        )
        with (
            mock.patch.object(
                CLI,
                "_resolve_remote_target",
                return_value=("d3", "skillbox@fixture"),
            ),
            mock.patch.object(
                CLI,
                "resolve_profile",
                return_value={"profile": "d3", "transport": "ssh"},
            ),
            mock.patch.object(CLI, "apply_remote_via_ssh", return_value=failed),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            code = CLI._apply_remote(ROOT_DIR, "d3", None)  # noqa: SLF001
        self.assertEqual(code, 255)
        self.assertEqual(stdout.getvalue(), "")
        report = stderr.getvalue()
        self.assertIn("class=authentication_failed", report)
        self.assertIn(
            "resume: scripts/clipboard-bootstrap --profile d3 --apply-remote",
            report,
        )
        self.assertIn("remote state may be partial", report)
        self.assertNotIn(secret, report)
        self.assertNotIn("hostile stdout", report)

    def test_remote_reversal_failure_does_not_start_local_reversal(self) -> None:
        from lib import clipboard_bootstrap_cli as CLI

        failed = subprocess.CompletedProcess(
            [], 23, stdout=b"", stderr=b"connection refused at private target"
        )
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = CLI._report_remote_failure(  # noqa: SLF001
                failed,
                action="rollback",
                profile="generic",
                target="user@example",
            )
        self.assertEqual(code, 23)
        report = stderr.getvalue()
        self.assertIn("class=target_unreachable", report)
        self.assertIn("local reversal was not started", report)
        self.assertIn(
            "resume: scripts/clipboard-bootstrap rollback --profile generic --target user@example --apply-remote",
            report,
        )
        self.assertNotIn("private target", report)

    def test_install_uninstall_restores_owned_files_byte_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            original_tmux = b"set -g mouse on\n"
            original_ghostty = b"font-size = 13\n"
            original_d2 = b"#!/bin/sh\necho legacy-d2\n"
            (home / ".tmux.conf").write_bytes(original_tmux)
            ghostty = CB.ghostty_conf_path(home)
            ghostty.parent.mkdir(parents=True)
            ghostty.write_bytes(original_ghostty)
            d2 = home / ".local" / "bin" / "d2"
            d2.parent.mkdir(parents=True)
            d2.write_bytes(original_d2)
            d2.chmod(0o755)

            CB.install_local(home, root=ROOT_DIR)
            cache = CB.installed_python_dir(home) / "__pycache__"
            cache.mkdir()
            (cache / "clipboard_route.cpython-312.pyc").write_bytes(b"owned")
            (cache / "unrelated.cpython-312.pyc").write_bytes(b"preserve")
            result = CB.uninstall_local(home)

            self.assertTrue(result["changed"])
            self.assertEqual((home / ".tmux.conf").read_bytes(), original_tmux)
            self.assertEqual(ghostty.read_bytes(), original_ghostty)
            self.assertEqual(d2.read_bytes(), original_d2)
            self.assertFalse((cache / "clipboard_route.cpython-312.pyc").exists())
            self.assertEqual(
                (cache / "unrelated.cpython-312.pyc").read_bytes(), b"preserve"
            )
            self.assertFalse(CB.lifecycle_state_dir(home).exists())

    def test_uninstall_removes_empty_owned_python_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            CB.install_local(home, root=ROOT_DIR)
            cache = CB.installed_python_dir(home) / "__pycache__"
            cache.mkdir()
            for name in CB.LOCAL_PYTHON_MODULES:
                (cache / f"{Path(name).stem}.cpython-312.pyc").write_bytes(b"owned")
            CB.uninstall_local(home)
            self.assertFalse(cache.exists())

    def test_uninstall_preserves_user_config_added_after_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            CB.install_local(home, root=ROOT_DIR)
            tmux_conf = home / ".tmux.conf"
            ghostty = CB.ghostty_conf_path(home)
            tmux_conf.write_text(tmux_conf.read_text() + "set -g status off\n")
            ghostty.write_text(ghostty.read_text() + "font-size = 14\n")

            CB.uninstall_local(home)

            self.assertEqual(tmux_conf.read_text(), "set -g status off\n")
            self.assertEqual(ghostty.read_text(), "font-size = 14\n")

    def test_install_migrates_legacy_ghostty_include_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            legacy = CB.legacy_ghostty_conf_path(home)
            legacy.parent.mkdir(parents=True)
            legacy.write_text(
                f"font-size = 12\n\n{CB.GHOSTTY_COMMENT}\n{CB.ghostty_source_line(home)}\n"
            )

            CB.install_local(home, root=ROOT_DIR)

            self.assertEqual(legacy.read_text(), "font-size = 12\n")
            current = CB.ghostty_conf_path(home)
            self.assertEqual(current.read_text().count(CB.ghostty_source_line(home)), 1)
            CB.uninstall_local(home)
            self.assertIn(CB.GHOSTTY_COMMENT, legacy.read_text())

    def test_upgrade_creates_one_step_rollback_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            CB.install_local(home, root=ROOT_DIR)
            helper = home / ".local" / "bin" / "clipboard-route"
            prior = helper.read_bytes()
            helper.write_bytes(b"prior pinned helper\n")

            CB.install_local(home, root=ROOT_DIR)
            self.assertEqual(helper.read_bytes(), prior)
            rollback = CB.rollback_local(home)
            self.assertTrue(rollback["ok"])
            self.assertEqual(helper.read_bytes(), b"prior pinned helper\n")

    def test_plan_remote_bootstrap_steps(self) -> None:
        plan = CB.plan_remote_bootstrap("d3", dry_run=True, root=ROOT_DIR)
        self.assertEqual(plan.ssh_target, "skillbox@skillbox-portfolio-devbox")
        joined = "\n".join(plan.steps)
        self.assertIn("xterm-ghostty", joined)
        self.assertIn("~/.local/bin", joined)
        self.assertIn("clipboard.tmux.conf", joined)

    def test_clipimg_put_conference_target_is_direct_wsl(self) -> None:
        content = (CB.bundle_dir(ROOT_DIR) / "clipimg-put").read_text(encoding="utf-8")
        hosts = CB.load_hosts(ROOT_DIR)
        route = CB.resolve_profile(
            CB.resolve_clipimg_alias("c", hosts=hosts), hosts=hosts
        )
        self.assertEqual(route["ssh_target"], "worker@conference1-wsl")
        self.assertIn("clipboard-route", content)
        self.assertIn("direct WSL is preferred", content)
        self.assertNotIn('case "$target_arg"', content)

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
        self.assertNotIn('tmux source-file "$tmux_conf"', script)

    def test_remote_plan_promises_no_running_tmux_reload(self) -> None:
        plan = CB.plan_remote_bootstrap("d3", dry_run=True, root=ROOT_DIR)
        self.assertIn(
            "ssh skillbox@skillbox-portfolio-devbox: leave every running tmux server untouched",
            plan.steps,
        )

    def test_run_remote_install_provisions_helpers_and_terminfo(self) -> None:
        if not shutil_which("tic"):
            self.skipTest("tic not available")
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            env = {"TERMINFO": str(home / ".terminfo")}
            result = CB.run_remote_install(home, root=ROOT_DIR, env=env)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((home / ".local" / "bin" / "clipcopy").is_file())
            self.assertTrue(
                (home / ".local" / "bin" / "clipboard-artifact-receive").is_file()
            )
            self.assertTrue(
                (
                    home
                    / ".local"
                    / "share"
                    / "skillbox"
                    / "python"
                    / "lib"
                    / "clipboard_transfer.py"
                ).is_file()
            )
            self.assertTrue(
                (home / ".config" / "skillbox" / "clipboard.tmux.conf").is_file()
            )
            verify = subprocess.run(
                ["infocmp", "-x", "xterm-ghostty"],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "TERMINFO": str(home / ".terminfo"),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, msg=verify.stderr)

    def test_remote_receiver_is_runnable_from_fresh_fixture_home(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            result = CB.run_remote_install(home, root=ROOT_DIR)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = b"\x89PNG\r\n\x1a\nremote-fixture"
            digest = hashlib.sha256(payload).hexdigest()
            receiver = home / ".local" / "bin" / "clipboard-artifact-receive"
            receive = subprocess.run(
                [
                    str(receiver),
                    "put",
                    "--sha256",
                    digest,
                    "--size",
                    str(len(payload)),
                    "--extension",
                    "png",
                ],
                input=payload,
                env={**os.environ, "HOME": str(home)},
                capture_output=True,
                check=False,
            )
            self.assertEqual(receive.returncode, 0, msg=receive.stderr.decode())
            self.assertIn(digest.encode(), receive.stdout)

    def test_remote_install_uninstall_restores_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            helper = home / ".local" / "bin" / "clipcopy"
            helper.parent.mkdir(parents=True)
            helper.write_bytes(b"legacy remote helper\n")
            helper.chmod(0o755)
            tmux_conf = home / ".tmux.conf"
            tmux_conf.write_bytes(b"set -g mouse on\n")
            installed = CB.run_remote_install(home, root=ROOT_DIR)
            self.assertEqual(installed.returncode, 0, msg=installed.stderr)
            state = home / CB.STATE_SUBDIR
            baseline = state / "baseline"
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (baseline / "records.tsv").stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                (baseline / "files" / "clipcopy").stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                (baseline / "files" / "tmux_conf").stat().st_mode & 0o777,
                0o600,
            )

            restored = subprocess.run(
                ["bash", "-s"],
                input=CB.remote_restore_script(),
                env={**os.environ, "HOME": str(home)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(restored.returncode, 0, msg=restored.stderr)
            self.assertEqual(helper.read_bytes(), b"legacy remote helper\n")
            self.assertEqual(tmux_conf.read_bytes(), b"set -g mouse on\n")
            self.assertFalse(
                (home / ".local" / "bin" / "clipboard-artifact-receive").exists()
            )
            self.assertFalse(home.joinpath(CB.STATE_SUBDIR).exists())

    def test_apply_remote_via_ssh_invokes_ssh_runner(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv, 0, stdout=b"skillbox clipboard bootstrap: ok\n", stderr=b""
            )

        proc = CB.apply_remote_via_ssh(
            "skillbox@example", root=ROOT_DIR, runner=fake_runner
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("ssh", calls[0])
        self.assertIn("skillbox@example", calls[0])
        self.assertTrue(
            any(arg.startswith("SKILLBOX_CLIPBOARD_BUNDLE_B64=") for arg in calls[0])
        )

    def test_apply_remote_via_ssh_wsl_transport(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=b"ok\n", stderr=b"")

        CB.apply_remote_via_ssh(
            "conference1-ssh", root=ROOT_DIR, transport="wsl", runner=fake_runner
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("wsl -d" in arg for arg in calls[0]))
        self.assertTrue(
            any("SKILLBOX_CLIPBOARD_BUNDLE_B64=" in arg for arg in calls[0])
        )

    def test_apply_remote_restore_via_ssh_uses_owned_restore_script(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(argv))
            self.assertIn(b"baseline", kwargs["input"])
            return subprocess.CompletedProcess(argv, 0, stdout=b"ok\n", stderr=b"")

        result = CB.apply_remote_restore_via_ssh("skillbox@example", runner=fake_runner)
        self.assertEqual(result.returncode, 0)
        self.assertIn("skillbox@example", calls[0])

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
            clip_path = f"{home}/.config/skillbox/clipboard.tmux.conf"
            tmux_conf.write_text(
                "\n".join(
                    [
                        "set -g mouse on",
                        "",
                        "# Skillbox clipboard integration: OSC52 across local tmux, SSH, mosh, and nested tmux.",
                        "if-shell [",
                        "-r",
                        clip_path,
                        "] source-file",
                        clip_path,
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

    def test_run_remote_install_repairs_malformed_block_preserves_following_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            tmux_conf = home / ".tmux.conf"
            clip_path = f"{home}/.config/skillbox/clipboard.tmux.conf"
            tmux_conf.write_text(
                "\n".join(
                    [
                        "set -g mouse on",
                        "# Skillbox clipboard integration: OSC52 across local tmux, SSH, mosh, and nested tmux.",
                        "if-shell [",
                        "-r",
                        clip_path,
                        "] source-file",
                        clip_path,
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

    def test_bootstrap_cli_remote_default_is_plan_mode(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                str(ROOT_DIR / "scripts" / "clipboard-bootstrap"),
                "--profile",
                "d3",
            ],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("(plan)", proc.stdout)
        self.assertNotIn("(apply)", proc.stdout)

    def test_install_local_repairs_stale_tmux_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            tmux_conf = home / ".tmux.conf"
            clip_path = f"{home}/.config/skillbox/clipboard.tmux.conf"
            tmux_conf.write_text(
                "\n".join(
                    [
                        "set -g mouse on",
                        CB.TMUX_COMMENT,
                        "if-shell [",
                        "-r",
                        clip_path,
                        "] source-file",
                        clip_path,
                    ]
                ),
                encoding="utf-8",
            )
            CB.install_local(home, dry_run=False, root=ROOT_DIR)
            content = tmux_conf.read_text(encoding="utf-8")
            self.assertIn("set -g mouse on", content)
            self.assertIn(CB.SOURCE_LINE, content)
            self.assertNotIn("if-shell [", content)
            self.assertEqual(CB.verify_local_install(home), [])

    def test_shell_syntax_clipboard_helpers(self) -> None:
        for name in ("clipcopy", "clippaste", "pbcopy", "clipimg-put"):
            path = CB.bundle_dir(ROOT_DIR) / name
            proc = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 0, msg=f"{name}: {proc.stderr}")

    def test_make_bundle_tar_contains_helpers(self) -> None:
        import io
        import tarfile

        archive = CB.make_bundle_tar(ROOT_DIR)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = {member.name for member in tar.getmembers()}
        self.assertTrue(
            {
                "clipcopy",
                "clippaste",
                "pbcopy",
                "tmux.conf",
                CB.TERMINFO_BUNDLE_NAME,
                "clipboard-artifact-receive",
                "lib/__init__.py",
                "lib/clipboard_transfer.py",
                "VERSION",
            }.issubset(names)
        )


def shutil_which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)


if __name__ == "__main__":
    unittest.main()
