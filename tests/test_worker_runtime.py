from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
MANAGER = ENV_MANAGER_DIR / "manage.py"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

SHARED = SourceFileLoader(
    "skillbox_runtime_shared_worker_tests",
    str((ENV_MANAGER_DIR / "runtime_manager" / "shared.py").resolve()),
).load_module()

import runtime_manager.cli as CLI  # noqa: E402

WORKER_RUNTIME_SHARED_MD = (
    ROOT_DIR.parent
    / "skillbox-config"
    / "clients"
    / "skillbox"
    / "plans"
    / "released"
    / "worker_runtime"
    / "shared.md"
)

LOCKED_SHARED_MD_VALUES = {
    "Task classes": (
        "analysis",
        "interpretation",
        "recommendation",
        "drafting",
        "research",
        "ops_execution",
    ),
    "Runtime ids": ("hermes",),
    "Write scopes": ("read_only", "propose_only", "repo_patch"),
    "Memory scopes": ("none", "repo", "client"),
    "Run states": (
        "queued",
        "resolving",
        "blocked",
        "launching",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "review_pending",
    ),
    "Stable error codes": (
        "WORKER_CONTEXT_UNRESOLVED",
        "WORKER_RUNTIME_UNKNOWN",
        "WORKER_POLICY_BLOCKED",
        "WORKER_LAUNCH_FAILED",
        "WORKER_RUN_NOT_FOUND",
        "WORKER_RESULT_NOT_READY",
        "WORKER_LEARNING_REVIEW_REQUIRED",
        "WORKER_WRITEBACK_REJECTED",
    ),
}


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
                        "repo_slug": "build000r/skills",
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


def _shared_md_values(heading: str) -> tuple[str, ...]:
    text = WORKER_RUNTIME_SHARED_MD.read_text(encoding="utf-8")
    marker = f"### {heading}"
    if marker not in text:
        raise AssertionError(f"{WORKER_RUNTIME_SHARED_MD} is missing {marker!r}")
    section = text.split(marker, 1)[1].split("\n### ", 1)[0]
    values: list[str] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        if not cells or cells[0] in {"Value", "Code"} or set(cells[0]) <= {"-"}:
            continue
        values.append(cells[0])
    return tuple(values)


