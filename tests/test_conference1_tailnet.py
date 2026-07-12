from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import conference1_tailnet as CT  # noqa: E402


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Conference1MetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = CT.load_conference1_tailnet(ROOT_DIR)

    def test_metadata_core_facts(self) -> None:
        self.assertEqual(self.meta["magicdns"], "conference1.tail4c481e.ts.net")
        self.assertEqual(self.meta["tailscale_ip"], "100.123.217.11")
        self.assertEqual(self.meta["windows_ssh_target"], "conference1-ssh")
        self.assertEqual(self.meta["wsl_ssh_target"], "worker@conference1-wsl")
        self.assertEqual(sorted(self.meta["serve_ports"]), [3000, 3001, 3170, 3210, 8050])

    def test_swimmers_remote_rust_lane(self) -> None:
        rust = self.meta["swimmers_remote_rust"]
        self.assertEqual(rust["host_env"], "SWIMMERS_REMOTE_RUST_HOST")
        self.assertIn("conference1-ssh", rust["host_values"])
        self.assertEqual(rust["build_side"], "wsl-ubuntu")
        self.assertEqual(rust["cargo_cache"], "/var/tmp/swimmers-remote-rust-cache")

    def test_security_rules_documented(self) -> None:
        security = self.meta["security"]
        self.assertEqual(security["posture"], "tailnet-only")
        rules = " ".join(security["rules"])
        self.assertIn("0.0.0.0", rules)
        self.assertIn("Funnel", rules)

    def test_urls_distinguish_magicdns_from_portproxy(self) -> None:
        magic = CT.magicdns_serve_urls(self.meta)
        proxy = CT.portproxy_fallback_urls(self.meta)
        self.assertIn("http://conference1.tail4c481e.ts.net:3000", magic)
        self.assertIn("http://100.123.217.11:3000", proxy)
        self.assertFalse(set(magic) & set(proxy))
        text = CT.render_urls_text(self.meta)
        self.assertIn("MagicDNS Serve URLs (primary", text)
        self.assertIn("Raw-IP portproxy fallback", text)

    def test_rendered_metadata_has_no_secret_shapes(self) -> None:
        for text in (
            CT.render_urls_text(self.meta),
            CT.render_helper_text(self.meta),
        ):
            self.assertEqual(CT.find_secret_leaks(text), [], msg=text[:200])
            CT.assert_no_secrets(text)

    def test_secret_guard_trips_on_synthetic_secret(self) -> None:
        with self.assertRaises(CT.SecretLeakError):
            CT.assert_no_secrets("serve url tskey-auth-k1234567CNTRL-abc")
        self.assertIn("[REDACTED]", CT.redact_secrets("x tskey-auth-k1234567 y"))


class Conference1CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = CT.load_conference1_tailnet(ROOT_DIR)

    def test_serve_list_argv_is_batchmode_readonly(self) -> None:
        argv = CT.serve_list_argv(self.meta)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("conference1-ssh", argv)
        self.assertIn("tailnet-serve.ps1 list", argv[-1])

    def test_portproxy_list_argv(self) -> None:
        argv = CT.portproxy_list_argv(self.meta)
        self.assertIn("netsh interface portproxy show v4tov4", argv[-1])

    def test_mutating_actions_require_port(self) -> None:
        with self.assertRaises(ValueError):
            CT.serve_helper_remote_command(self.meta, "expose")
        with self.assertRaises(ValueError):
            CT.serve_helper_remote_command(self.meta, "bogus")

    def test_funnel_refused_everywhere(self) -> None:
        with self.assertRaises(CT.FunnelRefusedError):
            CT.refuse_funnel(["funnel"])
        with self.assertRaises(CT.FunnelRefusedError):
            CT.serve_helper_remote_command(self.meta, "Funnel")
        with self.assertRaises(CT.FunnelRefusedError):
            CT.main(["expose", "80", "--funnel"])

    def test_status_labels_primary_and_fallback(self) -> None:
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> FakeProc:
            calls.append(argv)
            return FakeProc(stdout="live-line")

        payload = CT.status_payload(self.meta, runner=runner)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["magicdns_serve"]["kind"], "primary")
        self.assertEqual(payload["portproxy_fallback"]["kind"], "fallback")
        self.assertEqual(len(calls), 2)
        text = CT.render_status_text(payload)
        self.assertIn("MagicDNS Serve URLs — primary", text)
        self.assertIn("portproxy — fallback only", text)

    def test_status_unreachable_error_path(self) -> None:
        payload = CT.status_payload(
            self.meta,
            runner=lambda argv: FakeProc(returncode=255, stderr="Permission denied (publickey)"),
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "CONFERENCE1_SSH_UNREACHABLE")
        self.assertTrue(payload["error"]["next_actions"])

    def test_status_redacts_secret_shaped_live_output(self) -> None:
        payload = CT.status_payload(
            self.meta,
            runner=lambda argv: FakeProc(stdout="ok tskey-auth-kSECRET123"),
        )
        self.assertNotIn("kSECRET123", payload["magicdns_serve"]["live"]["stdout"])
        self.assertIn("[REDACTED]", payload["magicdns_serve"]["live"]["stdout"])

    def test_expose_without_yes_never_executes(self) -> None:
        runner = mock.Mock()
        result = CT.mutate_serve(self.meta, "expose", 3999, yes=False, runner=runner)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["executed"])
        self.assertIn("expose 3999", result["command"])
        runner.assert_not_called()

    def test_main_urls_default_and_json(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = CT.main(["--root", str(ROOT_DIR)])
        self.assertEqual(rc, 0)
        self.assertIn("MagicDNS Serve URLs", out.getvalue())

        out = io.StringIO()
        with redirect_stdout(out):
            rc = CT.main(["urls", "--format", "json", "--root", str(ROOT_DIR)])
        self.assertEqual(rc, 0)
        import json

        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["magicdns_serve"]["kind"], "primary")
        self.assertEqual(payload["portproxy_fallback"]["kind"], "fallback")
        self.assertEqual(CT.find_secret_leaks(out.getvalue()), [])


if __name__ == "__main__":
    unittest.main()
