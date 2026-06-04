from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_local_runtime import MANAGE_MODULE


class LocalUpIngressSyncTests(unittest.TestCase):
    def _model(self, root: Path) -> dict[str, object]:
        return {
            "root_dir": str(root),
            "env": {
                "SKILLBOX_WORKSPACE_ROOT": "/workspace",
                "SKILLBOX_HOME_ROOT": "/home/sandbox",
                "SKILLBOX_INGRESS_PRIVATE_BASE_URL": "http://100.64.0.1:9080",
            },
            "active_profiles": ["core", "local-app"],
            "active_clients": ["example"],
            "clients": [
                {
                    "id": "example",
                    "label": "Example",
                    "_overlay_path": str(root / "overlay.yaml"),
                }
            ],
            "repos": [
                {
                    "id": "app-repo",
                    "kind": "repo",
                    "path": str(root),
                    "repo_path": str(root),
                    "host_path": str(root),
                    "profiles": ["core", "local-app"],
                }
            ],
            "artifacts": [],
            "env_files": [],
            "skills": [],
            "tasks": [],
            "services": [
                {
                    "id": "app",
                    "kind": "http",
                    "repo_id": "app-repo",
                    "profiles": ["local-app"],
                    "depends_on": [],
                    "bootstrap_tasks": [],
                    "commands": {"reuse": "npm run dev"},
                    "origin_url": "http://127.0.0.1:3999",
                    "healthcheck": {
                        "type": "http",
                        "url": "http://127.0.0.1:3999/",
                    },
                }
            ],
            "ingress_routes": [
                {
                    "id": "app-private-root",
                    "client": "example",
                    "service_id": "app",
                    "listener": "private",
                    "path": "/",
                    "match": "prefix",
                    "profiles": ["local-app"],
                }
            ],
            "logs": [],
            "checks": [],
            "bridges": [],
            "parity_ledger": [],
        }

    def test_run_up_writes_ingress_manifest_before_starting_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            model = self._model(root)
            route_file = root / "logs" / "runtime" / "ingress-routes.json"

            import runtime_manager.workflows as workflows_mod

            def _fake_start(model_arg, services, **kwargs):  # type: ignore[no-untyped-def]
                self.assertTrue(route_file.is_file())
                payload = json.loads(route_file.read_text(encoding="utf-8"))
                self.assertEqual(payload["routes"][0]["id"], "app-private-root")
                self.assertEqual(payload["routes"][0]["origin_url"], "http://127.0.0.1:3999")
                return [{"id": "app", "result": "already-running"}]

            with mock.patch.object(workflows_mod, "start_services", side_effect=_fake_start):
                exit_code, payload = MANAGE_MODULE.run_up(
                    model=model,
                    client_id="example",
                    profile="local-app",
                    requested_mode="reuse",
                    dry_run=False,
                )

            self.assertEqual(exit_code, 0, payload)
            self.assertTrue(any(
                action.startswith("render-ingress-routes:")
                for action in payload["ingress_actions"]
            ))

    def test_run_up_dry_run_reports_ingress_manifest_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            model = self._model(root)
            route_file = root / "logs" / "runtime" / "ingress-routes.json"

            exit_code, payload = MANAGE_MODULE.run_up(
                model=model,
                client_id="example",
                profile="local-app",
                requested_mode="reuse",
                dry_run=True,
            )

            self.assertEqual(exit_code, 0, payload)
            self.assertFalse(route_file.exists())
            self.assertTrue(any(
                action.startswith("render-ingress-routes:")
                for action in payload["ingress_actions"]
            ))


if __name__ == "__main__":
    unittest.main()
