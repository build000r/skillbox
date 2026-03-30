from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
MANAGER = ROOT_DIR / ".env-manager" / "manage.py"


class RuntimeManagerTests(unittest.TestCase):
    def test_sync_creates_managed_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "sync")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo / "repos").is_dir())
            self.assertTrue((repo / "logs" / "runtime").is_dir())
            self.assertTrue((repo / "logs" / "api").is_dir())
            self.assertTrue((repo / "logs" / "web").is_dir())

    def test_render_resolves_runtime_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "render", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            repos = {item["id"]: item for item in payload["repos"]}
            self.assertEqual(repos["skillbox-self"]["path"], "/workspace")
            self.assertEqual(repos["managed-repos"]["path"], "/workspace/repos")

    def test_doctor_warns_before_sync_and_passes_after_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            before = self._run(repo, "doctor", "--format", "json")
            self.assertEqual(before.returncode, 0, before.stderr)
            before_results = json.loads(before.stdout)
            warning_codes = {item["code"] for item in before_results if item["status"] == "warn"}
            self.assertIn("runtime-log-paths", warning_codes)

            sync = self._run(repo, "sync")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            after = self._run(repo, "doctor", "--format", "json")
            self.assertEqual(after.returncode, 0, after.stderr)
            after_results = json.loads(after.stdout)
            warning_codes = {item["code"] for item in after_results if item["status"] == "warn"}
            self.assertNotIn("runtime-log-paths", warning_codes)

    def _run(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(MANAGER), "--root-dir", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_fixture(self, repo: Path) -> None:
        (repo / ".env.example").write_text(
            "SKILLBOX_NAME=skillbox\n"
            "SKILLBOX_WORKSPACE_ROOT=/workspace\n"
            "SKILLBOX_REPOS_ROOT=/workspace/repos\n"
            "SKILLBOX_SKILLS_ROOT=/workspace/skills\n"
            "SKILLBOX_LOG_ROOT=/workspace/logs\n"
            "SKILLBOX_HOME_ROOT=/home/sandbox\n"
            "SKILLBOX_API_PORT=8000\n"
            "SKILLBOX_WEB_PORT=3000\n",
            encoding="utf-8",
        )

        (repo / "workspace").mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "runtime.yaml").write_text(
            "version: 1\n"
            "repos:\n"
            "  - id: skillbox-self\n"
            "    kind: repo\n"
            "    path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "    required: true\n"
            "    source:\n"
            "      kind: bind\n"
            "      path: ${ROOT_DIR}\n"
            "    sync:\n"
            "      mode: external\n"
            "  - id: managed-repos\n"
            "    kind: workspace-root\n"
            "    path: ${SKILLBOX_REPOS_ROOT}\n"
            "    required: true\n"
            "    source:\n"
            "      kind: directory\n"
            "    sync:\n"
            "      mode: ensure-directory\n"
            "services:\n"
            "  - id: internal-env-manager\n"
            "    kind: orchestration\n"
            "    repo: skillbox-self\n"
            "    path: ${SKILLBOX_WORKSPACE_ROOT}/.env-manager\n"
            "    required: true\n"
            "    command: python3 .env-manager/manage.py\n"
            "    log: runtime\n"
            "logs:\n"
            "  - id: runtime\n"
            "    path: ${SKILLBOX_LOG_ROOT}/runtime\n"
            "  - id: api\n"
            "    path: ${SKILLBOX_LOG_ROOT}/api\n"
            "  - id: web\n"
            "    path: ${SKILLBOX_LOG_ROOT}/web\n"
            "checks:\n"
            "  - id: workspace-root\n"
            "    type: path_exists\n"
            "    path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "    required: true\n"
            "  - id: repos-root\n"
            "    type: path_exists\n"
            "    path: ${SKILLBOX_REPOS_ROOT}\n"
            "    required: true\n"
            "  - id: skills-root\n"
            "    type: path_exists\n"
            "    path: ${SKILLBOX_SKILLS_ROOT}\n"
            "    required: true\n"
            "  - id: log-root\n"
            "    type: path_exists\n"
            "    path: ${SKILLBOX_LOG_ROOT}\n"
            "    required: true\n",
            encoding="utf-8",
        )

        (repo / "skills").mkdir(parents=True, exist_ok=True)
        (repo / "logs").mkdir(parents=True, exist_ok=True)
        (repo / "repos").mkdir(parents=True, exist_ok=True)
        (repo / ".env-manager").mkdir(parents=True, exist_ok=True)
        (repo / ".env-manager" / "manage.py").write_text("# stub\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
