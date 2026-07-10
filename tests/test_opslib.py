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

    def test_classify_ssh_failure_uses_transport_error_table(self) -> None:
        retryable_cases = [
            (255, "ssh: connect to host box port 22: Connection timed out"),
            (255, "ssh: connect to host box port 22: Connection refused"),
            (255, "client_loop: send disconnect: Connection reset"),
            (255, "kex_exchange_identification: banner line contains invalid characters"),
        ]
        for rc, stderr in retryable_cases:
            with self.subTest(stderr=stderr):
                result = opslib.classify_ssh_failure({"rc": rc, "stderr_redacted": stderr})
                self.assertEqual(result["failure_class"], "ssh_transport")
                self.assertTrue(result["retryable"])

        non_retryable_cases = [
            (255, "Permission denied (publickey)."),
            (1, "remote command failed"),
            (127, "bash: missing-command: command not found"),
        ]
        for rc, stderr in non_retryable_cases:
            with self.subTest(stderr=stderr):
                result = opslib.classify_ssh_failure({"rc": rc, "stderr_redacted": stderr})
                self.assertEqual(result["failure_class"], "remote_command")
                self.assertFalse(result["retryable"])

    def test_run_checked_retry_respects_total_deadline(self) -> None:
        calls: list[float] = []
        sleeps: list[float] = []
        now = [0.0]

        def fake_run(cmd, **kwargs):
            calls.append(float(kwargs["timeout"]))
            return subprocess.CompletedProcess(
                cmd,
                255,
                stdout="",
                stderr="kex_exchange_identification: Connection closed by remote host",
            )

        def monotonic() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        policy = opslib.RetryPolicy(
            attempts=3,
            backoff_seconds=(1.0, 2.0, 4.0),
            jitter_seconds=0.0,
            total_deadline=1.5,
        )
        with mock.patch.object(opslib.subprocess, "run", side_effect=fake_run):
            result = opslib.run_checked(
                ["ssh", "box"],
                timeout=10,
                retry_policy=policy,
                retry_classifier=opslib.classify_ssh_failure,
                sleep=sleep,
                monotonic=monotonic,
            )

        self.assertEqual(result["attempts"], 2)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(calls, [1.5, 0.5])
        self.assertTrue(result["deadline_exhausted"])
        self.assertTrue(result["retryable_hint"])
        self.assertEqual(result["failure_class"], "ssh_transport")


if __name__ == "__main__":
    unittest.main()