class WorkerRuntimeContractTests(unittest.TestCase):
    def test_worker_runtime_constants_match_locked_shared_md_values(self) -> None:
        self.assertEqual(SHARED.WORKER_TASK_CLASSES, LOCKED_SHARED_MD_VALUES["Task classes"])
        self.assertEqual(SHARED.WORKER_RUNTIME_IDS, LOCKED_SHARED_MD_VALUES["Runtime ids"])
        self.assertEqual(SHARED.WORKER_WRITE_SCOPES, LOCKED_SHARED_MD_VALUES["Write scopes"])
        self.assertEqual(SHARED.WORKER_MEMORY_SCOPES, LOCKED_SHARED_MD_VALUES["Memory scopes"])
        self.assertEqual(SHARED.WORKER_RUN_STATES, LOCKED_SHARED_MD_VALUES["Run states"])
        self.assertEqual(SHARED.WORKER_ERROR_CODES, LOCKED_SHARED_MD_VALUES["Stable error codes"])

    def test_worker_runtime_constants_match_released_shared_md_exactly(self) -> None:
        if not WORKER_RUNTIME_SHARED_MD.is_file():
            self.skipTest(f"released worker_runtime shared.md unavailable at {WORKER_RUNTIME_SHARED_MD}")
        self.assertEqual(SHARED.WORKER_TASK_CLASSES, _shared_md_values("Task classes"))
        self.assertEqual(SHARED.WORKER_RUNTIME_IDS, _shared_md_values("Runtime ids"))
        self.assertEqual(SHARED.WORKER_WRITE_SCOPES, _shared_md_values("Write scopes"))
        self.assertEqual(SHARED.WORKER_MEMORY_SCOPES, _shared_md_values("Memory scopes"))
        self.assertEqual(SHARED.WORKER_RUN_STATES, _shared_md_values("Run states"))
        self.assertEqual(SHARED.WORKER_ERROR_CODES, _shared_md_values("Stable error codes"))

    def test_create_worker_run_queues_without_launch_before_context_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            payload = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Inspect the current repo.",
                client_id="personal",
                cwd=str(root),
            )

            self.assertEqual(payload["state"], "queued")
            self.assertEqual(payload["runtime"], "hermes")
            self.assertEqual(payload["run"]["state"], "queued")
            self.assertIsNone(payload["run"]["started_at"])
            self.assertIsNone(payload["resolved_context"])
            self.assertEqual(payload["context_state"], "resolving")
            self.assertFalse(payload["launch"]["attempted"])
            self.assertIsNone(payload["launch"]["blocked_reason"])
            self.assertTrue(Path(payload["paths"]["run"]).is_file())

    def test_create_worker_run_blocks_without_context_and_does_not_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            payload = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="No context should block before launch.",
            )

            self.assertEqual(payload["state"], "blocked")
            self.assertEqual(payload["run"]["blocked_reason"], "WORKER_CONTEXT_UNRESOLVED")
            self.assertIsNone(payload["run"]["started_at"])
            self.assertIsNone(payload["resolved_context"])
            self.assertFalse(payload["launch"]["attempted"])
            self.assertEqual(payload["launch"]["blocked_reason"], "WORKER_CONTEXT_UNRESOLVED")
            saved = json.loads(Path(payload["paths"]["run"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["run"]["state"], "blocked")

    def test_create_worker_run_resolves_active_client_and_records_missing_hermes(self) -> None:
        model = _active_worker_model()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "build_runtime_model", return_value=model),
            mock.patch.object(SHARED.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
        ):
            root = Path(tmpdir)

            payload = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Resolve active context.",
                client_id="skills",
                cwd="/tmp/skills/docs",
            )

            self.assertEqual(payload["state"], "failed")
            self.assertEqual(payload["context_state"], "resolved")
            self.assertTrue(payload["launch"]["attempted"])
            self.assertEqual(payload["launch"]["blocked_reason"], "WORKER_LAUNCH_FAILED")
            self.assertEqual(payload["resolved_context"]["client_id"], "skills")
            self.assertEqual(payload["resolved_context"]["repo_id"], "skills-repo")
            self.assertIn("skillbox_worker_submit", payload["resolved_context"]["mcp_surfaces"])
            self.assertEqual(payload["result"]["error"]["type"], "WORKER_LAUNCH_FAILED")

    def test_create_worker_run_launches_configured_hermes_command(self) -> None:
        model = _active_worker_model()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "build_runtime_model", return_value=model),
            mock.patch.object(SHARED.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
        ):
            root = Path(tmpdir)
            command = root / "fake_hermes.py"
            command.write_text(
                "\n".join(
                    [
                        "import json, os",
                        "result_path = os.environ['SKILLBOX_WORKER_RESULT_PATH']",
                        "run_id = os.environ['SKILLBOX_WORKER_RUN_ID']",
                        "json.dump({'run_id': run_id, 'state': 'succeeded', 'summary': 'Hermes done.'}, open(result_path, 'w'))",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.dict("os.environ", {"SKILLBOX_WORKER_HERMES_COMMAND": f"{sys.executable} {command}"}):
                payload = SHARED.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Launch configured Hermes.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                )

            self.assertEqual(payload["state"], "succeeded")
            self.assertTrue(payload["launch"]["attempted"])
            self.assertEqual(payload["launch"]["returncode"], 0)
            self.assertEqual(payload["result"]["summary"], "Hermes done.")
            self.assertEqual(payload["artifacts"][0]["kind"], "summary")
            self.assertEqual(SHARED.worker_artifacts_payload(root, payload["run_id"])["result"]["summary"], "Hermes done.")

    def test_create_worker_run_returns_running_and_status_reconciles_later_result(self) -> None:
        model = _active_worker_model()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "build_runtime_model", return_value=model),
            mock.patch.object(SHARED.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
        ):
            root = Path(tmpdir)
            command = root / "fake_hermes_async.py"
            command.write_text(
                "\n".join(
                    [
                        "import json, os, time",
                        "result_path = os.environ['SKILLBOX_WORKER_RESULT_PATH']",
                        "run_id = os.environ['SKILLBOX_WORKER_RUN_ID']",
                        "time.sleep(1.0)",
                        "json.dump({'run_id': run_id, 'state': 'succeeded', 'summary': 'Async Hermes done.'}, open(result_path, 'w'))",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.dict("os.environ", {"SKILLBOX_WORKER_HERMES_COMMAND": f"{sys.executable} {command}"}):
                payload = SHARED.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Launch async Hermes.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                )

            self.assertEqual(payload["state"], "running")
            self.assertTrue(payload["launch"]["attempted"])
            self.assertIsInstance(payload["launch"]["pid"], int)
            self.assertFalse(Path(payload["paths"]["run"]).with_name("result.json").exists())

            status = payload
            for _ in range(40):
                status = SHARED.worker_status_payload(root, payload["run_id"])
                if status["state"] == "succeeded":
                    break
                time.sleep(0.05)

            self.assertEqual(status["state"], "succeeded")
            self.assertEqual(status["summary"], "Async Hermes done.")
            self.assertTrue(status["artifacts_ready"])
            self.assertEqual(
                SHARED.worker_artifacts_payload(root, payload["run_id"])["result"]["summary"],
                "Async Hermes done.",
            )

    def test_create_worker_run_records_hermes_start_exception(self) -> None:
        env = _no_hermes_env()
        env["SKILLBOX_WORKER_HERMES_COMMAND"] = "hermes"
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "build_runtime_model", return_value=_active_worker_model()),
            mock.patch.dict("os.environ", env),
            mock.patch.object(SHARED.subprocess, "Popen", side_effect=OSError("boom")),
        ):
            payload = SHARED.create_worker_run(
                Path(tmpdir),
                task_class="analysis",
                instruction="Launch raises.",
                client_id="skills",
                cwd="/tmp/skills/docs",
            )

            self.assertEqual(payload["state"], "failed")
            self.assertEqual(payload["launch"]["blocked_reason"], "WORKER_LAUNCH_FAILED")
            self.assertIn("failed to start", payload["result"]["error"]["message"])

    def test_create_worker_run_records_nonzero_hermes_exit(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "build_runtime_model", return_value=_active_worker_model()),
        ):
            root = Path(tmpdir)
            command = root / "fake_hermes_fail.py"
            command.write_text(
                "\n".join(
                    [
                        "import sys",
                        "sys.stderr.write('launch failed')",
                        "sys.exit(7)",
                    ]
                ),
                encoding="utf-8",
            )
            env = _no_hermes_env()
            env["SKILLBOX_WORKER_HERMES_COMMAND"] = f"{sys.executable} {command}"

            with mock.patch.dict("os.environ", env):
                payload = SHARED.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Launch exits nonzero.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                )

            self.assertEqual(payload["state"], "failed")
            self.assertEqual(payload["result"]["error"]["details"]["returncode"], 7)
            self.assertEqual(payload["result"]["error"]["details"]["stderr"], "launch failed")

    def test_create_worker_run_normalizes_worker_result_payload(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "build_runtime_model", return_value=_active_worker_model()),
        ):
            root = Path(tmpdir)
            command = root / "fake_hermes_result.py"
            command.write_text(
                "\n".join(
                    [
                        "import json, os",
                        "result_path = os.environ['SKILLBOX_WORKER_RESULT_PATH']",
                        "json.dump({",
                        "    'state': 'running',",
                        "    'summary': '',",
                        "    'findings': ['kept'],",
                        "    'actions_taken': ['checked'],",
                        "    'next_action': 'none',",
                        "}, open(result_path, 'w'))",
                    ]
                ),
                encoding="utf-8",
            )
            env = _no_hermes_env()
            env["SKILLBOX_WORKER_HERMES_COMMAND"] = f"{sys.executable} {command}"

            with mock.patch.dict("os.environ", env):
                payload = SHARED.create_worker_run(
                    root,
                    task_class="analysis",
                    instruction="Normalize result.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                )

            self.assertEqual(payload["state"], "succeeded")
            self.assertEqual(payload["result"]["findings"], ["kept"])
            self.assertEqual(payload["result"]["actions_taken"], ["checked"])
            self.assertEqual(payload["result"]["next_action"], "none")
            self.assertEqual(payload["artifacts"][0]["kind"], "summary")

    def test_create_worker_run_defaults_non_object_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result_path = root / "result.json"
            result_path.write_text("{}", encoding="utf-8")
            launch_paths = {
                "result_path": result_path,
                "summary_path": root / "summary.md",
            }
            payload = {"run_id": "wr_20260505_000000_abcdef"}
            with mock.patch.object(SHARED, "load_json_file", return_value=["bad"]):
                state, result, artifacts, learning_proposals = SHARED._worker_loaded_result(  # noqa: SLF001
                    payload,
                    launch_paths,
                )

            self.assertEqual(state, "succeeded")
            self.assertEqual(result["summary"], "Worker completed.")
            self.assertEqual(artifacts[0]["kind"], "summary")
            self.assertEqual(learning_proposals, [])

    def test_worker_summary_artifact_skips_blank_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            launch_paths = {"summary_path": Path(tmpdir) / "summary.md"}
            self.assertIsNone(  # noqa: SLF001
                SHARED._worker_summary_artifact(
                    {"run_id": "wr_20260505_000000_abcdef"},
                    launch_paths,
                    {"summary": ""},
                )
            )

    def test_create_worker_run_blocks_client_missing_from_active_runtime(self) -> None:
        model = {"clients": [{"id": "skills", "default_cwd_host_path": "/tmp/skills"}]}
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "build_runtime_model", return_value=model),
            mock.patch.object(SHARED.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
        ):
            root = Path(tmpdir)

            payload = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Reject inactive client.",
                client_id="skillbox",
                cwd="/tmp/skillbox",
            )

            self.assertEqual(payload["state"], "blocked")
            self.assertEqual(payload["context_state"], "unresolved")
            self.assertEqual(payload["run"]["blocked_reason"], "WORKER_CONTEXT_UNRESOLVED")
            self.assertEqual(payload["client_id"], "skillbox")
            self.assertIsNone(payload["resolved_context"])

    def test_create_worker_run_infers_active_client_from_cwd(self) -> None:
        model = {
            "clients": [
                {
                    "id": "skills",
                    "default_cwd_host_path": "/tmp/skills",
                    "context": {"cwd_match": ["/tmp/skills"]},
                }
            ]
        }
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "build_runtime_model", return_value=model),
            mock.patch.object(SHARED.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", _no_hermes_env()),
        ):
            root = Path(tmpdir)

            payload = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Infer context.",
                cwd="/tmp/skills/src",
            )

            self.assertEqual(payload["state"], "failed")
            self.assertEqual(payload["client_id"], "skills")
            self.assertEqual(payload["context_state"], "resolved")
            self.assertEqual(payload["resolved_context"]["effective_cwd"], "/tmp/skills/src")

    def test_create_worker_run_blocks_ambiguous_cwd_context(self) -> None:
        model = {
            "clients": [
                {"id": "one", "default_cwd_host_path": "/tmp/shared"},
                {"id": "two", "context": {"cwd_match": ["/tmp/shared"]}},
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(SHARED, "build_runtime_model", return_value=model):
            root = Path(tmpdir)

            payload = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Ambiguous context.",
                cwd="/tmp/shared/repo",
            )

            self.assertEqual(payload["state"], "blocked")
            self.assertEqual(payload["context_state"], "unresolved")
            self.assertEqual(payload["run"]["blocked_reason"], "WORKER_CONTEXT_UNRESOLVED")

    def test_worker_repo_id_resolution_prefers_explicit_and_exact_matches(self) -> None:
        self.assertEqual(SHARED._worker_repo_id({}, "repo-hint", "/tmp/repo"), "repo-hint")  # noqa: SLF001
        self.assertEqual(
            SHARED._worker_repo_id(  # noqa: SLF001
                {"repos": [{"id": "exact", "host_path": "/tmp/repo"}]},
                "",
                "/tmp/repo",
                "fallback",
            ),
            "exact",
        )
        self.assertEqual(
            SHARED._worker_repo_id(  # noqa: SLF001
                {"repos": [{"id": "parent", "host_path": "/tmp"}]},
                "",
                "/tmp/repo",
                "fallback",
            ),
            "fallback",
        )
        self.assertEqual(
            SHARED._worker_repo_id(  # noqa: SLF001
                {"repos": [{"id": "parent", "host_path": "/tmp"}]},
                "",
                "/tmp/repo",
            ),
            "parent",
        )
        self.assertEqual(SHARED._worker_repo_id({"repos": [None]}, "", "${ROOT}/repo", "fallback"), "fallback")  # noqa: SLF001

    def test_worker_launch_helpers_resolve_env_and_timeout_values(self) -> None:
        env = _no_hermes_env()
        env["SKILLBOX_WORKER_HERMES_BIN"] = "/opt/hermes/bin/hermes"
        env["SKILLBOX_WORKER_LAUNCH_TIMEOUT_SECONDS"] = "0.5"
        with mock.patch.dict("os.environ", env):
            self.assertEqual(SHARED._worker_hermes_command(), ["/opt/hermes/bin/hermes"])  # noqa: SLF001
            self.assertEqual(SHARED._worker_launch_timeout_seconds(), 1.0)  # noqa: SLF001

        with mock.patch.dict("os.environ", {"SKILLBOX_WORKER_LAUNCH_TIMEOUT_SECONDS": "bad"}):
            self.assertEqual(  # noqa: SLF001
                SHARED._worker_launch_timeout_seconds(),
                SHARED.WORKER_DEFAULT_LAUNCH_TIMEOUT_SECONDS,
            )

    def test_create_worker_run_rejects_invalid_instruction_and_repo_patch_policy(self) -> None:
        with self.assertRaises(SHARED.WorkerRuntimeError) as blank_error:
            SHARED.create_worker_run(Path("."), task_class="analysis", instruction=" ")
        self.assertEqual(blank_error.exception.code, "WORKER_POLICY_BLOCKED")

        with self.assertRaises(SHARED.WorkerRuntimeError) as patch_error:
            SHARED.create_worker_run(
                Path("."),
                task_class="analysis",
                instruction="Patch this repo.",
                write_scope="repo_patch",
            )
        self.assertEqual(patch_error.exception.code, "WORKER_POLICY_BLOCKED")

    def test_promote_worker_learning_validates_review_and_target_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            created = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Produces learning proposals.",
                client_id="personal",
                cwd=str(root),
            )
            run_path = Path(created["paths"]["run"])
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            saved["state"] = "review_pending"
            saved["run"]["state"] = "review_pending"
            saved["learning_proposals"] = [
                {
                    "proposal_id": "lp_pending",
                    "run_id": created["run_id"],
                    "target_kind": "skill",
                    "target_location": "opensource/skills/report-analyst",
                    "summary": "Needs review.",
                    "status": "pending_review",
                    "requires_review": True,
                },
                {
                    "proposal_id": "lp_approved",
                    "run_id": created["run_id"],
                    "target_kind": "skill",
                    "target_location": "opensource/skills/report-analyst",
                    "summary": "Approved.",
                    "status": "approved",
                    "requires_review": False,
                },
                {
                    "proposal_id": "lp_wrong_target",
                    "run_id": created["run_id"],
                    "target_kind": "skill",
                    "target_location": "opensource/skills/other",
                    "summary": "Approved for another target.",
                    "status": "approved",
                    "requires_review": False,
                },
            ]
            SHARED.write_json_file(run_path, saved)

            with self.assertRaises(SHARED.WorkerRuntimeError) as missing_id:
                SHARED.promote_worker_learning(
                    root,
                    proposal_id="",
                    approved_by="operator",
                    target_kind="skill",
                    target_location="opensource/skills/report-analyst",
                )
            self.assertEqual(missing_id.exception.code, "WORKER_WRITEBACK_REJECTED")

            with self.assertRaises(SHARED.WorkerRuntimeError) as missing_proposal:
                SHARED.promote_worker_learning(
                    root,
                    proposal_id="lp_missing",
                    approved_by="operator",
                    target_kind="skill",
                    target_location="opensource/skills/report-analyst",
                )
            self.assertEqual(missing_proposal.exception.code, "WORKER_WRITEBACK_REJECTED")

            with self.assertRaises(SHARED.WorkerRuntimeError) as pending_error:
                SHARED.promote_worker_learning(
                    root,
                    proposal_id="lp_pending",
                    approved_by="operator",
                    target_kind="skill",
                    target_location="opensource/skills/report-analyst",
                )
            self.assertEqual(pending_error.exception.code, "WORKER_LEARNING_REVIEW_REQUIRED")

            with self.assertRaises(SHARED.WorkerRuntimeError) as mode_error:
                SHARED.promote_worker_learning(
                    root,
                    proposal_id="lp_approved",
                    approved_by="operator",
                    target_kind="skill",
                    target_location="opensource/skills/report-analyst",
                    promotion_mode="copy",
                )
            self.assertEqual(mode_error.exception.code, "WORKER_WRITEBACK_REJECTED")

            with self.assertRaises(SHARED.WorkerRuntimeError) as target_error:
                SHARED.promote_worker_learning(
                    root,
                    proposal_id="lp_wrong_target",
                    approved_by="operator",
                    target_kind="skill",
                    target_location="opensource/skills/report-analyst",
                )
            self.assertEqual(target_error.exception.code, "WORKER_WRITEBACK_REJECTED")

            promoted = SHARED.promote_worker_learning(
                root,
                proposal_id="lp_approved",
                approved_by="operator",
                target_kind="skill",
                target_location="opensource/skills/report-analyst",
            )
            self.assertEqual(promoted["status"], "promoted")
            promoted_again = SHARED.promote_worker_learning(
                root,
                proposal_id="lp_approved",
                approved_by="operator",
                target_kind="skill",
                target_location="opensource/skills/report-analyst",
            )
            self.assertEqual(promoted_again["status"], "promoted")

    def test_worker_submit_cli_persists_blocked_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(root),
                    "worker-submit",
                    "analysis",
                    "No context should block.",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["state"], "blocked")
            self.assertEqual(payload["run"]["blocked_reason"], "WORKER_CONTEXT_UNRESOLVED")
            self.assertFalse(payload["launch"]["attempted"])
            self.assertTrue(Path(payload["paths"]["run"]).is_file())

    def test_worker_submit_cli_rejects_unknown_runtime_with_stable_worker_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(root),
                    "worker-submit",
                    "analysis",
                    "Reject this runtime.",
                    "--client",
                    "personal",
                    "--runtime",
                    "not-real",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "WORKER_RUNTIME_UNKNOWN")
            self.assertIn("not-real", payload["error"]["message"])

    def test_worker_submit_cli_rejects_repo_patch_without_launch_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(root),
                    "worker-submit",
                    "analysis",
                    "Patch this repo.",
                    "--client",
                    "personal",
                    "--cwd",
                    str(root),
                    "--write-scope",
                    "repo_patch",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "WORKER_POLICY_BLOCKED")
            self.assertEqual(payload["error"]["details"]["field"], "write_scope")

    def test_worker_status_cli_reads_persisted_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            created = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Inspect this run.",
                client_id="personal",
                cwd=str(root),
            )

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(root),
                    "worker-status",
                    created["run_id"],
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["run_id"], created["run_id"])
            self.assertEqual(payload["state"], "queued")
            self.assertFalse(payload["artifacts_ready"])

    def test_worker_artifacts_cli_rejects_non_terminal_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            created = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Not done yet.",
                client_id="personal",
                cwd=str(root),
            )

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(root),
                    "worker-artifacts",
                    created["run_id"],
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "WORKER_RESULT_NOT_READY")

    def test_worker_status_cli_rejects_malformed_run_id_with_stable_worker_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(root),
                    "worker-status",
                    "not-a-run-id",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "WORKER_RUN_NOT_FOUND")

    def test_worker_artifacts_cli_returns_terminal_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            created = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Done run.",
                client_id="personal",
                cwd=str(root),
            )
            run_path = Path(created["paths"]["run"])
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            saved["state"] = "succeeded"
            saved["run"]["state"] = "succeeded"
            saved["run"]["finished_at"] = 1.0
            saved["result"] = {
                "run_id": created["run_id"],
                "state": "succeeded",
                "summary": "Completed.",
                "findings": [],
                "actions_taken": [],
                "next_action": "",
            }
            saved["artifacts"] = [
                {
                    "artifact_id": "art_001",
                    "run_id": created["run_id"],
                    "kind": "summary",
                    "path": "invocations/summary.md",
                    "mime_type": "text/markdown",
                    "summary": "Summary",
                }
            ]
            SHARED.write_json_file(run_path, saved)

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(root),
                    "worker-artifacts",
                    created["run_id"],
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["artifacts"][0]["artifact_id"], "art_001")
            self.assertEqual(payload["result"]["summary"], "Completed.")

    def test_worker_promote_learning_requires_review_for_pending_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            created = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Produces learning.",
                client_id="personal",
                cwd=str(root),
            )
            run_path = Path(created["paths"]["run"])
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            saved["state"] = "review_pending"
            saved["run"]["state"] = "review_pending"
            saved["learning_proposals"] = [
                {
                    "proposal_id": "lp_001",
                    "run_id": created["run_id"],
                    "target_kind": "skill",
                    "target_location": "opensource/skills/report-analyst",
                    "summary": "Add checklist.",
                    "status": "pending_review",
                    "requires_review": True,
                }
            ]
            SHARED.write_json_file(run_path, saved)

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(root),
                    "worker-promote-learning",
                    "lp_001",
                    "--approved-by",
                    "operator",
                    "--target-kind",
                    "skill",
                    "--target-location",
                    "opensource/skills/report-analyst",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["type"], "WORKER_LEARNING_REVIEW_REQUIRED")

    def test_worker_promote_learning_records_approved_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            created = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Produces approved learning.",
                client_id="personal",
                cwd=str(root),
            )
            run_path = Path(created["paths"]["run"])
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            saved["state"] = "review_pending"
            saved["run"]["state"] = "review_pending"
            saved["learning_proposals"] = [
                {
                    "proposal_id": "lp_002",
                    "run_id": created["run_id"],
                    "target_kind": "skill",
                    "target_location": "opensource/skills/report-analyst",
                    "summary": "Add checklist.",
                    "status": "approved",
                    "requires_review": False,
                }
            ]
            SHARED.write_json_file(run_path, saved)

            result = subprocess.run(
                [
                    "python3",
                    str(MANAGER),
                    "--root-dir",
                    str(root),
                    "worker-promote-learning",
                    "lp_002",
                    "--approved-by",
                    "operator",
                    "--target-kind",
                    "skill",
                    "--target-location",
                    "opensource/skills/report-analyst",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["proposal_id"], "lp_002")
            self.assertEqual(payload["status"], "promoted")
            saved_after = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_after["learning_proposals"][0]["status"], "promoted")

    def test_worker_cli_handlers_emit_json_without_subprocess_coverage_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            submit_args = CLI._build_parser().parse_args(  # noqa: SLF001
                [
                    "worker-submit",
                    "analysis",
                    "Direct submit handler.",
                    "--client",
                    "personal",
                    "--cwd",
                    str(root),
                    "--format",
                    "json",
                ]
            )
            submit_out = StringIO()
            with redirect_stdout(submit_out):
                submit_code = CLI._handle_worker_submit(submit_args, root)  # noqa: SLF001
            self.assertEqual(submit_code, 0)
            self.assertEqual(json.loads(submit_out.getvalue())["state"], "queued")

            created = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Direct handler coverage.",
                client_id="personal",
                cwd=str(root),
            )

            status_args = CLI._build_parser().parse_args(  # noqa: SLF001
                ["worker-status", created["run_id"], "--format", "json"]
            )
            status_out = StringIO()
            with redirect_stdout(status_out):
                status_code = CLI._handle_worker_status(status_args, root)  # noqa: SLF001
            self.assertEqual(status_code, 0)
            self.assertEqual(json.loads(status_out.getvalue())["state"], "queued")

            artifacts_args = CLI._build_parser().parse_args(  # noqa: SLF001
                ["worker-artifacts", created["run_id"], "--format", "json"]
            )
            artifacts_out = StringIO()
            with redirect_stdout(artifacts_out):
                artifacts_code = CLI._handle_worker_artifacts(artifacts_args, root)  # noqa: SLF001
            self.assertEqual(artifacts_code, 1)
            self.assertEqual(
                json.loads(artifacts_out.getvalue())["error"]["type"],
                "WORKER_RESULT_NOT_READY",
            )

            run_path = Path(created["paths"]["run"])
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            saved["state"] = "succeeded"
            saved["run"]["state"] = "succeeded"
            saved["result"] = {
                "run_id": created["run_id"],
                "state": "succeeded",
                "summary": "Direct artifact success.",
                "findings": [],
                "actions_taken": [],
                "next_action": "",
            }
            saved["artifacts"] = [
                {
                    "artifact_id": "art_direct",
                    "run_id": created["run_id"],
                    "kind": "summary",
                    "path": "summary.md",
                    "mime_type": "text/markdown",
                    "summary": "Direct summary",
                }
            ]
            saved["learning_proposals"] = [
                {
                    "proposal_id": "lp_direct",
                    "run_id": created["run_id"],
                    "target_kind": "skill",
                    "target_location": "opensource/skills/report-analyst",
                    "summary": "Direct proposal",
                    "status": "approved",
                    "requires_review": False,
                }
            ]
            SHARED.write_json_file(run_path, saved)

            artifacts_success_out = StringIO()
            with redirect_stdout(artifacts_success_out):
                artifacts_success_code = CLI._handle_worker_artifacts(artifacts_args, root)  # noqa: SLF001
            self.assertEqual(artifacts_success_code, 0)
            self.assertEqual(
                json.loads(artifacts_success_out.getvalue())["artifacts"][0]["artifact_id"],
                "art_direct",
            )

            promote_args = CLI._build_parser().parse_args(  # noqa: SLF001
                [
                    "worker-promote-learning",
                    "lp_direct",
                    "--approved-by",
                    "operator",
                    "--target-kind",
                    "skill",
                    "--target-location",
                    "opensource/skills/report-analyst",
                    "--format",
                    "json",
                ]
            )
            promote_out = StringIO()
            with redirect_stdout(promote_out):
                promote_code = CLI._handle_worker_promote_learning(promote_args, root)  # noqa: SLF001
            self.assertEqual(promote_code, 0)
            self.assertEqual(json.loads(promote_out.getvalue())["status"], "promoted")

    def test_worker_submit_handler_reports_structured_runtime_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            submit_args = CLI._build_parser().parse_args(  # noqa: SLF001
                [
                    "worker-submit",
                    "analysis",
                    "Bad runtime.",
                    "--runtime",
                    "not-real",
                    "--format",
                    "json",
                ]
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = CLI._handle_worker_submit(submit_args, root)  # noqa: SLF001
            self.assertEqual(exit_code, 1)
            self.assertEqual(json.loads(stdout.getvalue())["error"]["type"], "WORKER_RUNTIME_UNKNOWN")

    def test_worker_cli_handlers_emit_text_for_human_terminal_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            submit_args = CLI._build_parser().parse_args(  # noqa: SLF001
                [
                    "worker-submit",
                    "analysis",
                    "Text submit handler.",
                    "--client",
                    "personal",
                    "--cwd",
                    str(root),
                ]
            )
            submit_out = StringIO()
            with redirect_stdout(submit_out):
                self.assertEqual(CLI._handle_worker_submit(submit_args, root), 0)  # noqa: SLF001
            self.assertIn("worker run: wr_", submit_out.getvalue())
            self.assertIn("context: resolving", submit_out.getvalue())

            created = SHARED.create_worker_run(
                root,
                task_class="analysis",
                instruction="Text handler coverage.",
                client_id="personal",
                cwd=str(root),
            )
            status_args = CLI._build_parser().parse_args(["worker-status", created["run_id"]])  # noqa: SLF001
            status_out = StringIO()
            with redirect_stdout(status_out):
                self.assertEqual(CLI._handle_worker_status(status_args, root), 0)  # noqa: SLF001
            self.assertIn(f"worker run: {created['run_id']}", status_out.getvalue())
            self.assertIn("state: queued", status_out.getvalue())

            run_path = Path(created["paths"]["run"])
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            saved["state"] = "succeeded"
            saved["run"]["state"] = "succeeded"
            saved["result"] = {
                "run_id": created["run_id"],
                "state": "succeeded",
                "summary": "Terminal summary.",
                "findings": [],
                "actions_taken": [],
                "next_action": "",
            }
            saved["artifacts"] = [
                {
                    "artifact_id": "art_text",
                    "run_id": created["run_id"],
                    "kind": "summary",
                    "path": "summary.md",
                    "mime_type": "text/markdown",
                    "summary": "Text summary",
                }
            ]
            saved["learning_proposals"] = [
                {
                    "proposal_id": "lp_text",
                    "run_id": created["run_id"],
                    "target_kind": "skill",
                    "target_location": "opensource/skills/report-analyst",
                    "summary": "Text proposal",
                    "status": "approved",
                    "requires_review": False,
                }
            ]
            SHARED.write_json_file(run_path, saved)

            artifacts_args = CLI._build_parser().parse_args(["worker-artifacts", created["run_id"]])  # noqa: SLF001
            artifacts_out = StringIO()
            with redirect_stdout(artifacts_out):
                self.assertEqual(CLI._handle_worker_artifacts(artifacts_args, root), 0)  # noqa: SLF001
            self.assertIn("artifacts: 1", artifacts_out.getvalue())
            self.assertIn("summary: Terminal summary.", artifacts_out.getvalue())

            promote_args = CLI._build_parser().parse_args(  # noqa: SLF001
                [
                    "worker-promote-learning",
                    "lp_text",
                    "--approved-by",
                    "operator",
                    "--target-kind",
                    "skill",
                    "--target-location",
                    "opensource/skills/report-analyst",
                ]
            )
            promote_out = StringIO()
            with redirect_stdout(promote_out):
                self.assertEqual(CLI._handle_worker_promote_learning(promote_args, root), 0)  # noqa: SLF001
            self.assertIn("proposal: lp_text", promote_out.getvalue())
            self.assertIn("status: promoted", promote_out.getvalue())

            bad_args = CLI._build_parser().parse_args(  # noqa: SLF001
                ["worker-submit", "analysis", "Bad runtime text.", "--runtime", "not-real"]
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(CLI._handle_worker_submit(bad_args, root), 1)  # noqa: SLF001
            self.assertIn("Unsupported worker runtime", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
