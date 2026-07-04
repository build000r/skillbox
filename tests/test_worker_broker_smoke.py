"""Worker-broker fake-Hermes smoke test (additive).

Proves the worker broker contract end to end against a FAKE / no-op runtime in a
temporary state root:

  1. worker-submit context resolution,
  2. broker state write (run.json + run-dir launch artifacts in the temp state root),
  3. result read-back via the status + artifacts surfaces.

No real Codex/Hermes/worker is ever launched. The runtime is a tiny fake Python
script wired through ``SKILLBOX_WORKER_HERMES_COMMAND``; ``shutil.which`` is pinned
to ``None`` so a real ``hermes``/``codex`` binary can never be discovered, and a
``subprocess.Popen`` spy records every launched command so the test can assert that
only the fake script ran.

This reuses the established fake-runtime / temp-state-root harness from
``tests/test_worker_runtime.py`` (same module loader, same ``_active_worker_model``
shape, same ``SKILLBOX_WORKER_HERMES_COMMAND`` wiring). It does not introduce a new
harness.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

SHARED = SourceFileLoader(
    "skillbox_runtime_shared_worker_broker_smoke",
    str((ENV_MANAGER_DIR / "runtime_manager" / "shared.py").resolve()),
).load_module()


def _active_worker_model(repo_root: str = "/tmp/skills") -> dict[str, object]:
    """Active runtime model with a single resolvable client.

    Mirrors the ``_active_worker_model`` helper in ``tests/test_worker_runtime.py``.
    Notably it carries no ``storage.state_root`` key, so ``worker_runs_root`` falls
    back to ``<root_dir>/.skillbox-state/worker-runs`` and stays inside the temp dir.
    """
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
    """Blank every Hermes discovery env var so only our explicit fake is used."""
    return {
        "SKILLBOX_WORKER_HERMES_COMMAND": "",
        "SKILLBOX_HERMES_COMMAND": "",
        "SKILLBOX_WORKER_HERMES_BIN": "",
        "SKILLBOX_HERMES_BIN": "",
    }


# A no-op fake Hermes runtime. It records its own invocation to a marker file (so the
# test can prove the FAKE ran rather than a real runtime), echoes the broker-supplied
# run id, and writes a terminal result to the broker-supplied result path. It launches
# no further processes and contacts no network/codex/hermes.
_FAKE_HERMES_SOURCE = "\n".join(
    [
        "import json, os",
        "run_id = os.environ['SKILLBOX_WORKER_RUN_ID']",
        "result_path = os.environ['SKILLBOX_WORKER_RESULT_PATH']",
        "marker_path = os.environ['SKILLBOX_FAKE_HERMES_MARKER']",
        "task_path = os.environ['SKILLBOX_WORKER_TASK_PATH']",
        "# Record the fake invocation so the test can assert no real runtime launched.",
        "with open(marker_path, 'w') as handle:",
        "    json.dump({'fake': True, 'run_id': run_id, 'task_path': task_path}, handle)",
        "with open(result_path, 'w') as handle:",
        "    json.dump({",
        "        'run_id': run_id,",
        "        'state': 'succeeded',",
        "        'summary': 'fake-hermes smoke result',",
        "        'findings': ['context-resolved'],",
        "        'actions_taken': ['noop'],",
        "        'next_action': 'none',",
        "    }, handle)",
    ]
)


def _poll_to_terminal(root: Path, run_id: str, *, timeout_s: float = 8.0) -> dict[str, object]:
    """Poll the broker status surface until the run reaches a terminal state.

    The broker waits only ``WORKER_LAUNCH_SETTLE_SECONDS`` (0.2s) before returning a
    ``running`` payload and reconciling the on-disk result later, so a synchronous
    smoke would race the fake script under load. Polling status (the documented
    reconcile path) keeps the result-read assertion deterministic.
    """
    deadline = time.monotonic() + timeout_s
    status = SHARED.worker_status_payload(root, run_id)
    while status["state"] not in SHARED.WORKER_TERMINAL_STATES and time.monotonic() < deadline:
        time.sleep(0.05)
        status = SHARED.worker_status_payload(root, run_id)
    return status


class WorkerBrokerFakeHermesSmokeTests(unittest.TestCase):
    def test_broker_resolves_context_writes_state_and_reads_back_fake_result(self) -> None:
        model = _active_worker_model()
        launched_commands: list[list[str]] = []
        real_popen = subprocess.Popen

        def _recording_popen(command, *args, **kwargs):  # type: ignore[no-untyped-def]
            launched_commands.append(list(command))
            return real_popen(command, *args, **kwargs)

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            # Resolve client/repo context from a fake model, never the real runtime.
            mock.patch.object(SHARED, "build_runtime_model", return_value=model),
            # No real `hermes`/`codex` can ever be discovered on PATH.
            mock.patch.object(SHARED.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
            # Spy on every subprocess launch to prove only the fake script runs.
            mock.patch.object(SHARED.subprocess, "Popen", side_effect=_recording_popen),
        ):
            root = Path(tmpdir)
            fake_command = root / "fake_hermes_smoke.py"
            fake_command.write_text(_FAKE_HERMES_SOURCE, encoding="utf-8")
            marker_path = root / "fake_hermes_invocation.json"

            env = {
                # Point the broker at the fake/no-op runtime (profiling candidate #6).
                "SKILLBOX_WORKER_HERMES_COMMAND": f"{sys.executable} {fake_command}",
                "SKILLBOX_FAKE_HERMES_MARKER": str(marker_path),
            }
            with mock.patch.dict("os.environ", env):
                submit = SHARED.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Smoke-test the broker with a fake runtime.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                )

                run_id = submit["run_id"]

                # --- (1) worker-submit context resolution ------------------------
                self.assertEqual(submit["context_state"], "resolved")
                resolved = submit["resolved_context"]
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved["client_id"], "skills")
                self.assertEqual(resolved["repo_id"], "skills-repo")
                self.assertEqual(resolved["effective_cwd"], "/tmp/skills/docs")
                self.assertIn("skillbox_worker_submit", resolved["mcp_surfaces"])
                self.assertEqual(submit["runtime"], SHARED.WORKER_DEFAULT_RUNTIME_ID)
                self.assertTrue(submit["launch"]["attempted"])

                # --- (2) broker state write (in the temp state root) -------------
                paths = SHARED.worker_run_paths(root, run_id)
                runs_root = SHARED.worker_runs_root(root)
                # Everything lives under the temp dir; no real runtime state touched.
                self.assertTrue(str(runs_root).startswith(str(root)))
                self.assertEqual(runs_root, root / ".skillbox-state" / "worker-runs")
                self.assertTrue(paths["run_path"].is_file(), "broker did not persist run.json")
                self.assertTrue(paths["events_path"].is_file(), "broker did not persist events.jsonl")
                persisted = json.loads(paths["run_path"].read_text(encoding="utf-8"))
                self.assertEqual(persisted["run_id"], run_id)
                self.assertEqual(persisted["context_state"], "resolved")
                # The broker wrote the task hand-off file for the runtime to read.
                task_path = paths["run_dir"] / "task.json"
                self.assertTrue(task_path.is_file(), "broker did not write task.json")
                task_doc = json.loads(task_path.read_text(encoding="utf-8"))
                self.assertEqual(task_doc["resolved_context"]["client_id"], "skills")

                # --- (3) result read-back ----------------------------------------
                status = _poll_to_terminal(root, run_id)
                self.assertEqual(status["state"], "succeeded")
                self.assertTrue(status["artifacts_ready"])
                self.assertEqual(status["summary"], "fake-hermes smoke result")

                artifacts = SHARED.worker_artifacts_payload(root, run_id)
                self.assertEqual(artifacts["state"], "succeeded")
                self.assertEqual(artifacts["result"]["summary"], "fake-hermes smoke result")
                self.assertEqual(artifacts["result"]["findings"], ["context-resolved"])
                self.assertEqual(artifacts["result"]["actions_taken"], ["noop"])
                self.assertEqual(artifacts["artifacts"][0]["kind"], "summary")
                # The summary artifact was materialized on disk under the temp root.
                summary_path = Path(artifacts["artifacts"][0]["path"])
                self.assertTrue(summary_path.is_file())
                self.assertTrue(str(summary_path).startswith(str(root)))

                # --- NO real runtime launched ------------------------------------
                # The fake recorded its own invocation, keyed to the broker's run id.
                self.assertTrue(marker_path.is_file(), "fake runtime never recorded an invocation")
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                self.assertTrue(marker["fake"])
                self.assertEqual(marker["run_id"], run_id)

                # The broker launched exactly one process: our fake script via the
                # current interpreter. No codex/hermes binary was ever invoked.
                self.assertEqual(len(launched_commands), 1, f"unexpected launches: {launched_commands}")
                argv = launched_commands[0]
                self.assertEqual(argv[0], sys.executable)
                self.assertEqual(argv[1], str(fake_command))
                # The launched argv[0] is the test interpreter, not a real runtime
                # binary; no codex/hermes executable appears anywhere in the command.
                for part in argv:
                    base = Path(part).name.lower()
                    self.assertNotIn(base, {"hermes", "codex"}, f"real runtime binary launched: {part}")

    def test_unconfigured_runtime_fails_closed_without_launch(self) -> None:
        """With no fake configured and no discoverable binary, the broker fails
        closed (WORKER_LAUNCH_FAILED) and never spawns a process."""
        model = _active_worker_model()
        launched_commands: list[list[str]] = []

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "build_runtime_model", return_value=model),
            mock.patch.object(SHARED.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
            mock.patch.object(
                SHARED.subprocess,
                "Popen",
                side_effect=lambda command, *a, **k: launched_commands.append(list(command)),
            ),
        ):
            root = Path(tmpdir)
            payload = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="No runtime configured.",
                client_id="skills",
                cwd="/tmp/skills/docs",
            )

            # Context still resolves; only the launch fails closed.
            self.assertEqual(payload["context_state"], "resolved")
            self.assertEqual(payload["state"], "failed")
            self.assertEqual(payload["launch"]["blocked_reason"], "WORKER_LAUNCH_FAILED")
            self.assertEqual(payload["result"]["error"]["type"], "WORKER_LAUNCH_FAILED")
            # The broker reported a fake `hermes` placeholder command but never spawned it.
            self.assertEqual(payload["launch"]["command"], ["hermes"])
            self.assertEqual(launched_commands, [], "no subprocess should launch when unconfigured")


if __name__ == "__main__":
    unittest.main()
