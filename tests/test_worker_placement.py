"""Worker placement grant-persist, idempotency, and remote classify tests.

Imports new helpers from ``runtime_manager._shared.worker`` (not the shared.py
facade). Fake-Hermes launch pattern matches ``tests/test_worker_broker_smoke.py``.
No network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager._shared import worker as W  # noqa: E402
import runtime_manager.cli as CLI  # noqa: E402

try:
    import yaml  # noqa: F401

    _HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover
    _HAVE_YAML = False


FIXTURE_YAML = textwrap.dedent(
    """
    version: 1

    machines:
      mac-laptop:
        hostnames: [Mac-2, bs-macbook-air]
        home: /Users/operator
        repo_roots:
          - /Users/operator/repos
        caps: [os:darwin, arch:arm64, xcode, durable]
        trust: local

      portfolio-devbox:
        hostnames: [portfolio-devbox]
        home: /home/skillbox
        repo_roots:
          - /srv/skillbox/repos
        caps: [os:linux, arch:amd64, docker, tailnet, durable]
        trust: allowlisted
    """
).strip()

_FAKE_HERMES_SOURCE = "\n".join(
    [
        "import json, os",
        "run_id = os.environ['SKILLBOX_WORKER_RUN_ID']",
        "result_path = os.environ['SKILLBOX_WORKER_RESULT_PATH']",
        "marker_path = os.environ['SKILLBOX_FAKE_HERMES_MARKER']",
        "task_path = os.environ['SKILLBOX_WORKER_TASK_PATH']",
        "with open(marker_path, 'w') as handle:",
        "    json.dump({'fake': True, 'run_id': run_id, 'task_path': task_path}, handle)",
        "with open(result_path, 'w') as handle:",
        "    json.dump({",
        "        'run_id': run_id,",
        "        'state': 'succeeded',",
        "        'summary': 'fake-hermes placement result',",
        "        'findings': ['placed'],",
        "        'actions_taken': ['noop'],",
        "        'next_action': 'none',",
        "    }, handle)",
    ]
)


def _active_worker_model(repo_root: str = "/tmp/skills") -> dict[str, object]:
    return {
        "active_profiles": ["core"],
        "clients": [
            {
                "id": "skills",
                "default_cwd_host_path": repo_root,
                "context": {
                    "deploy": {
                        "repo_root": repo_root,
                        "repo_slug": "example/skills",
                    }
                },
            }
        ],
        "repos": [{"id": "skills-repo", "host_path": repo_root}],
    }


def _no_hermes_env() -> dict[str, str]:
    return {
        "SKILLBOX_WORKER_HERMES_COMMAND": "",
        "SKILLBOX_HERMES_COMMAND": "",
        "SKILLBOX_WORKER_HERMES_BIN": "",
        "SKILLBOX_HERMES_BIN": "",
    }


def _write_machines(root: Path) -> Path:
    path = root / "machines.yaml"
    path.write_text(FIXTURE_YAML + "\n", encoding="utf-8")
    return path


def _poll_to_terminal(root: Path, run_id: str, *, timeout_s: float = 8.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    status = W.worker_status_payload(root, run_id)
    while status["state"] not in W.WORKER_TERMINAL_STATES and time.monotonic() < deadline:
        time.sleep(0.05)
        status = W.worker_status_payload(root, run_id)
    return status


class ClassifyRemoteResultTests(unittest.TestCase):
    def test_truth_table(self) -> None:
        self.assertEqual(W.classify_remote_result(0, False), "completed")
        self.assertEqual(W.classify_remote_result(255, True), "result_unavailable")
        self.assertEqual(W.classify_remote_result(0, True), "result_unavailable")
        self.assertEqual(W.classify_remote_result(1, False), "command_failed")
        self.assertEqual(W.classify_remote_result(255, False), "command_failed")


class BuildRemoteSubmitCommandTests(unittest.TestCase):
    def test_composes_ssh_via_box_conventions(self) -> None:
        argv = W.build_remote_submit_command(
            {"machine_id": "mac-laptop", "box_id": "portfolio-devbox"},
            ["python3", ".env-manager/manage.py", "worker-submit", "analysis", "go"],
        )
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-o", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("--", argv)
        self.assertIn("skillbox@portfolio-devbox", argv)
        self.assertTrue(any("worker-submit" in part for part in argv))

    def test_falls_back_to_machine_id(self) -> None:
        argv = W.build_remote_submit_command({"machine_id": "mac-laptop"}, ["true"])
        self.assertIn("skillbox@mac-laptop", argv)


class WorkerRunExistsTests(unittest.TestCase):
    def test_create_worker_run_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = W.create_worker_run(
                root,
                task_class="analysis",
                instruction="First write.",
                client_id="personal",
                cwd=str(root),
            )
            with self.assertRaises(W.WorkerRuntimeError) as raised:
                W.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Overwrite attempt.",
                    client_id="personal",
                    cwd=str(root),
                    run_id=first["run_id"],
                )
            self.assertEqual(raised.exception.code, W.WORKER_RUN_EXISTS)
            persisted = json.loads(
                W.worker_run_paths(root, first["run_id"])["run_path"].read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["task_spec"]["instruction"], "First write.")


class WorkerIdempotencyTests(unittest.TestCase):
    def test_duplicate_key_returns_existing_run(self) -> None:
        model = _active_worker_model()
        launched: list[list[str]] = []
        real_popen = subprocess.Popen

        def _recording_popen(command, *args, **kwargs):  # type: ignore[no-untyped-def]
            launched.append(list(command))
            return real_popen(command, *args, **kwargs)

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(W, "build_runtime_model", return_value=model),
            mock.patch.object(W.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
            mock.patch.object(W.subprocess, "Popen", side_effect=_recording_popen),
        ):
            root = Path(tmpdir)
            fake_command = root / "fake_hermes_placement.py"
            fake_command.write_text(_FAKE_HERMES_SOURCE, encoding="utf-8")
            marker_path = root / "fake_hermes_invocation.json"
            env = {
                "SKILLBOX_WORKER_HERMES_COMMAND": f"{sys.executable} {fake_command}",
                "SKILLBOX_FAKE_HERMES_MARKER": str(marker_path),
            }
            with mock.patch.dict("os.environ", env):
                first = W.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Idempotent submit.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                    idempotency_key="job-place-1",
                )
                second = W.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Idempotent submit again.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                    idempotency_key="job-place-1",
                )
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertTrue(second.get("duplicate"))
            self.assertEqual(len(launched), 1)


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class WorkerPlacementGrantTests(unittest.TestCase):
    def test_local_selection_writes_placement_before_launch(self) -> None:
        model = _active_worker_model()
        original_launch = W._launch_worker_if_ready
        seen_before_launch: list[dict[str, object]] = []

        def _launch_after_assert(root_dir, paths, payload):  # type: ignore[no-untyped-def]
            persisted = json.loads(paths["run_path"].read_text(encoding="utf-8"))
            seen_before_launch.append(persisted)
            return original_launch(root_dir, paths, payload)

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(W, "build_runtime_model", return_value=model),
            mock.patch.object(W.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
            mock.patch.object(W, "_launch_worker_if_ready", side_effect=_launch_after_assert),
        ):
            root = Path(tmpdir)
            _write_machines(root)
            env = {"SKILLBOX_MACHINE": "mac-laptop"}
            with mock.patch.dict("os.environ", env):
                payload = W.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Place on current Mac.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                    needs=["xcode"],
                )
            self.assertEqual(len(seen_before_launch), 1)
            grant = seen_before_launch[0]["placement"]
            self.assertEqual(grant["kind"], "machine-placement/v1")
            self.assertEqual(grant["decision"], "selected")
            self.assertEqual(grant["machine_id"], "mac-laptop")
            self.assertEqual(payload["placement"]["machine_id"], "mac-laptop")
            self.assertEqual(payload["machine_id"], "mac-laptop")
            status = W.worker_status_payload(root, payload["run_id"])
            self.assertEqual(status["placement"]["machine_id"], "mac-laptop")
            self.assertEqual(status["machine_id"], "mac-laptop")

    def test_non_local_is_typed_refusal_without_fabricated_state(self) -> None:
        launched: list[list[str]] = []
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(W, "build_runtime_model", return_value=_active_worker_model()),
            mock.patch.object(W.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
            mock.patch.object(
                W.subprocess,
                "Popen",
                side_effect=lambda command, *a, **k: launched.append(list(command)),
            ),
        ):
            root = Path(tmpdir)
            _write_machines(root)
            with mock.patch.dict("os.environ", {"SKILLBOX_MACHINE": "portfolio-devbox"}):
                with self.assertRaises(W.WorkerRuntimeError) as raised:
                    W.create_worker_run(
                        root,
                        task_class="analysis",
                        instruction="Needs Xcode on another machine.",
                        client_id="skills",
                        cwd="/tmp/skills/docs",
                        needs=["xcode"],
                        allow_unverified=True,
                    )
            self.assertEqual(raised.exception.code, W.PLACEMENT_NOT_LOCAL)
            decision = raised.exception.details["decision"]
            self.assertEqual(decision["decision"], "selected")
            self.assertEqual(decision["machine_id"], "mac-laptop")
            self.assertTrue(raised.exception.details["next_actions"])
            self.assertTrue(
                any("box.py ssh" in str(item) for item in raised.exception.details["next_actions"])
            )
            envelope = W.worker_runtime_error_payload(raised.exception)
            self.assertEqual(envelope["error"]["code"], W.PLACEMENT_NOT_LOCAL)
            self.assertIn("next_actions", envelope["error"])
            self.assertEqual(launched, [])
            runs_root = W.worker_runs_root(root)
            if runs_root.is_dir():
                self.assertEqual(list(runs_root.glob("*/run.json")), [])

    def test_fake_hermes_local_placement_round_trip(self) -> None:
        model = _active_worker_model()
        launched: list[list[str]] = []
        real_popen = subprocess.Popen

        def _recording_popen(command, *args, **kwargs):  # type: ignore[no-untyped-def]
            launched.append(list(command))
            return real_popen(command, *args, **kwargs)

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(W, "build_runtime_model", return_value=model),
            mock.patch.object(W.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
            mock.patch.object(W.subprocess, "Popen", side_effect=_recording_popen),
        ):
            root = Path(tmpdir)
            _write_machines(root)
            fake_command = root / "fake_hermes_placement.py"
            fake_command.write_text(_FAKE_HERMES_SOURCE, encoding="utf-8")
            marker_path = root / "fake_hermes_invocation.json"
            env = {
                "SKILLBOX_WORKER_HERMES_COMMAND": f"{sys.executable} {fake_command}",
                "SKILLBOX_FAKE_HERMES_MARKER": str(marker_path),
                "SKILLBOX_MACHINE": "mac-laptop",
            }
            with mock.patch.dict("os.environ", env):
                submit = W.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Smoke place + fake hermes.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                    needs=["os:darwin", "xcode"],
                    need_trust="local",
                )
                status = _poll_to_terminal(root, submit["run_id"])
            self.assertEqual(submit["placement"]["machine_id"], "mac-laptop")
            self.assertEqual(status["state"], "succeeded")
            self.assertEqual(status["placement"]["needs"]["caps"], ["os:darwin", "xcode"])
            self.assertEqual(len(launched), 1)
            self.assertEqual(launched[0][0], sys.executable)


class WorkerSubmitCliNeedFlagTests(unittest.TestCase):
    def test_parser_accepts_repeatable_need_and_trust_flags(self) -> None:
        args = CLI._build_parser().parse_args(  # noqa: SLF001
            [
                "worker-submit",
                "analysis",
                "Need flags.",
                "--need",
                "xcode",
                "--need",
                "os:darwin",
                "--need-trust",
                "local",
                "--allow-unverified",
                "--idempotency-key",
                "k1",
            ]
        )
        self.assertEqual(args.need, ["xcode", "os:darwin"])
        self.assertEqual(args.need_trust, "local")
        self.assertTrue(args.allow_unverified)
        self.assertEqual(args.idempotency_key, "k1")

    def test_status_text_includes_machine_when_present(self) -> None:
        lines = CLI._worker_status_text(  # noqa: SLF001
            {
                "run_id": "wr_20260813_000000_abcdef",
                "state": "queued",
                "runtime": "hermes",
                "placement": {"machine_id": "mac-laptop"},
            }
        )
        self.assertIn("machine: mac-laptop", lines)


if __name__ == "__main__":
    unittest.main()
