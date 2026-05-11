from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import swimmers_launch as MODULE  # noqa: E402


class Response:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def getcode(self) -> int:
        return self.status


class SwimmersLaunchTests(unittest.TestCase):
    def test_dry_run_builds_batch_payload_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            invoke_cwd = Path(tmpdir) / "launcher"
            invoke_cwd.mkdir()
            dirs_file = invoke_cwd / "dirs.txt"
            dirs_file.write_text("../worker\n# comment\n../worker\n../reviewer\n", encoding="utf-8")

            with mock.patch.object(MODULE.urllib.request, "urlopen") as urlopen:
                exit_code, payload = MODULE.launch_swimmers_batch(
                    positional_dirs=["core"],
                    dirs_file="dirs.txt",
                    request="Ship it",
                    invoke_cwd=str(invoke_cwd),
                    dry_run=True,
                )

        self.assertEqual(exit_code, 0)
        urlopen.assert_not_called()
        self.assertEqual(
            payload["request_body"],
            {
                "dirs": [
                    str(invoke_cwd / "core"),
                    str(invoke_cwd.parent / "worker"),
                    str(invoke_cwd.parent / "reviewer"),
                ],
                "spawn_tool": "codex",
                "initial_request": "Ship it",
            },
        )

    def test_launch_posts_batch_request_with_bearer_token(self) -> None:
        response = {
            "results": [
                {
                    "index": 0,
                    "cwd": "/repo/a",
                    "ok": True,
                    "session": {"session_id": "sess_1", "tmux_name": "swimmers-1"},
                }
            ]
        }
        with mock.patch.dict(os.environ, {"SWIMMERS_AUTH_TOKEN": "secret"}, clear=False):
            with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=Response(response, 201)) as urlopen:
                exit_code, payload = MODULE.launch_swimmers_batch(
                    positional_dirs=["/repo/a"],
                    request="Check tests",
                    tool="claude",
                    launch_target="local",
                    base_url="http://127.0.0.1:3210/",
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["success_count"], 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:3210/v1/sessions/batch")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "dirs": ["/repo/a"],
                "spawn_tool": "claude",
                "launch_target": "local",
                "initial_request": "Check tests",
            },
        )

    def test_group_resolution_reads_dirs_before_posting_batch(self) -> None:
        dirs_response = {
            "path": "/repos",
            "entries": [
                {"name": "api"},
                {"name": "web", "full_path": "/workspace/web"},
                {"name": "virtual", "group": "nested"},
            ],
        }
        launch_response = {
            "results": [
                {"index": 0, "cwd": "/repos/api", "ok": True, "session": {"session_id": "a"}},
                {"index": 1, "cwd": "/workspace/web", "ok": True, "session": {"session_id": "b"}},
            ]
        }
        with mock.patch.object(
            MODULE.urllib.request,
            "urlopen",
            side_effect=[Response(dirs_response), Response(launch_response, 201)],
        ) as urlopen:
            exit_code, payload = MODULE.launch_swimmers_batch(
                positional_dirs=[],
                group="core",
                request="Audit",
                base_url="http://localhost:3210",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["request_body"]["dirs"], ["/repos/api", "/workspace/web"])
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("/v1/dirs?group=core", urlopen.call_args_list[0].args[0].full_url)


if __name__ == "__main__":
    unittest.main()
