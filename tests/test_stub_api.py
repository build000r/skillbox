from __future__ import annotations

import json
import sys
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
