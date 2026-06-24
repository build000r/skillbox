"""Tests for repo-local .skillbox/skill-overrides.yaml inputs."""

from __future__ import annotations

import fcntl
import multiprocessing as mp
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import policy_eval as POLICY_EVAL  # noqa: E402
from runtime_manager.errors import OVERRIDE_PARSE_ERROR  # noqa: E402
from runtime_manager.policy_eval import (  # noqa: E402
    OverrideWriteLockTimeout,
    _repo_override_policy,
    update_repo_override_policy,
)
from tests.fixture_fleet import build_fixture_fleet  # noqa: E402


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _append_pin(policy: dict[str, object], name: str) -> dict[str, object]:
    pins = list(policy.get("pin_on") or [])
    if name not in pins:
        pins.append(name)
    policy["pin_on"] = pins
    return policy


def _override_writer_worker(repo_path: str, name: str) -> None:
    update_repo_override_policy(Path(repo_path), lambda policy: _append_pin(policy, name))


class RepoSkillOverridePolicyTests(unittest.TestCase):
    def test_reads_fixture_repo_override_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fleet = build_fixture_fleet(tmpdir)

            policy = _repo_override_policy(fleet.repo("healthy"))

        self.assertTrue(policy["ok"], policy["errors"])
        self.assertEqual(policy["pin_on"], ["needs-beads"])
        self.assertEqual(policy["pin_off"], ["tiny-marketing"])
        self.assertEqual(policy["opt_out_global"], ["project-status-mmdx"])
        self.assertEqual(policy["overlays"]["enable"], ["marketing"])
        self.assertEqual(policy["overlays"]["disable"], ["swarm"])
        self.assertEqual(policy["defaults"], ["tiny-ui"])
        self.assertEqual(policy["reason"], "fixture override")
        self.assertTrue(policy["_policy_path"].endswith(".skillbox/skill-overrides.yaml"))

    def test_subdir_invocation_resolves_to_git_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\npin_on: [wiki]\n",
                encoding="utf-8",
            )
            subdir = repo / "src" / "pkg"
            subdir.mkdir(parents=True)

            policy = _repo_override_policy(subdir)

        self.assertEqual(policy["pin_on"], ["wiki"])
        self.assertEqual(policy["_repo_root"], str(repo))

    def test_unknown_top_level_key_is_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\nsurprise: true\n",
                encoding="utf-8",
            )

            policy = _repo_override_policy(repo)

        self.assertFalse(policy["ok"])
        self.assertEqual(policy["pin_on"], [])
        self.assertEqual(policy["errors"][0]["code"], OVERRIDE_PARSE_ERROR)
        self.assertEqual(policy["errors"][0]["key"], "surprise")
        self.assertIn("surprise", policy["errors"][0]["message"])

    def test_wrong_overlay_shape_is_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\noverlays: []\n",
                encoding="utf-8",
            )

            policy = _repo_override_policy(repo)

        self.assertFalse(policy["ok"])
        self.assertEqual(policy["errors"][0]["key"], "overlays")
        self.assertIn("mapping", policy["errors"][0]["message"])

    def test_malformed_yaml_fails_safe_with_parse_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\npin_on: [unterminated\n",
                encoding="utf-8",
            )

            policy = _repo_override_policy(repo)

        self.assertFalse(policy["ok"])
        self.assertEqual(policy["pin_on"], [])
        self.assertEqual(policy["overlays"], {"enable": [], "disable": []})
        self.assertEqual(policy["errors"][0]["code"], OVERRIDE_PARSE_ERROR)
        self.assertIn("Failed to parse", policy["errors"][0]["message"])

    def test_missing_override_file_is_empty_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))

            policy = _repo_override_policy(repo)

        self.assertTrue(policy["ok"], policy["errors"])
        self.assertEqual(policy["pin_on"], [])
        self.assertEqual(policy["pin_off"], [])
        self.assertEqual(policy["defaults"], [])

    def test_override_writer_preserves_sequential_read_modify_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))

            first = update_repo_override_policy(repo, lambda policy: _append_pin(policy, "alpha"))
            second = update_repo_override_policy(repo, lambda policy: _append_pin(policy, "beta"))
            policy = _repo_override_policy(repo)

        self.assertTrue(first["changed"])
        self.assertTrue(second["changed"])
        self.assertEqual(policy["pin_on"], ["alpha", "beta"])

    def test_override_writer_serializes_concurrent_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            ctx = mp.get_context("spawn")
            names = ["alpha", "beta", "gamma"]
            procs = [
                ctx.Process(target=_override_writer_worker, args=(str(repo), name))
                for name in names
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=10)
                self.assertIsNotNone(proc.exitcode, "writer process hung")
                self.assertEqual(proc.exitcode, 0, "writer process failed")

            policy = _repo_override_policy(repo)

        self.assertEqual(policy["pin_on"], names)

    def test_override_writer_failed_replace_leaves_old_file_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            update_repo_override_policy(repo, lambda policy: _append_pin(policy, "alpha"))
            policy_path = repo / ".skillbox" / "skill-overrides.yaml"
            before = policy_path.read_text(encoding="utf-8")

            with mock.patch.object(POLICY_EVAL.os, "replace", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    update_repo_override_policy(repo, lambda policy: _append_pin(policy, "beta"))

            leftovers = [
                path.name for path in policy_path.parent.iterdir()
                if path.name.startswith(".skill-overrides.yaml.") and path.suffix == ".tmp"
            ]
            after = policy_path.read_text(encoding="utf-8")

        self.assertEqual(after, before)
        self.assertEqual(leftovers, [])

    def test_override_writer_fsyncs_file_and_parent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))

            with mock.patch.object(POLICY_EVAL.os, "fsync") as fsync_mock:
                update_repo_override_policy(repo, lambda policy: _append_pin(policy, "alpha"))

        self.assertGreaterEqual(fsync_mock.call_count, 2)

    def test_override_writer_lock_timeout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            policy_dir = repo / ".skillbox"
            policy_dir.mkdir()
            lock_path = policy_dir / "skill-overrides.yaml.lock"
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                with self.assertRaises(OverrideWriteLockTimeout) as ctx:
                    update_repo_override_policy(
                        repo,
                        lambda policy: _append_pin(policy, "blocked"),
                        timeout=0.05,
                    )
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

        self.assertEqual(ctx.exception.lock_path, lock_path)


if __name__ == "__main__":
    unittest.main()
