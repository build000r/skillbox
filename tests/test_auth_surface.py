"""WG-008 auth/status surface tests for distributor-aware context + status."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ENV_MANAGER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, ".env-manager")
sys.path.insert(0, os.path.abspath(ENV_MANAGER_DIR))

from runtime_manager.context_rendering import generate_context_markdown  # noqa: E402
from runtime_manager.runtime_ops import runtime_status  # noqa: E402


class AuthSurfaceTests(unittest.TestCase):
    def _base_model(self, root_dir: Path) -> dict:
        return {
            "root_dir": str(root_dir),
            "clients": [],
            "selection": {},
            "active_clients": [],
            "active_profiles": ["core"],
            "storage": {},
            "repos": [],
            "artifacts": [],
            "env_files": [],
            "skills": [],
            "tasks": [],
            "services": [],
            "logs": [],
            "checks": [],
            "ingress_routes": [],
            "parity_ledger": [],
        }

    def _write_skill_repos_config(self, path: Path, *, with_distributors: bool) -> None:
        payload: dict = {"version": 2, "skill_repos": []}
        if with_distributors:
            payload["distributors"] = [
                {
                    "id": "acme-skills",
                    "url": "https://skills.acme.dev/api/v1",
                    "client_id": "client-42",
                    "auth": {"method": "api-key", "key_env": "ACME_DISTRIBUTOR_KEY"},
                    "verification": {"public_key": "ed25519:YWJjMTIz"},
                }
            ]
            payload["skill_repos"] = [{"distributor": "acme-skills", "pick": ["deploy"]}]
        else:
            payload["skill_repos"] = [{"path": "./workspace-skills", "pick": ["scratch"]}]
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def _write_distribution_lock(self, path: Path) -> None:
        lock_payload = {
            "version": 3,
            "config_sha": "cfg-sha",
            "synced_at": "2026-04-21T10:05:00Z",
            "distributor_manifests": {
                "acme-skills": {
                    "manifest_version": 14,
                    "fetched_at": "2026-04-21T10:00:00Z",
                    "signature_verified": True,
                }
            },
            "skills": [
                {
                    "name": "deploy",
                    "source": "distributor",
                    "distributor_id": "acme-skills",
                    "version": 7,
                    "bundle_sha256": "abc123",
                    "install_tree_sha": "tree123",
                    "pinned_by": "manifest_recommendation",
                }
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(lock_payload, indent=2) + "\n", encoding="utf-8")

    def _append_skill_repo_set(
        self,
        model: dict,
        *,
        config_path: Path,
        lock_path: Path,
    ) -> None:
        model["skills"] = [
            {
                "id": "personal-skills",
                "kind": "skill-repo-set",
                "sync": {"mode": "clone-and-install"},
                "skill_repos_config": "workspace/clients/personal/skill-repos.yaml",
                "skill_repos_config_host_path": str(config_path),
                "lock_path": "workspace/clients/personal/skill-repos.lock.json",
                "lock_path_host_path": str(lock_path),
                "install_targets": [
                    {
                        "id": "codex",
                        "host_path": str(lock_path.parent / "installed"),
                    }
                ],
            }
        ]

    def test_runtime_status_includes_distributors_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "workspace" / "clients" / "personal" / "skill-repos.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_skill_repos_config(config_path, with_distributors=True)

            lock_path = root / ".skillbox-state" / "skill-repos.lock.json"
            self._write_distribution_lock(lock_path)

            model = self._base_model(root)
            self._append_skill_repo_set(model, config_path=config_path, lock_path=lock_path)

            with mock.patch.dict(os.environ, {"ACME_DISTRIBUTOR_KEY": "token-123"}, clear=False):
                status = runtime_status(model)

            self.assertIn("distributors", status)
            self.assertEqual(len(status["distributors"]), 1)
            distributor = status["distributors"][0]
            self.assertEqual(distributor["id"], "acme-skills")
            self.assertEqual(distributor["client_id"], "client-42")
            self.assertEqual(distributor["url"], "https://skills.acme.dev/api/v1")
            self.assertEqual(distributor["skills_count"], 1)
            self.assertEqual(distributor["manifest_version"], 14)
            self.assertEqual(distributor["last_sync"], "2026-04-21T10:05:00Z")
            self.assertTrue(distributor["auth_key_present"])
            self.assertEqual(distributor["auth_probe_result"], "unknown")

    def test_runtime_status_distributors_empty_for_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "workspace" / "clients" / "personal" / "skill-repos.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_skill_repos_config(config_path, with_distributors=False)

            lock_path = root / ".skillbox-state" / "skill-repos.lock.json"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "config_sha": "legacy",
                        "synced_at": "2026-04-21T10:00:00Z",
                        "skills": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            model = self._base_model(root)
            self._append_skill_repo_set(model, config_path=config_path, lock_path=lock_path)

            status = runtime_status(model)
            self.assertIn("distributors", status)
            self.assertEqual(status["distributors"], [])

    def test_context_markdown_includes_connected_distributors_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "workspace" / "clients" / "personal" / "skill-repos.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_skill_repos_config(config_path, with_distributors=True)

            lock_path = root / ".skillbox-state" / "skill-repos.lock.json"
            self._write_distribution_lock(lock_path)

            model = self._base_model(root)
            self._append_skill_repo_set(model, config_path=config_path, lock_path=lock_path)

            content = generate_context_markdown(model)
            self.assertIn("## Connected Distributors", content)
            self.assertIn("acme-skills", content)
            self.assertIn("https://skills.acme.dev/api/v1", content)

    def test_context_markdown_omits_connected_distributors_for_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "workspace" / "clients" / "personal" / "skill-repos.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_skill_repos_config(config_path, with_distributors=False)

            lock_path = root / ".skillbox-state" / "skill-repos.lock.json"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "config_sha": "legacy",
                        "synced_at": "2026-04-21T10:00:00Z",
                        "skills": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            model = self._base_model(root)
            self._append_skill_repo_set(model, config_path=config_path, lock_path=lock_path)

            content = generate_context_markdown(model)
            self.assertNotIn("## Connected Distributors", content)


if __name__ == "__main__":
    unittest.main()
