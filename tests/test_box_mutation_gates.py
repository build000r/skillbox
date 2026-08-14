"""R-201: box.py CLI mutation-gate parity with the operator MCP.

Direct `box.py down <id> --yes` used to bypass all three gates the operator
MCP enforces (dry-run marker, clean tree, confirmation) — a one-call,
dirty-tree droplet destroy (pass-2 audit F-box-01/P0). These tests pin:

- real down without --yes/--confirm → confirmation_required
- real down with --yes but no marker → dryrun_marker_required
- real down from a dirty tree → dirty_tree_refused (checked before marker)
- dry-run stamps a marker the operator MCP would also honor (same path shape)
- markers expire after the TTL; SKILLBOX_CLI_MUTATION_GATE=skip warns loudly
- cmd_down / cmd_status defaults are fail-closed
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"

spec = importlib.util.spec_from_file_location("box_gate_test_module", BOX_SCRIPT)
assert spec and spec.loader
BOX = importlib.util.module_from_spec(spec)
sys.modules["box_gate_test_module"] = BOX
spec.loader.exec_module(BOX)


class MarkerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.repo_root_patch = mock.patch.object(BOX, "REPO_ROOT", Path(self.tmpdir.name))
        self.repo_root_patch.start()
        self.addCleanup(self.repo_root_patch.stop)

    def test_marker_path_matches_operator_mcp_shape(self) -> None:
        path = BOX._cli_dryrun_marker_path("operator_teardown", "demo-box")
        self.assertEqual(path.name, ".skillbox-dryrun-operator_teardown-demo-box")
        self.assertEqual(path.parent.name, "dryrun-markers")
        self.assertEqual(path.parent.parent.name, ".skillbox-state")

    def test_stamp_then_valid_then_clear(self) -> None:
        self.assertFalse(BOX.cli_dryrun_marker_valid("operator_teardown", "demo-box"))
        BOX.stamp_cli_dryrun_marker("operator_teardown", "demo-box")
        self.assertTrue(BOX.cli_dryrun_marker_valid("operator_teardown", "demo-box"))
        payload = json.loads(
            BOX._cli_dryrun_marker_path("operator_teardown", "demo-box").read_text()
        )
        self.assertEqual(payload["tool"], "operator_teardown")
        self.assertEqual(payload["source"], "box-cli")
        BOX.clear_cli_dryrun_marker("operator_teardown", "demo-box")
        self.assertFalse(BOX.cli_dryrun_marker_valid("operator_teardown", "demo-box"))

    def test_marker_expires_after_ttl(self) -> None:
        BOX.stamp_cli_dryrun_marker("operator_teardown", "demo-box")
        marker = BOX._cli_dryrun_marker_path("operator_teardown", "demo-box")
        stale = time.time() - (BOX.CLI_DRYRUN_MARKER_TTL_SECONDS + 5)
        os.utime(marker, (stale, stale))
        self.assertFalse(BOX.cli_dryrun_marker_valid("operator_teardown", "demo-box"))


class MutationGateTests(unittest.TestCase):
    def _gate(self, **kwargs):
        payloads: list[dict[str, object]] = []
        with mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cli_mutation_gate(
                "operator_teardown", "demo-box", fmt="json",
                command_hint="python3 scripts/box.py down demo-box --dry-run --format json",
                **kwargs,
            )
        return result, payloads

    def test_dirty_tree_refused_before_marker_check(self) -> None:
        with mock.patch.object(BOX, "_repo_tree_dirty", return_value="2 uncommitted path(s)"), \
             mock.patch.object(BOX, "cli_dryrun_marker_valid") as marker_probe:
            result, payloads = self._gate()
        self.assertIsNotNone(result)
        self.assertEqual(payloads[-1]["error"]["type"], "dirty_tree_refused")
        marker_probe.assert_not_called()

    def test_marker_required_when_tree_clean(self) -> None:
        with mock.patch.object(BOX, "_repo_tree_dirty", return_value=""), \
             mock.patch.object(BOX, "cli_dryrun_marker_valid", return_value=False):
            result, payloads = self._gate()
        self.assertIsNotNone(result)
        self.assertEqual(payloads[-1]["error"]["type"], "dryrun_marker_required")
        self.assertIn("--dry-run", " ".join(payloads[-1]["next_actions"]))

    def test_clean_tree_with_marker_passes(self) -> None:
        with mock.patch.object(BOX, "_repo_tree_dirty", return_value=""), \
             mock.patch.object(BOX, "cli_dryrun_marker_valid", return_value=True):
            result, payloads = self._gate()
        self.assertIsNone(result)
        self.assertEqual(payloads, [])

    def test_env_skip_warns_and_passes(self) -> None:
        with mock.patch.dict(os.environ, {"SKILLBOX_CLI_MUTATION_GATE": "skip"}), \
             mock.patch.object(BOX, "_repo_tree_dirty") as dirty_probe:
            result, payloads = self._gate()
        self.assertIsNone(result)
        dirty_probe.assert_not_called()


class ContractDriftTests(unittest.TestCase):
    """R-211: every registered subparser must be declared in BOX_COMMAND_NAMES.

    posture-proof was a ghost surface — a working subcommand absent from the
    contract, so capabilities/robot-docs never advertised it and the --json
    argv alias was rejected for it (F-box-03).
    """

    def test_parser_and_contract_agree(self) -> None:
        import re
        import subprocess

        help_text = subprocess.run(
            [sys.executable, str(BOX_SCRIPT), "--help"],
            capture_output=True, text=True, check=False,
        ).stdout
        match = re.search(r"\{([a-z0-9,\-]+)\}", help_text)
        assert match, help_text[:300]
        registered = set(match.group(1).split(","))
        self.assertEqual(
            registered, BOX.BOX_COMMAND_NAMES,
            "argparse subcommands and BOX_COMMAND_NAMES drifted — a new verb "
            "must be added to the machine contract (capabilities/robot-docs).",
        )

    def test_status_no_probe_returns_inventory_state(self) -> None:
        box = BOX.Box(id="fast", profile="dev-small", state="ready")
        status = BOX.box_health(box, probe=False)
        self.assertTrue(status["probes_skipped"])
        self.assertFalse(status["ssh_reachable"])


class UpgradeMarkerKeyTests(unittest.TestCase):
    def test_key_binds_box_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_a = Path(tmpdir) / "a.json"
            manifest_b = Path(tmpdir) / "b.json"
            manifest_a.write_text('{"v":1}')
            manifest_b.write_text('{"v":2}')
            key_a = BOX._upgrade_marker_key("demo", str(manifest_a))
            self.assertEqual(key_a, BOX._upgrade_marker_key("demo", str(manifest_a)))
            # Different manifest, different box, or edited content → new key.
            self.assertNotEqual(key_a, BOX._upgrade_marker_key("demo", str(manifest_b)))
            self.assertNotEqual(key_a, BOX._upgrade_marker_key("other", str(manifest_a)))
            manifest_a.write_text('{"v":1,"edited":true}')
            self.assertNotEqual(key_a, BOX._upgrade_marker_key("demo", str(manifest_a)))


class DispatchGateWiringTests(unittest.TestCase):
    """Drive the gates through real argv (fresh-eyes P2: the unit tests alone
    didn't pin main()'s wiring)."""

    def _env(self, tmpdir: str) -> dict[str, str]:
        state_root = Path(tmpdir) / ".skillbox-state"
        state_root.mkdir(parents=True, exist_ok=True)
        inv = state_root / "inventory" / "boxes.json"
        inv.parent.mkdir(parents=True, exist_ok=True)
        inv.write_text(json.dumps({"boxes": [
            {"id": "gatebox", "profile": "dev-small", "state": "ready",
             "droplet_id": "1", "droplet_ip": "10.0.0.9",
             "tailscale_hostname": "skillbox-gatebox", "tailscale_ip": "100.100.0.9",
             "ssh_user": "skillbox", "created_at": "", "updated_at": "",
             "region": "nyc3", "size": "s-2vcpu-4gb"},
        ]}))
        return {
            **os.environ,
            "SKILLBOX_BOX_INVENTORY": str(inv),
            "SKILLBOX_STATE_ROOT": str(state_root),
            "SKILLBOX_DRYRUN_MARKER_ROOT": str(state_root),
            "SKILLBOX_DO_TOKEN": "",
        }

    def _run(self, env: dict[str, str], *args: str):
        import subprocess

        return subprocess.run(
            [sys.executable, str(BOX_SCRIPT), *args],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_real_down_via_argv_is_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env(tmpdir)
            result = self._run(env, "down", "gatebox", "--yes", "--format", "json")
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            # Either gate refusing proves the dispatch wiring fires; which one
            # depends on the repo's tree state when the suite runs.
            self.assertIn(payload["error"]["type"], ("dirty_tree_refused", "dryrun_marker_required"))

    def test_dry_run_down_via_argv_stamps_the_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env(tmpdir)
            result = self._run(env, "down", "gatebox", "--dry-run", "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            marker = (Path(tmpdir) / ".skillbox-state" / "dryrun-markers"
                      / ".skillbox-dryrun-operator_teardown-gatebox")
            self.assertTrue(marker.exists(), "dry-run did not stamp the teardown marker")

    def test_real_up_via_argv_is_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._env(tmpdir)
            result = self._run(env, "up", "newbox", "--profile", "dev-small", "--format", "json")
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertIn(payload["error"]["type"], ("dirty_tree_refused", "dryrun_marker_required"))


class FailClosedDefaultTests(unittest.TestCase):
    def test_cmd_down_default_is_unconfirmed(self) -> None:
        self.assertIs(inspect.signature(BOX.cmd_down).parameters["confirmed"].default, False)

    def test_cmd_status_default_does_not_write_cache(self) -> None:
        self.assertIs(inspect.signature(BOX.cmd_status).parameters["write_cache"].default, False)

    def test_real_down_without_confirmation_is_refused(self) -> None:
        payloads: list[dict[str, object]] = []
        with mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            result = BOX.cmd_down("demo-box", dry_run=False, fmt="json")
        self.assertNotEqual(result, BOX.EXIT_OK)
        # NOTE: uses the flat structured_cli_error envelope; the error.type
        # envelope unification is tracked separately (F-box-05).
        self.assertEqual(payloads[-1]["error_code"], "confirmation_required")


if __name__ == "__main__":
    unittest.main()
