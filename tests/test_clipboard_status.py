from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.lib import clipboard_bootstrap
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

    def test_listener_collection_failure_is_a_safe_empty_observation(self) -> None:
        def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("lsof")

        self.assertEqual(status.collect_clipboard_listeners(runner=runner), [])

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

    def test_installed_fixture_has_ready_redacted_status_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            home.mkdir()
            clipboard_bootstrap.install_local(home, root=ROOT_DIR)
            report = status.inspect_status(
                home=home, root=ROOT_DIR, profile="d3", now=1000.0
            )
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["state"], "ready")
            self.assertTrue(report["install"]["ready"])
            self.assertIn("clipboard bytes", report["redaction"])
            self.assertNotIn("clipboard_text", status.dump(report))

    def test_unsupported_profile_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            home.mkdir()
            clipboard_bootstrap.install_local(home, root=ROOT_DIR)
            report = status.inspect_status(
                home=home, root=ROOT_DIR, profile="conference1-fallback", now=1000.0
            )
            self.assertEqual(report["state"], "unsupported")


if __name__ == "__main__":
    unittest.main()
