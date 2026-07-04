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

from lib import opslib  # noqa: E402


class OpslibValidatorTests(unittest.TestCase):
    def test_identifier_validators_accept_shared_safe_grammar(self) -> None:
        cases = [
            (opslib.validate_box_id, "Box.1_alpha-2", "Box.1_alpha-2"),
            (opslib.validate_profile_name, " dev-small ", "dev-small"),
            (lambda value: opslib.validate_identifier(value, "tool_name"), "operator_test", "operator_test"),
            (opslib.validate_ssh_user, "skillbox_1", "skillbox_1"),
            (opslib.validate_ssh_user, "_svc", "_svc"),
            (opslib.validate_host, "skillbox-alpha.tailnet", "skillbox-alpha.tailnet"),
            (opslib.validate_host, "100.64.0.1", "100.64.0.1"),
        ]
        for validator, raw_value, expected in cases:
            with self.subTest(raw_value=raw_value):
                self.assertEqual(validator(raw_value), expected)

    def test_identifier_validators_reject_unsafe_values(self) -> None:
        cases = [
            (opslib.validate_box_id, ""),
            (opslib.validate_box_id, "-bad"),
            (opslib.validate_box_id, "bad/name"),
            (opslib.validate_box_id, "a" * 65),
            (opslib.validate_profile_name, "bad\\name"),
            (opslib.validate_profile_name, "bad name"),
            (opslib.validate_ssh_user, "1bad"),
            (opslib.validate_ssh_user, "a" * 33),
            (opslib.validate_host, "-bad"),
            (opslib.validate_host, "bad-"),
            (opslib.validate_host, "bad/name"),
        ]
        for validator, raw_value in cases:
            with self.subTest(raw_value=raw_value):
                with self.assertRaises(ValueError):
                    validator(raw_value)

    def test_resolve_inventory_path_defaults_to_repo_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    opslib.resolve_inventory_path(repo_root=repo),
                    repo / "workspace" / "boxes.json",
                )

    def test_resolve_inventory_path_allows_repo_and_state_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            state = root / "state"
            repo.mkdir()
            state.mkdir()
            repo_inventory = repo / "workspace" / "boxes.json"
            state_inventory = state / "boxes.json"

            with mock.patch.dict(
                os.environ,
                {"SKILLBOX_BOX_INVENTORY": str(repo_inventory), "SKILLBOX_STATE_ROOT": str(state)},
                clear=True,
            ):
                self.assertEqual(opslib.resolve_inventory_path(repo_root=repo), repo_inventory)

            with mock.patch.dict(
                os.environ,
                {"SKILLBOX_BOX_INVENTORY": str(state_inventory), "SKILLBOX_STATE_ROOT": str(state)},
                clear=True,
            ):
                self.assertEqual(opslib.resolve_inventory_path(repo_root=repo), state_inventory)

    def test_resolve_inventory_path_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            state = root / "state"
            outside = root / "outside" / "boxes.json"
            repo.mkdir()
            state.mkdir()
            outside.parent.mkdir()
            with mock.patch.dict(
                os.environ,
                {"SKILLBOX_BOX_INVENTORY": str(outside), "SKILLBOX_STATE_ROOT": str(state)},
                clear=True,
            ):
                with self.assertRaises(opslib.InventoryPathError) as ctx:
                    opslib.resolve_inventory_path(repo_root=repo)
        self.assertEqual(ctx.exception.error_code, opslib.INVENTORY_PATH_INVALID)
        self.assertIn(opslib.INVENTORY_PATH_INVALID, str(ctx.exception))

    def test_run_checked_returns_redacted_structured_result(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tool"],
            7,
            stdout="ok SKILLBOX_DO_TOKEN=do-secret",
            stderr="denied Authorization: Bearer bearer-secret",
        )
        with mock.patch.object(opslib.subprocess, "run", return_value=completed):
            result = opslib.run_checked(["tool"], timeout=5)

        self.assertEqual(result["rc"], 7)
        self.assertIn("ok", result["stdout"])
        self.assertIn("[REDACTED]", result["stdout"])
        self.assertIn("[REDACTED]", result["stderr_redacted"])
        self.assertNotIn("do-secret", result["stdout"])
        self.assertNotIn("bearer-secret", result["stderr_redacted"])
        self.assertIsInstance(result["elapsed"], float)


if __name__ == "__main__":
    unittest.main()
