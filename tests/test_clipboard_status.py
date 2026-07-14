from __future__ import annotations

import json
import subprocess
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.lib import clipboard_bootstrap
from scripts.lib import clipboard_session
from scripts.lib import clipboard_status as status


ROOT_DIR = Path(__file__).resolve().parents[1]


class ClipboardStatusTests(unittest.TestCase):
    def test_live_listener_collection_is_scoped_and_redacted(self) -> None:
        output = """COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
Xvfb 10 user 6u IPv4 0 0t0 TCP *:6000 (LISTEN)
cc-clip 11 user 7u IPv6 0 0t0 TCP [::1]:18339 (LISTEN)
python 12 user 8u IPv4 0 0t0 TCP *:9999 (LISTEN)
"""

        def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, output, "")

        self.assertEqual(
            status.collect_clipboard_listeners(runner=runner),
            [
                {"process": "Xvfb", "address": "0.0.0.0", "port": 6000},
                {"process": "cc-clip", "address": "::1", "port": 18339},
            ],
        )

    def test_listener_collection_failure_cannot_claim_safe_observation(self) -> None:
        def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("lsof")

        with self.assertRaises(status.ListenerProbeError):
            status.collect_clipboard_listeners(runner=runner)
        checks = status.diagnose_facts(
            {"listeners": [], "listener_probe_error": "unavailable"}
        )
        containment = next(
            check for check in checks if check["id"] == "network.containment"
        )
        self.assertEqual(containment["status"], "fail")
        self.assertIn("lsof", containment["repair"])

    def test_state_vocabulary_is_deterministic(self) -> None:
        self.assertEqual(status.classify_state(), "ready")
        self.assertEqual(status.classify_state(install_issues=True), "degraded")
        self.assertEqual(
            status.classify_state(installed=True, install_issues=True), "configured"
        )
        self.assertEqual(status.classify_state(stale=True), "stale")
        self.assertEqual(status.classify_state(unsupported=True), "unsupported")
        self.assertEqual(status.classify_state(ambiguous=True), "ambiguous")
        self.assertEqual(status.classify_state(offline=True), "offline")

    def test_explicit_target_probe_is_minimal_redacted_and_reports_offline(self) -> None:
        observed: list[str] = []
        secret = "/home/user/private/session"

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            observed.extend(command)
            return subprocess.CompletedProcess(
                command, 255, "", f"connection failed near {secret}"
            )

        result = status.probe_ssh_target("skillbox@example", runner=runner)
        self.assertEqual(
            observed,
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=3",
                "skillbox@example",
                "true",
            ],
        )
        self.assertEqual(result["reachable"], False)
        self.assertEqual(result["error"], "offline_or_unreachable")
        self.assertNotIn(secret, status.dump(result))

    def test_target_probe_rejects_option_injection_without_spawning_ssh(self) -> None:
        called = False

        def runner(*_args: object, **_kwargs: object) -> None:
            nonlocal called
            called = True

        result = status.probe_ssh_target("-oProxyCommand=id", runner=runner)
        self.assertEqual(result["error"], "invalid_target")
        self.assertFalse(called)

    def test_ready_route_with_failed_explicit_probe_reports_offline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            home.mkdir()
            clipboard_bootstrap.install_local(home, root=ROOT_DIR)
            _, route_path = clipboard_session.register(
                profile="d3",
                transport="ssh",
                terminal_id="ghostty-offline-fixture",
                root=home / ".local" / "state" / "skillbox" / "paste-routes",
                hosts_path=ROOT_DIR / "scripts" / "clipboard" / "hosts.json",
                now=900.0,
                ttl_seconds=1_000,
                stamp_tmux=False,
            )
            report = status.inspect_status(
                home=home,
                root=ROOT_DIR,
                profile="d3",
                route_path=route_path,
                now=1000.0,
                environment={},
                probe_target_live=True,
                codex_version_fn=lambda: "codex-cli 0.144.4",
                target_runner=lambda command, **_kwargs: subprocess.CompletedProcess(
                    command, 255, "", "connection timed out"
                ),
                listener_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                    [], 1, "", ""
                ),
            )
            self.assertEqual(report["state"], "offline")
            self.assertEqual(report["target_probe"]["reachable"], False)

    def test_doctor_detects_containment_modes_stale_route_and_duplicate_tmux(
        self,
    ) -> None:
        checks = status.diagnose_facts(
            {
                "listeners": [{"address": "0.0.0.0", "port": 6000}],
                "private_files": [{"path": "/tmp/token", "mode": 0o644}],
                "route_stale": True,
                "duplicate_tmux_features": 2,
                "legacy_bridge": {
                    "token_stale": True,
                    "display_missing": True,
                    "sidecar_dead": True,
                    "dependency_outdated": True,
                },
            }
        )
        failures = {item["id"] for item in checks if item["status"] == "fail"}
        self.assertTrue(
            {
                "network.containment",
                "files.private_modes",
                "route.freshness",
                "tmux.duplicate_features",
                "legacy_bridge.token_stale",
                "legacy_bridge.display_missing",
                "legacy_bridge.sidecar_dead",
                "legacy_bridge.dependency_outdated",
            }.issubset(failures)
        )
        self.assertTrue(
            all(item["repair"] for item in checks if item["status"] == "fail")
        )

    def test_private_state_walk_rejects_symlinks_and_unsafe_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            receipts = home / ".cache" / "skillbox" / "smart-paste" / "receipts"
            receipts.mkdir(parents=True)
            receipts.chmod(0o755)
            outside = Path(raw) / "outside.json"
            outside.write_text("secret", encoding="utf-8")
            (receipts / "linked.json").symlink_to(outside)

            facts = status._private_file_facts(home)  # noqa: SLF001
            by_kind = {item["kind"] for item in facts}
            self.assertIn("directory", by_kind)
            self.assertIn("symlink", by_kind)
            receipt_fact = next(item for item in facts if item["path"] == str(receipts))
            self.assertEqual(receipt_fact["mode"], 0o755)
            checks = status.diagnose_facts({"private_paths": facts})
            private = next(
                check for check in checks if check["id"] == "files.private_modes"
            )
            self.assertEqual(private["status"], "fail")
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)

    def test_installed_fixture_without_exact_route_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            home.mkdir()
            clipboard_bootstrap.install_local(home, root=ROOT_DIR)
            report = status.inspect_status(
                home=home,
                root=ROOT_DIR,
                profile="d3",
                now=1000.0,
                listener_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                    [], 1, "", ""
                ),
            )
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["state"], "ambiguous")
            self.assertTrue(report["install"]["ready"])
            self.assertIn("clipboard bytes", report["redaction"])
            self.assertNotIn("clipboard_text", status.dump(report))

    def test_status_redacts_paths_target_session_and_receipt_location(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "private-operator-home"
            home.mkdir()
            clipboard_bootstrap.install_local(home, root=ROOT_DIR)
            record, route_path = clipboard_session.register(
                profile="d3",
                transport="ssh",
                terminal_id="ghostty-redaction-fixture",
                remote_session="private-session-name",
                root=home / ".local" / "state" / "skillbox" / "paste-routes",
                hosts_path=ROOT_DIR / "scripts" / "clipboard" / "hosts.json",
                now=900.0,
                ttl_seconds=1_000,
                stamp_tmux=False,
            )
            receipts = home / ".cache" / "skillbox" / "smart-paste" / "receipts"
            receipts.mkdir(parents=True)
            (receipts / "gesture-safe-id.json").write_text("{}", encoding="utf-8")

            report = status.inspect_status(
                home=home,
                root=ROOT_DIR,
                profile="d3",
                route_path=route_path,
                now=1000.0,
                environment={},
                codex_version_fn=lambda: "codex-cli 0.144.4",
                listener_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                    [], 1, "", ""
                ),
            )
            public = status.dump(report)
            self.assertEqual(report["target"], "configured")
            self.assertEqual(report["route"]["remote_session"], "registered")
            self.assertEqual(report["last_receipt"], "gesture-safe-id.json")
            self.assertEqual(report["route"]["route_id"], record["route_id"])
            self.assertNotIn(str(home), public)
            self.assertNotIn("skillbox-portfolio-devbox", public)
            self.assertNotIn("private-session-name", public)

    def test_malformed_manifest_is_a_typed_degraded_check_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            home.mkdir()
            clipboard_bootstrap.install_local(home, root=ROOT_DIR)
            manifest = clipboard_bootstrap.lifecycle_state_dir(home) / "manifest.json"
            manifest.write_text("{not-json", encoding="utf-8")

            report = status.inspect_status(
                home=home,
                root=ROOT_DIR,
                profile="d3",
                now=1000.0,
                environment={},
                listener_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                    [], 1, "", ""
                ),
            )
            self.assertEqual(report["state"], "configured")
            self.assertFalse(report["install"]["ready"])
            manifest_check = next(
                item for item in report["checks"] if item["id"] == "lifecycle.manifest"
            )
            self.assertEqual(manifest_check["status"], "fail")
            json.loads(status.dump(report))

    def test_current_tmux_pane_auto_resolves_exact_route_to_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            home.mkdir()
            clipboard_bootstrap.install_local(home, root=ROOT_DIR)
            record, route_path = clipboard_session.register(
                profile="d3",
                transport="ssh",
                tmux_pane="%42",
                tmux_client="/dev/ttys042",
                tmux_server="/tmp/tmux-fixture",
                root=home / ".local" / "state" / "skillbox" / "paste-routes",
                hosts_path=ROOT_DIR / "scripts" / "clipboard" / "hosts.json",
                now=900.0,
                ttl_seconds=1_000,
                stamp_tmux=False,
            )

            def tmux_runner(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                fmt = command[-1]
                values = {
                    "#{client_name}": "/dev/ttys042",
                    clipboard_session.TMUX_ROUTE_OPTION: str(route_path),
                    clipboard_session.TMUX_GENERATION_OPTION: str(
                        record["generation"]
                    ),
                }
                return subprocess.CompletedProcess(command, 0, values[fmt] + "\n", "")

            report = status.inspect_status(
                home=home,
                root=ROOT_DIR,
                profile="d3",
                now=1000.0,
                environment={"TMUX": "/tmp/tmux-fixture,1,0", "TMUX_PANE": "%42"},
                tmux_runner=tmux_runner,
                codex_version_fn=lambda: "codex-cli 0.144.4",
                listener_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                    [], 1, "", ""
                ),
            )
            self.assertEqual(report["state"], "ready")
            self.assertTrue(report["route"]["ready"])
            self.assertEqual(report["route"]["route_id"], record["route_id"])

    def test_exact_route_with_old_codex_is_degraded_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            home.mkdir()
            clipboard_bootstrap.install_local(home, root=ROOT_DIR)
            _, route_path = clipboard_session.register(
                profile="d3",
                transport="ssh",
                terminal_id="ghostty-old-codex",
                root=home / ".local" / "state" / "skillbox" / "paste-routes",
                hosts_path=ROOT_DIR / "scripts" / "clipboard" / "hosts.json",
                now=900.0,
                ttl_seconds=1_000,
                stamp_tmux=False,
            )
            report = status.inspect_status(
                home=home,
                root=ROOT_DIR,
                profile="d3",
                route_path=route_path,
                now=1000.0,
                environment={},
                codex_version_fn=lambda: "codex-cli 0.143.9",
                listener_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                    [], 1, "", ""
                ),
            )
            self.assertEqual(report["state"], "degraded")
            adapter_report = report["agent"]["adapter"]
            self.assertEqual(adapter_report["strategy"], "text_reference")
            check = next(
                item
                for item in report["checks"]
                if item["id"] == "agent.codex_attachment"
            )
            self.assertEqual(check["status"], "fail")

    def test_unsupported_profile_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            home.mkdir()
            clipboard_bootstrap.install_local(home, root=ROOT_DIR)
            report = status.inspect_status(
                home=home,
                root=ROOT_DIR,
                profile="conference1-fallback",
                now=1000.0,
                listener_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                    [], 1, "", ""
                ),
            )
            self.assertEqual(report["state"], "unsupported")


if __name__ == "__main__":
    unittest.main()
