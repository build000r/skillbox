from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts.lib import runtime_model


class RuntimeModelTests(unittest.TestCase):
    def test_build_runtime_model_populates_host_paths_and_profile_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            self._write_fixture(repo)

            model = runtime_model.build_runtime_model(repo)

            repos = {item["id"]: item for item in model["repos"]}
            artifacts = {item["id"]: item for item in model["artifacts"]}
            env_files = {item["id"]: item for item in model["env_files"]}
            skills = {item["id"]: item for item in model["skills"]}
            tasks = {item["id"]: item for item in model["tasks"]}
            services = {item["id"]: item for item in model["services"]}
            logs = {item["id"]: item for item in model["logs"]}
            checks = {item["id"]: item for item in model["checks"]}
            clients = {item["id"]: item for item in model["clients"]}

            self.assertEqual(repos["core-repo"]["host_path"], str(repo / "repos" / "alpha"))
            self.assertEqual(repos["overlay-root"]["host_path"], str(repo / "repos" / "overlayed"))
            self.assertEqual(repos["overlay-root"]["kind"], "repo-root")
            self.assertEqual(artifacts["asset"]["host_path"], str(repo / "workspace" / "asset.txt"))
            self.assertEqual(
                artifacts["asset"]["source"]["host_path"],
                str((repo / "defaults" / "asset.txt").resolve()),
            )
            self.assertEqual(env_files["client-env"]["host_path"], str(repo / "workspace" / "client.env"))
            self.assertEqual(env_files["client-env"]["mode"], "0600")
            self.assertEqual(skills["default-skills"]["bundle_dir_host_path"], str(repo / "default-skills"))
            self.assertEqual(
                skills["default-skills"]["install_targets"][0]["host_path"],
                str(repo / "home" / ".claude" / "skills"),
            )
            self.assertEqual(
                tasks["bootstrap"]["success"]["host_path"],
                str(repo / "logs" / "runtime" / "bootstrap.ok"),
            )
            self.assertEqual(
                services["api"]["healthcheck"]["host_path"],
                str(repo / "scripts" / "check-api.sh"),
            )
            self.assertEqual(services["web"]["profiles"], ["surfaces"])
            self.assertEqual(logs["runtime"]["host_path"], str(repo / "logs" / "runtime.log"))
            self.assertEqual(checks["repo-root"]["host_path"], str(repo / "repos"))
            self.assertEqual(clients["inline"]["default_cwd_host_path"], str(repo / "repos" / "inline"))
            self.assertEqual(clients["overlayed"]["default_cwd_host_path"], str(repo / "repos" / "overlayed"))

    def test_normalize_runtime_sections_scopes_client_and_profile_items(self) -> None:
        resolved = {
            "version": 2,
            "core": {"repos": [{"id": "core"}]},
            "surfaces": {"services": [{"id": "web"}]},
            "clients": [
                {
                    "id": "inline",
                    "repos": [{"id": "inline-repo"}],
                    "checks": [{"id": "inline-check"}],
                }
            ],
        }

        normalized = runtime_model._normalize_runtime_sections(
            resolved,
            overlay_clients=[{"id": "overlayed", "_overlay_path": "/tmp/overlay.yaml", "logs": [{"id": "overlay-log"}]}],
        )

        self.assertEqual({repo["id"] for repo in normalized["repos"]}, {"core", "inline-repo"})
        self.assertEqual(normalized["services"][0]["profiles"], ["surfaces"])
        self.assertEqual(normalized["checks"][0]["client"], "inline")
        self.assertEqual(normalized["logs"][0]["client"], "overlayed")

    def test_helper_defaults_populate_host_paths_for_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = {
                "env": {
                    "SKILLBOX_WORKSPACE_ROOT": "/workspace",
                    "SKILLBOX_LOG_ROOT": "/workspace/logs",
                    "SKILLBOX_HOME_ROOT": "/workspace/home",
                },
                "artifacts": [
                    {"path": "/workspace/home/bin/tool", "source": {"kind": "file", "path": "./defaults/tool"}}
                ],
                "env_files": [
                    {"path": "/workspace/client.env", "source": {"kind": "file", "path": "./defaults/client.env"}}
                ],
                "skills": [
                    {
                        "bundle_dir": "/workspace/default-skills",
                        "manifest": "/workspace/workspace/default-skills.manifest",
                        "sources_config": "/workspace/workspace/default-skills.sources.yaml",
                        "lock_path": "/workspace/workspace/default-skills.lock.json",
                        "install_targets": [{"path": "/workspace/home/.claude/skills"}],
                    }
                ],
                "services": [
                    {"path": "/workspace/scripts/run-api.sh", "healthcheck": {"path": "/workspace/scripts/check-api.sh"}}
                ],
            }

            runtime_model._populate_artifact_defaults(model, repo)
            runtime_model._populate_env_file_defaults(model, repo)
            runtime_model._populate_skill_defaults(model, repo)
            runtime_model._populate_service_defaults(model, repo)

            self.assertEqual(model["artifacts"][0]["host_path"], str(repo / "home" / "bin" / "tool"))
            self.assertEqual(
                model["env_files"][0]["source"]["host_path"],
                str((repo / "defaults" / "client.env").resolve()),
            )
            self.assertEqual(
                model["skills"][0]["install_targets"][0]["host_path"],
                str(repo / "home" / ".claude" / "skills"),
            )
            self.assertEqual(
                model["services"][0]["healthcheck"]["host_path"],
                str(repo / "scripts" / "check-api.sh"),
            )

    def test_collect_client_metadata_rejects_duplicates(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Duplicate runtime client id: inline"):
            runtime_model._collect_client_metadata(
                [{"id": "inline"}, {"id": "inline"}],
                runtime_model._empty_runtime_sections(),
            )

    def test_load_client_overlays_rejects_non_mapping_client_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            (repo / "workspace" / "clients" / "broken").mkdir(parents=True)
            (repo / "workspace" / "clients" / "broken" / "overlay.yaml").write_text(
                "client:\n  - invalid\n",
                encoding="utf-8",
            )
            (repo / ".env.example").write_text("", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Expected `client` to be a mapping"):
                runtime_model.load_client_overlays(repo, runtime_model.load_runtime_env(repo))

    def _write_fixture(self, repo: Path) -> None:
        (repo / "workspace" / "clients" / "overlayed").mkdir(parents=True)
        (repo / "defaults").mkdir()
        (repo / ".env.example").write_text(
            textwrap.dedent(
                """\
                SKILLBOX_WORKSPACE_ROOT=/workspace
                SKILLBOX_REPOS_ROOT=/workspace/repos
                SKILLBOX_LOG_ROOT=/workspace/logs
                SKILLBOX_HOME_ROOT=/workspace/home
                SKILLBOX_CLIENTS_HOST_ROOT=./workspace/clients
                SKILLBOX_MONOSERVER_HOST_ROOT=..
                """
            ),
            encoding="utf-8",
        )
        (repo / "workspace" / "runtime.yaml").write_text(
            textwrap.dedent(
                """\
                version: 1
                selection:
                  profiles:
                    - core
                core:
                  repos:
                    - id: core-repo
                      path: ${SKILLBOX_REPOS_ROOT}/alpha
                  artifacts:
                    - id: asset
                      path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/asset.txt
                      source:
                        kind: file
                        path: ./defaults/asset.txt
                  env_files:
                    - id: client-env
                      path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/client.env
                      source:
                        kind: file
                        path: ./defaults/client.env
                  skills:
                    - id: default-skills
                      bundle_dir: ${SKILLBOX_WORKSPACE_ROOT}/default-skills
                      manifest: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.manifest
                      sources_config: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.sources.yaml
                      lock_path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.lock.json
                      install_targets:
                        - id: claude
                          path: ${SKILLBOX_HOME_ROOT}/.claude/skills
                  tasks:
                    - id: bootstrap
                      success:
                        path: ${SKILLBOX_LOG_ROOT}/runtime/bootstrap.ok
                  services:
                    - id: api
                      path: ${SKILLBOX_WORKSPACE_ROOT}/scripts/run-api.sh
                      healthcheck:
                        path: ${SKILLBOX_WORKSPACE_ROOT}/scripts/check-api.sh
                  logs:
                    - id: runtime
                      path: ${SKILLBOX_LOG_ROOT}/runtime.log
                  checks:
                    - id: repo-root
                      path: ${SKILLBOX_REPOS_ROOT}
                surfaces:
                  services:
                    - id: web
                      path: ${SKILLBOX_WORKSPACE_ROOT}/scripts/run-web.sh
                clients:
                  - id: inline
                    label: Inline
                    default_cwd: ${SKILLBOX_REPOS_ROOT}/inline
                    repos:
                      - id: inline-repo
                        path: ${SKILLBOX_REPOS_ROOT}/inline
                """
            ),
            encoding="utf-8",
        )
        (repo / "workspace" / "clients" / "overlayed" / "overlay.yaml").write_text(
            textwrap.dedent(
                """\
                client:
                  id: overlayed
                  default_cwd: ${SKILLBOX_REPOS_ROOT}/overlayed
                  repo_roots:
                    - id: overlay-root
                      path: ${SKILLBOX_REPOS_ROOT}/overlayed
                """
            ),
            encoding="utf-8",
        )
        (repo / "defaults" / "asset.txt").write_text("asset\n", encoding="utf-8")
        (repo / "defaults" / "client.env").write_text("TOKEN=1\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
