from __future__ import annotations

import importlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
RUNTIME_MANAGER_DIR = ENV_MANAGER_DIR / "runtime_manager"


def _import_runtime_modules() -> tuple[types.ModuleType, types.ModuleType]:
    if str(ENV_MANAGER_DIR) not in sys.path:
        sys.path.insert(0, str(ENV_MANAGER_DIR))
    if "runtime_manager" not in sys.modules:
        package = types.ModuleType("runtime_manager")
        package.__path__ = [str(RUNTIME_MANAGER_DIR)]  # type: ignore[attr-defined]
        sys.modules["runtime_manager"] = package
    shared = importlib.import_module("runtime_manager.shared")
    runtime_ops = importlib.import_module("runtime_manager.runtime_ops")
    return shared, runtime_ops


SHARED, RUNTIME_OPS = _import_runtime_modules()


SYNC_SUBPROCESS = r"""
import importlib
import json
import os
import sys
import types
from pathlib import Path

repo = Path(os.environ["SKILLBOX_REPO_ROOT"])
env_manager = repo / ".env-manager"
runtime_manager_dir = env_manager / "runtime_manager"
sys.path.insert(0, str(env_manager))
package = types.ModuleType("runtime_manager")
package.__path__ = [str(runtime_manager_dir)]
sys.modules["runtime_manager"] = package
runtime_ops = importlib.import_module("runtime_manager.runtime_ops")
model = json.loads(Path(os.environ["SKILLBOX_TEST_MODEL"]).read_text(encoding="utf-8"))
runtime_ops._sync_distributor_sources = lambda model, dry_run: []
runtime_ops.sync_skill_sets = lambda model, dry_run: []
runtime_ops._log_model_runtime_event = lambda *args, **kwargs: None
runtime_ops.sync_runtime(model, dry_run=False)
"""


def _base_model(root: Path) -> dict[str, object]:
    return {
        "root_dir": str(root),
        "env": {},
        "repos": [],
        "artifacts": [],
        "env_files": [],
        "logs": [],
        "skills": [],
        "services": [],
        "tasks": [],
        "checks": [],
        "bridges": [],
        "ingress_routes": [],
        "clients": [],
        "storage": {},
    }


def _write_model(root: Path, model: dict[str, object]) -> Path:
    path = root / "model.json"
    path.write_text(json.dumps(model), encoding="utf-8")
    return path


