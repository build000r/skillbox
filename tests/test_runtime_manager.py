from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
MANAGER = ROOT_DIR / ".env-manager" / "manage.py"


class RuntimeManagerTests(unittest.TestCase):
    def test_sync_creates_core_runtime_state_and_installs_default_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "sync", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            actions = payload["actions"]
            self.assertTrue((repo / "repos").is_dir())
            self.assertTrue((repo / "logs" / "runtime").is_dir())
            self.assertTrue((repo / "logs" / "repos").is_dir())
            self.assertFalse((repo / "logs" / "api").exists())
            self.assertFalse((repo / "logs" / "web").exists())
            self.assertTrue((repo / "home" / ".claude" / "skills" / "sample-skill" / "SKILL.md").is_file())
            self.assertTrue((repo / "home" / ".codex" / "skills" / "sample-skill" / "SKILL.md").is_file())
            self.assertFalse((repo / "home" / ".claude" / "skills" / "personal-skill").exists())
            self.assertTrue((repo / "workspace" / "default-skills.lock.json").is_file())
            self.assertTrue(any("install-skill:" in action for action in actions))
            self.assertTrue(any("write-lockfile:" in action for action in actions))

    def test_render_resolves_runtime_placeholders_and_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "render", "--format", "json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            repos = {item["id"]: item for item in payload["repos"]}
            skills = {item["id"]: item for item in payload["skills"]}
            self.assertEqual(payload["active_profiles"], ["core"])
            self.assertEqual(payload["active_clients"], [])
            self.assertEqual({client["id"] for client in payload["clients"]}, {"personal", "vibe-coding-client"})
            self.assertEqual(repos["skillbox-self"]["path"], "/workspace")
            self.assertEqual(repos["managed-repos"]["path"], "/workspace/repos")
            self.assertEqual(skills["default-skills"]["bundle_dir"], "/workspace/default-skills")
            self.assertEqual(
                skills["default-skills"]["install_targets"][0]["path"],
                "/home/sandbox/.claude/skills",
            )

    def test_doctor_warns_before_sync_and_passes_after_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            before = self._run(repo, "doctor", "--format", "json")
            self.assertEqual(before.returncode, 0, before.stderr)
            before_results = json.loads(before.stdout)
            before_warning_codes = {item["code"] for item in before_results if item["status"] == "warn"}
            self.assertIn("runtime-log-paths", before_warning_codes)
            self.assertIn("skill-lock-state", before_warning_codes)
            self.assertIn("skill-install-state", before_warning_codes)

            sync = self._run(repo, "sync")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            after = self._run(repo, "doctor", "--format", "json")
            self.assertEqual(after.returncode, 0, after.stderr)
            after_results = json.loads(after.stdout)
            after_warning_codes = {item["code"] for item in after_results if item["status"] == "warn"}
            after_failure_codes = {item["code"] for item in after_results if item["status"] == "fail"}
            self.assertNotIn("runtime-log-paths", after_warning_codes)
            self.assertNotIn("skill-lock-state", after_warning_codes)
            self.assertNotIn("skill-install-state", after_warning_codes)
            self.assertEqual(after_failure_codes, set(), after_results)

    def test_status_reports_installed_skill_targets_and_lock_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            sync = self._run(repo, "sync")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            status = self._run(repo, "status", "--format", "json")
            self.assertEqual(status.returncode, 0, status.stderr)
            payload = json.loads(status.stdout)
            skillset = payload["skills"][0]
            skill_entry = skillset["skills"][0]
            target_states = {target["id"]: target["state"] for target in skill_entry["targets"]}

            self.assertEqual(payload["active_profiles"], ["core"])
            self.assertEqual(payload["active_clients"], [])
            self.assertTrue(skillset["lock_present"])
            self.assertEqual(skill_entry["name"], "sample-skill")
            self.assertEqual(target_states["claude"], "ok")
            self.assertEqual(target_states["codex"], "ok")

    def test_client_selection_activates_client_repo_root_logs_and_skill_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            render = self._run(repo, "render", "--client", "personal", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            render_payload = json.loads(render.stdout)
            self.assertEqual(render_payload["active_profiles"], ["core"])
            self.assertEqual(render_payload["active_clients"], ["personal"])
            self.assertEqual(
                {item["id"] for item in render_payload["repos"]},
                {"skillbox-self", "managed-repos", "personal-root"},
            )
            self.assertEqual(
                {item["id"] for item in render_payload["skills"]},
                {"default-skills", "personal-skills"},
            )
            self.assertEqual(
                {item["id"] for item in render_payload["logs"]},
                {"runtime", "repos", "personal"},
            )

            sync = self._run(repo, "sync", "--client", "personal", "--format", "json")

            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / "logs" / "clients" / "personal").is_dir())
            self.assertTrue((repo / "workspace" / "clients" / "personal" / "skills.lock.json").is_file())
            self.assertTrue((repo / "home" / ".claude" / "skills" / "personal-skill" / "SKILL.md").is_file())
            self.assertTrue((repo / "home" / ".codex" / "skills" / "personal-skill" / "SKILL.md").is_file())

    def test_profile_selection_limits_optional_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            render = self._run(repo, "render", "--profile", "surfaces", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            payload = json.loads(render.stdout)
            self.assertEqual(payload["active_profiles"], ["core", "surfaces"])
            self.assertEqual({item["id"] for item in payload["services"]}, {"internal-env-manager", "api-stub", "web-stub"})
            self.assertEqual({item["id"] for item in payload["logs"]}, {"runtime", "repos", "api", "web"})

    def test_profile_selection_activates_swimmers_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)
            (repo / "monoserver-host" / "swimmers").mkdir(parents=True, exist_ok=True)

            render = self._run(repo, "render", "--profile", "swimmers", "--format", "json")

            self.assertEqual(render.returncode, 0, render.stderr)
            payload = json.loads(render.stdout)
            self.assertEqual(payload["active_profiles"], ["core", "swimmers"])
            self.assertEqual(
                {item["id"] for item in payload["repos"]},
                {"skillbox-self", "managed-repos", "swimmers-repo"},
            )
            self.assertEqual(
                {item["id"] for item in payload["services"]},
                {"internal-env-manager", "swimmers-server"},
            )
            self.assertEqual(
                {item["id"] for item in payload["logs"]},
                {"runtime", "repos", "swimmers"},
            )

            sync = self._run(repo, "sync", "--profile", "swimmers", "--format", "json")
            self.assertEqual(sync.returncode, 0, sync.stderr)
            self.assertTrue((repo / "logs" / "swimmers").is_dir())

    def test_doctor_fails_when_selected_client_root_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            result = self._run(repo, "doctor", "--client", "vibe-coding-client", "--format", "json")

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            failure_codes = {item["code"] for item in payload if item["status"] == "fail"}
            self.assertIn("required-runtime-paths", failure_codes)
            self.assertIn("required-runtime-checks", failure_codes)

    def test_doctor_fails_when_declared_bundle_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo, include_bundle=False)

            result = self._run(repo, "doctor", "--format", "json")

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            failure_codes = {item["code"] for item in payload if item["status"] == "fail"}
            self.assertIn("skill-bundle-state", failure_codes)

    def test_doctor_fails_when_installed_skill_drifts_from_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_fixture(repo)

            sync = self._run(repo, "sync")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            (repo / "home" / ".claude" / "skills" / "sample-skill" / "SKILL.md").write_text(
                "---\nname: sample-skill\ndescription: drifted\n---\n",
                encoding="utf-8",
            )

            result = self._run(repo, "doctor", "--format", "json")

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            install_failures = [
                item for item in payload if item["status"] == "fail" and item["code"] == "skill-install-state"
            ]
            self.assertEqual(len(install_failures), 1, payload)
            self.assertIn("claude", " ".join(install_failures[0]["details"]["issues"]))

    def _run(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(MANAGER), "--root-dir", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_fixture(self, repo: Path, include_bundle: bool = True) -> None:
        (repo / ".env.example").write_text(
            "SKILLBOX_NAME=skillbox\n"
            "SKILLBOX_WORKSPACE_ROOT=/workspace\n"
            "SKILLBOX_REPOS_ROOT=/workspace/repos\n"
            "SKILLBOX_SKILLS_ROOT=/workspace/skills\n"
            "SKILLBOX_LOG_ROOT=/workspace/logs\n"
            "SKILLBOX_HOME_ROOT=/home/sandbox\n"
            "SKILLBOX_MONOSERVER_ROOT=/monoserver\n"
            "SKILLBOX_MONOSERVER_HOST_ROOT=./monoserver-host\n"
            "SKILLBOX_API_PORT=8000\n"
            "SKILLBOX_WEB_PORT=3000\n"
            "SKILLBOX_SWIMMERS_PORT=3210\n"
            "SKILLBOX_SWIMMERS_PUBLISH_HOST=127.0.0.1\n"
            "SKILLBOX_SWIMMERS_REPO=/monoserver/swimmers\n"
            "SKILLBOX_SWIMMERS_INSTALL_DIR=/home/sandbox/.local/bin\n"
            "SKILLBOX_SWIMMERS_BIN=/home/sandbox/.local/bin/swimmers\n"
            "SKILLBOX_SWIMMERS_DOWNLOAD_URL=\n"
            "SKILLBOX_SWIMMERS_AUTH_MODE=\n"
            "SKILLBOX_SWIMMERS_AUTH_TOKEN=\n"
            "SKILLBOX_SWIMMERS_OBSERVER_TOKEN=\n",
            encoding="utf-8",
        )

        (repo / "workspace" / "runtime.yaml").parent.mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "runtime.yaml").write_text(
            "version: 2\n"
            "selection: {}\n"
            "core:\n"
            "  repos:\n"
            "    - id: skillbox-self\n"
            "      kind: repo\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: bind\n"
            "        path: ${ROOT_DIR}\n"
            "      sync:\n"
            "        mode: external\n"
            "    - id: managed-repos\n"
            "      kind: workspace-root\n"
            "      path: ${SKILLBOX_REPOS_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      source:\n"
            "        kind: directory\n"
            "      sync:\n"
            "        mode: ensure-directory\n"
            "    - id: swimmers-repo\n"
            "      kind: repo\n"
            "      path: ${SKILLBOX_SWIMMERS_REPO}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - swimmers\n"
            "      source:\n"
            "        kind: bind\n"
            "      sync:\n"
            "        mode: external\n"
            "  skills:\n"
            "    - id: default-skills\n"
            "      kind: packaged-skill-set\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      bundle_dir: ${SKILLBOX_WORKSPACE_ROOT}/default-skills\n"
            "      manifest: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.manifest\n"
            "      sources_config: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.sources.yaml\n"
            "      lock_path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.lock.json\n"
            "      sync:\n"
            "        mode: unpack-bundles\n"
            "      install_targets:\n"
            "        - id: claude\n"
            "          path: ${SKILLBOX_HOME_ROOT}/.claude/skills\n"
            "        - id: codex\n"
            "          path: ${SKILLBOX_HOME_ROOT}/.codex/skills\n"
            "  services:\n"
            "    - id: internal-env-manager\n"
            "      kind: orchestration\n"
            "      repo: skillbox-self\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}/.env-manager\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "      command: python3 .env-manager/manage.py\n"
            "      log: runtime\n"
            "    - id: api-stub\n"
            "      kind: http\n"
            "      repo: skillbox-self\n"
            "      required: false\n"
            "      profiles:\n"
            "        - surfaces\n"
            "      command: python3 -m http.server 8000\n"
            "      healthcheck:\n"
            "        type: path_exists\n"
            "        path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "      log: api\n"
            "    - id: web-stub\n"
            "      kind: http\n"
            "      repo: skillbox-self\n"
            "      required: false\n"
            "      profiles:\n"
            "        - surfaces\n"
            "      command: python3 -m http.server 3000\n"
            "      healthcheck:\n"
            "        type: path_exists\n"
            "        path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "      log: web\n"
            "    - id: swimmers-server\n"
            "      kind: tmux-api\n"
            "      repo: swimmers-repo\n"
            "      path: ${SKILLBOX_SWIMMERS_REPO}\n"
            "      required: false\n"
            "      profiles:\n"
            "        - swimmers\n"
            "      command: /workspace/scripts/05-swimmers.sh --inside start\n"
            "      healthcheck:\n"
            "        type: path_exists\n"
            "        path: ${SKILLBOX_LOG_ROOT}/swimmers/swimmers-server.pid\n"
            "      log: swimmers\n"
            "  logs:\n"
            "    - id: runtime\n"
            "      path: ${SKILLBOX_LOG_ROOT}/runtime\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: repos\n"
            "      path: ${SKILLBOX_LOG_ROOT}/repos\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: api\n"
            "      path: ${SKILLBOX_LOG_ROOT}/api\n"
            "      profiles:\n"
            "        - surfaces\n"
            "    - id: web\n"
            "      path: ${SKILLBOX_LOG_ROOT}/web\n"
            "      profiles:\n"
            "        - surfaces\n"
            "    - id: swimmers\n"
            "      path: ${SKILLBOX_LOG_ROOT}/swimmers\n"
            "      profiles:\n"
            "        - swimmers\n"
            "  checks:\n"
            "    - id: workspace-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: repos-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_REPOS_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: skills-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_SKILLS_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: log-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_LOG_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: monoserver-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_MONOSERVER_ROOT}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: runtime-manager\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_WORKSPACE_ROOT}/.env-manager/manage.py\n"
            "      required: true\n"
            "      profiles:\n"
            "        - core\n"
            "    - id: swimmers-repo-root\n"
            "      type: path_exists\n"
            "      path: ${SKILLBOX_SWIMMERS_REPO}\n"
            "      required: true\n"
            "      profiles:\n"
            "        - swimmers\n"
            "clients:\n"
            "  - id: personal\n"
            "    label: Personal\n"
            "    default_cwd: ${SKILLBOX_MONOSERVER_ROOT}\n"
            "    repo_roots:\n"
            "      - id: personal-root\n"
            "        path: ${SKILLBOX_MONOSERVER_ROOT}\n"
            "        required: true\n"
            "        profiles:\n"
            "          - core\n"
            "        source:\n"
            "          kind: bind\n"
            "        sync:\n"
            "          mode: external\n"
            "    skills:\n"
            "      - id: personal-skills\n"
            "        kind: packaged-skill-set\n"
            "        required: false\n"
            "        profiles:\n"
            "          - core\n"
            "        bundle_dir: ${SKILLBOX_WORKSPACE_ROOT}/default-skills/clients/personal\n"
            "        manifest: ${SKILLBOX_WORKSPACE_ROOT}/workspace/clients/personal/skills.manifest\n"
            "        sources_config: ${SKILLBOX_WORKSPACE_ROOT}/workspace/clients/personal/skills.sources.yaml\n"
            "        lock_path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/clients/personal/skills.lock.json\n"
            "        sync:\n"
            "          mode: unpack-bundles\n"
            "        install_targets:\n"
            "          - id: claude\n"
            "            path: ${SKILLBOX_HOME_ROOT}/.claude/skills\n"
            "          - id: codex\n"
            "            path: ${SKILLBOX_HOME_ROOT}/.codex/skills\n"
            "    logs:\n"
            "      - id: personal\n"
            "        path: ${SKILLBOX_LOG_ROOT}/clients/personal\n"
            "        profiles:\n"
            "          - core\n"
            "    checks:\n"
            "      - id: personal-root\n"
            "        type: path_exists\n"
            "        path: ${SKILLBOX_MONOSERVER_ROOT}\n"
            "        required: true\n"
            "        profiles:\n"
            "          - core\n"
            "  - id: vibe-coding-client\n"
            "    label: Vibe Coding Client\n"
            "    default_cwd: ${SKILLBOX_MONOSERVER_ROOT}/vibe-coding-client\n"
            "    repo_roots:\n"
            "      - id: vibe-coding-client-root\n"
            "        path: ${SKILLBOX_MONOSERVER_ROOT}/vibe-coding-client\n"
            "        required: true\n"
            "        profiles:\n"
            "          - core\n"
            "        source:\n"
            "          kind: bind\n"
            "        sync:\n"
            "          mode: external\n"
            "    skills:\n"
            "      - id: vibe-coding-client-skills\n"
            "        kind: packaged-skill-set\n"
            "        required: false\n"
            "        profiles:\n"
            "          - core\n"
            "        bundle_dir: ${SKILLBOX_WORKSPACE_ROOT}/default-skills/clients/vibe-coding-client\n"
            "        manifest: ${SKILLBOX_WORKSPACE_ROOT}/workspace/clients/vibe-coding-client/skills.manifest\n"
            "        sources_config: ${SKILLBOX_WORKSPACE_ROOT}/workspace/clients/vibe-coding-client/skills.sources.yaml\n"
            "        lock_path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/clients/vibe-coding-client/skills.lock.json\n"
            "        sync:\n"
            "          mode: unpack-bundles\n"
            "        install_targets:\n"
            "          - id: claude\n"
            "            path: ${SKILLBOX_HOME_ROOT}/.claude/skills\n"
            "          - id: codex\n"
            "            path: ${SKILLBOX_HOME_ROOT}/.codex/skills\n"
            "    logs:\n"
            "      - id: vibe-coding-client\n"
            "        path: ${SKILLBOX_LOG_ROOT}/clients/vibe-coding-client\n"
            "        profiles:\n"
            "          - core\n"
            "    checks:\n"
            "      - id: vibe-coding-client-root\n"
            "        type: path_exists\n"
            "        path: ${SKILLBOX_MONOSERVER_ROOT}/vibe-coding-client\n"
            "        required: true\n"
            "        profiles:\n"
            "          - core\n",
            encoding="utf-8",
        )

        (repo / "workspace" / "default-skills.manifest").write_text("sample-skill\n", encoding="utf-8")
        (repo / "workspace" / "default-skills.sources.yaml").write_text(
            "version: 1\n"
            "sources:\n"
            "  - kind: local\n"
            "    path: ./skills\n",
            encoding="utf-8",
        )
        (repo / "workspace" / "clients" / "personal").mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "clients" / "personal" / "skills.manifest").write_text(
            "personal-skill\n",
            encoding="utf-8",
        )
        (repo / "workspace" / "clients" / "personal" / "skills.sources.yaml").write_text(
            "version: 1\n"
            "sources:\n"
            "  - kind: local\n"
            "    path: ./skills/clients/personal\n",
            encoding="utf-8",
        )
        (repo / "workspace" / "clients" / "vibe-coding-client").mkdir(parents=True, exist_ok=True)
        (repo / "workspace" / "clients" / "vibe-coding-client" / "skills.manifest").write_text("", encoding="utf-8")
        (repo / "workspace" / "clients" / "vibe-coding-client" / "skills.sources.yaml").write_text(
            "version: 1\n"
            "sources:\n"
            "  - kind: local\n"
            "    path: ./skills/clients/vibe-coding-client\n",
            encoding="utf-8",
        )

        (repo / "default-skills").mkdir(parents=True, exist_ok=True)
        (repo / "default-skills" / "clients" / "personal").mkdir(parents=True, exist_ok=True)
        (repo / "default-skills" / "clients" / "vibe-coding-client").mkdir(parents=True, exist_ok=True)
        if include_bundle:
            self._write_skill_bundle(repo / "default-skills" / "sample-skill.skill", "sample-skill")
            self._write_skill_bundle(
                repo / "default-skills" / "clients" / "personal" / "personal-skill.skill",
                "personal-skill",
            )

        (repo / "skills").mkdir(parents=True, exist_ok=True)
        (repo / "skills" / "clients" / "personal").mkdir(parents=True, exist_ok=True)
        (repo / "skills" / "clients" / "vibe-coding-client").mkdir(parents=True, exist_ok=True)
        (repo / "logs").mkdir(parents=True, exist_ok=True)
        (repo / "repos").mkdir(parents=True, exist_ok=True)
        (repo / "home" / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / "home" / ".codex").mkdir(parents=True, exist_ok=True)
        (repo / "monoserver-host").mkdir(parents=True, exist_ok=True)
        (repo / ".env-manager").mkdir(parents=True, exist_ok=True)
        (repo / ".env-manager" / "manage.py").write_text("# stub\n", encoding="utf-8")

    def _write_skill_bundle(self, bundle_path: Path, skill_name: str) -> None:
        with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                f"{skill_name}/SKILL.md",
                "---\n"
                f"name: {skill_name}\n"
                f"description: Fixture skill {skill_name} for runtime manager tests.\n"
                "---\n\n"
                "# Sample Skill\n",
            )
            archive.writestr(
                f"{skill_name}/references/overview.md",
                "fixture reference\n",
            )


if __name__ == "__main__":
    unittest.main()
