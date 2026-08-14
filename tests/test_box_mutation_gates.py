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
