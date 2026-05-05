from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (ROOT_DIR / "scripts").resolve()
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
SCRIPT = ROOT_DIR / "scripts" / "stub_api.py"
MODULE = SourceFileLoader(
    "skillbox_stub_api",
    str(SCRIPT.resolve()),
).load_module()


class StubApiTests(unittest.TestCase):
    def test_helpers_list_directories_existing_paths_and_main_server_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "repo-a").mkdir()
            (root / "file.txt").write_text("ignored", encoding="utf-8")
            self.assertEqual(MODULE.list_directories(root), [{"id": "repo-a", "path": str(root / "repo-a")}])
            self.assertEqual(MODULE.list_directories(root / "missing"), [])

            present = root / "present"
            present.write_text("ok", encoding="utf-8")
            paths = MODULE.existing_paths(
                [
                    {"id": "present", "path": "/runtime/present", "host_path": str(present)},
                    {"id": "missing", "path": str(root / "missing")},
                ]
            )
            self.assertTrue(paths[0]["present"])
            self.assertFalse(paths[1]["present"])

        model = {
            "manifest_file": "workspace/runtime.yaml",
            "clients": [{"id": "client"}],
            "selection": {"profiles": ["core"]},
            "repos": [],
            "skills": [],
            "services": [],
            "logs": [],
            "checks": [],
        }
        with mock.patch.object(MODULE, "build_runtime_model", return_value=model) as build_model:
            summary = MODULE.runtime_summary()
        build_model.assert_called_once_with(MODULE.ROOT)
        self.assertEqual(summary["manifest"], "workspace/runtime.yaml")
        self.assertEqual(summary["clients"], [{"id": "client"}])

        server = mock.Mock()
        with (
            mock.patch.object(MODULE, "ThreadingHTTPServer", return_value=server) as server_class,
            mock.patch("sys.stdout"),
        ):
            MODULE.main()
        server_class.assert_called_once_with(("0.0.0.0", MODULE.PORT), MODULE.Handler)
        server.serve_forever.assert_called_once()

    def test_handler_serves_health_and_runtime_routes(self) -> None:
        server = MODULE.ThreadingHTTPServer(("127.0.0.1", 0), MODULE.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base_url = f"http://127.0.0.1:{server.server_port}"

        runtime_payload = {
            "manifest": "workspace/runtime.yaml",
            "clients": [{"id": "personal"}],
            "selection": {"profiles": ["core"]},
            "repos": [{"id": "repo", "path": "/workspace/repos/repo", "host_path": "/tmp/repo"}],
            "skills": [],
            "services": [],
            "logs": [{"id": "runtime", "path": "/workspace/logs/runtime", "host_path": "/tmp/runtime"}],
            "checks": [],
        }

        with mock.patch.object(MODULE, "runtime_summary", return_value=runtime_payload), \
            mock.patch.object(
                MODULE,
                "list_directories",
                side_effect=[[{"id": "repo", "path": "/repos/repo"}], [{"id": "skill", "path": "/skills/skill"}]],
            ):
            health = json.loads(urllib.request.urlopen(f"{base_url}/health").read().decode("utf-8"))
            sandbox = json.loads(urllib.request.urlopen(f"{base_url}/v1/sandbox").read().decode("utf-8"))
            runtime = json.loads(urllib.request.urlopen(f"{base_url}/v1/runtime").read().decode("utf-8"))

        self.assertTrue(health["ok"])
        self.assertEqual(sandbox["runtime_manager"]["client_count"], 1)
        self.assertEqual(runtime["manifest"], "workspace/runtime.yaml")

    def test_handler_serves_repo_and_log_presence_routes(self) -> None:
        server = MODULE.ThreadingHTTPServer(("127.0.0.1", 0), MODULE.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base_url = f"http://127.0.0.1:{server.server_port}"

        runtime_payload = {
            "repos": [{"id": "repo", "path": "/repo", "host_path": "/tmp/repo"}],
            "logs": [{"id": "log", "path": "/log", "host_path": "/tmp/log"}],
        }
        with mock.patch.object(MODULE, "runtime_summary", return_value=runtime_payload):
            repos = json.loads(urllib.request.urlopen(f"{base_url}/v1/repos").read().decode("utf-8"))
            logs = json.loads(urllib.request.urlopen(f"{base_url}/v1/logs").read().decode("utf-8"))

        self.assertEqual(repos["repos"][0]["id"], "repo")
        self.assertEqual(logs["logs"][0]["id"], "log")

    def test_handler_returns_not_found_for_unknown_path(self) -> None:
        server = MODULE.ThreadingHTTPServer(("127.0.0.1", 0), MODULE.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        url = f"http://127.0.0.1:{server.server_port}/missing"

        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url)

        payload = json.loads(exc.exception.read().decode("utf-8"))
        self.assertEqual(exc.exception.code, 404)
        self.assertEqual(payload["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