def _run_sync(model_path: Path, *, kill_at: int | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SKILLBOX_REPO_ROOT"] = str(ROOT_DIR)
    env["SKILLBOX_TEST_MODEL"] = str(model_path)
    if kill_at is not None:
        env["SKILLBOX_TEST_ATOMICITY_KILL_AT"] = str(kill_at)
    else:
        env.pop("SKILLBOX_TEST_ATOMICITY_KILL_AT", None)
    return subprocess.run(
        [sys.executable, "-c", SYNC_SUBPROCESS],
        cwd=str(model_path.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )


def _assert_sync_completed(test: unittest.TestCase, result: subprocess.CompletedProcess[str]) -> None:
    test.assertEqual(
        result.returncode,
        0,
        f"sync failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
    )


class SyncAtomicityHelperTests(unittest.TestCase):
    def test_atomic_write_text_keeps_existing_file_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.txt"
            path.write_text("old\n", encoding="utf-8")

            with mock.patch.object(SHARED.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    SHARED.atomic_write_text(path, "new\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(list(path.parent.glob(".state.txt.*.tmp")), [])

    def test_atomic_write_bytes_publishes_payload_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "secret.env"

            SHARED.atomic_write_bytes(path, b"token=value\n", mode=0o640)

            self.assertEqual(path.read_bytes(), b"token=value\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_atomic_replace_tree_keeps_old_tree_when_build_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "skill"
            target.mkdir()
            (target / "SKILL.md").write_text("old\n", encoding="utf-8")

            def build(stage_dir: Path) -> None:
                (stage_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
                raise RuntimeError("stop before publish")

            with self.assertRaises(RuntimeError):
                SHARED.atomic_replace_tree(target, build)

            self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "old\n")

    def test_filtered_copy_skill_publishes_complete_filtered_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source" / "alpha"
            target = root / "target" / "alpha"
            (source / "docs").mkdir(parents=True)
            (source / "cache").mkdir()
            (source / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
            (source / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
            (source / "ignored.txt").write_text("ignore me\n", encoding="utf-8")
            (source / "cache" / "tmp.txt").write_text("ignore me\n", encoding="utf-8")
            (source / ".skillignore").write_text("ignored.txt\ncache/\n", encoding="utf-8")
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old\n", encoding="utf-8")

            tree_sha = SHARED.filtered_copy_skill(source, target)

            self.assertEqual(tree_sha, SHARED.directory_tree_sha256(target))
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertTrue((target / "docs" / "guide.md").is_file())
            self.assertFalse((target / "old.txt").exists())
            self.assertFalse((target / "ignored.txt").exists())
            self.assertFalse((target / "cache").exists())

    def test_skill_repo_sync_records_lockfile_after_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "skill-src" / "alpha"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
            config = root / "skill-repos.yaml"
            config.write_text("version: 2\nskill_repos: []\n", encoding="utf-8")
            lock = root / "skill-repos.lock.json"
            install_root = root / "skills"
            events: list[str] = []
            model = _base_model(root)
            model["skills"] = [
                {
                    "id": "local-skills",
                    "kind": "skill-repo-set",
                    "sync": {"mode": "clone-and-install"},
                    "skill_repos_config_host_path": str(config),
                    "lock_path_host_path": str(lock),
                    "clone_root_host_path": str(root / "clones"),
                    "install_targets": [{"id": "test", "host_path": str(install_root)}],
                }
            ]

            def fake_filtered_copy(skill_source: Path, install_dir: Path) -> str:
                events.append(f"install:{skill_source.name}")
                install_dir.mkdir(parents=True, exist_ok=True)
                (install_dir / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
                return "tree-sha"

            def fake_write_json(path: Path, payload: dict[str, object]) -> bool:
                events.append(f"lock:{path.name}")
                return True

            with (
                mock.patch.object(
                    SHARED,
                    "load_skill_repos_config",
                    return_value={"skill_repos": [{"path": str(root / "skill-src"), "pick": ["alpha"]}]},
                ),
                mock.patch.object(SHARED, "filtered_copy_skill", side_effect=fake_filtered_copy),
                mock.patch.object(SHARED, "write_json_file", side_effect=fake_write_json),
            ):
                actions = SHARED.sync_skill_repo_sets(model, dry_run=False)

            self.assertEqual(events, ["install:alpha", "lock:skill-repos.lock.json"])
            self.assertEqual(actions[-1], f"write-lockfile: {lock}")


class SyncAtomicityKillHarnessTests(unittest.TestCase):
    def test_seeded_mid_sync_kills_never_publish_partial_state(self) -> None:
        rng = random.Random(86209)
        scenarios = ["artifact", "env", "tree"]
        for index in range(50):
            scenario = rng.choice(scenarios)
            kill_at = rng.randint(1, 4)
            with self.subTest(iteration=index, scenario=scenario, kill_at=kill_at):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    model, paths = self._prepare_scenario(root, scenario)
                    model_path = _write_model(root, model)
                    _assert_sync_completed(self, _run_sync(model_path))
                    self._make_single_item_unsynced(scenario, paths, index)

                    result = _run_sync(model_path, kill_at=kill_at)
                    self.assertIn(result.returncode, (0, -signal.SIGKILL))

                    unsynced = self._assert_no_partial_state(scenario, paths)
                    if result.returncode == 0:
                        self.assertEqual(unsynced, [])
                    else:
                        self.assertIn(unsynced, ([], [scenario]))

    def _prepare_scenario(self, root: Path, scenario: str) -> tuple[dict[str, object], dict[str, Path]]:
        model = _base_model(root)
        paths: dict[str, Path] = {}
        if scenario == "artifact":
            source = root / "sources" / "artifact.txt"
            target = root / "out" / "artifact.txt"
            source.parent.mkdir(parents=True)
            source.write_text("artifact-0\n", encoding="utf-8")
            model["artifacts"] = [
                {
                    "id": "artifact",
                    "host_path": str(target),
                    "path": str(target),
                    "source": {"kind": "file", "host_path": str(source)},
                    "sync": {"mode": "copy-if-missing"},
                }
            ]
            paths.update({"source": source, "target": target})
            return model, paths

        if scenario == "env":
            source = root / "sources" / ".env.source"
            target = root / "out" / ".env"
            source.parent.mkdir(parents=True)
            source.write_text("TOKEN=0\n", encoding="utf-8")
            model["env_files"] = [
                {
                    "id": "env",
                    "host_path": str(target),
                    "path": str(target),
                    "source": {"kind": "file", "host_path": str(source)},
                    "sync": {"mode": "write"},
                    "mode": "0600",
                }
            ]
            paths.update({"source": source, "target": target})
            return model, paths

        source_root = root / "skill-src"
        source = source_root / "alpha"
        install_root = root / "out" / "skills"
        target = install_root / "alpha"
        config = root / "skill-repos.yaml"
        lock = root / "out" / "skill-repos.lock.json"
        (source / "docs").mkdir(parents=True)
        source.joinpath("SKILL.md").write_text("# Alpha\n", encoding="utf-8")
        source.joinpath("docs", "guide.md").write_text("guide\n", encoding="utf-8")
        config.write_text(
            "version: 2\n"
            "skill_repos:\n"
            "  - path: ./skill-src\n"
            "    pick: [alpha]\n",
            encoding="utf-8",
        )
        model["skills"] = [
            {
                "id": "local-skills",
                "kind": "skill-repo-set",
                "sync": {"mode": "clone-and-install"},
                "skill_repos_config_host_path": str(config),
                "lock_path_host_path": str(lock),
                "clone_root_host_path": str(root / "clones"),
                "install_targets": [{"id": "test", "host_path": str(install_root)}],
            }
        ]
        paths.update({"source": source, "target": target, "lock": lock})
        return model, paths

    def _make_single_item_unsynced(self, scenario: str, paths: dict[str, Path], index: int) -> None:
        if scenario == "artifact":
            paths["source"].write_text(f"artifact-{index + 1}\n", encoding="utf-8")
            return
        if scenario == "env":
            paths["source"].write_text(f"TOKEN={index + 1}\n", encoding="utf-8")
            return
        shutil.rmtree(paths["target"])

    def _assert_no_partial_state(self, scenario: str, paths: dict[str, Path]) -> list[str]:
        if scenario in {"artifact", "env"}:
            source_payload = paths["source"].read_bytes()
            target = paths["target"]
            if not target.exists():
                return [scenario]
            target_payload = target.read_bytes()
            if target_payload == source_payload:
                return []
            old_payload = b"artifact-0\n" if scenario == "artifact" else b"TOKEN=0\n"
            self.assertEqual(target_payload, old_payload)
            return [scenario]

        target = paths["target"]
        lock = paths["lock"]
        self.assertTrue(lock.is_file())
        payload = json.loads(lock.read_text(encoding="utf-8"))
        self.assertEqual([skill["name"] for skill in payload["skills"]], ["alpha"])
        if not target.exists():
            return [scenario]
        self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "# Alpha\n")
        self.assertEqual((target / "docs" / "guide.md").read_text(encoding="utf-8"), "guide\n")
        self.assertEqual(payload["skills"][0]["install_tree_sha"], SHARED.directory_tree_sha256(target))
        return []


if __name__ == "__main__":
    unittest.main()
