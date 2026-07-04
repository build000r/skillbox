from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = ROOT_DIR / ".env-manager" / "mcp_server.py"
MODULE = SourceFileLoader(
    "skillbox_mcp_server_resources",
    str(SCRIPT.resolve()),
).load_module()


class SkillboxMcpServerResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.state_root = self.root / "state"
        self.skill_dir = self.root / "skills" / "alpha"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text("# Alpha\n\nUse alpha.\n", encoding="utf-8")
        (self.skill_dir / "references").mkdir()
        (self.skill_dir / "references" / "notes.md").write_text("alpha notes\n", encoding="utf-8")
        hidden = self.root / "skills" / "hidden"
        hidden.mkdir()
        (hidden / "SKILL.md").write_text("# Hidden\n", encoding="utf-8")

        self.visibility_payload = {
            "effective": [
                {
                    "name": "alpha",
                    "source": str(self.skill_dir),
                    "state": "installed",
                    "layer": "project:codex",
                    "source_bucket": "local",
                }
            ]
        }

        fake_skill_visibility = SimpleNamespace(_skill_source_options=lambda *_args, **_kwargs: [])
        self.patches = [
            mock.patch.dict(
                os.environ,
                {
                    MODULE.SKILL_RESOURCE_FLAG_ENV: "1",
                    "SKILLBOX_STATE_ROOT": str(self.state_root),
                    "SKILLBOX_SESSION_ID": "sess-alpha",
                },
                clear=False,
            ),
            mock.patch.object(MODULE, "_load_skill_resource_model", return_value={}),
            mock.patch.object(MODULE, "_skill_visibility_module", return_value=fake_skill_visibility),
            mock.patch.object(
                MODULE,
                "_collect_skill_visibility_payload",
                side_effect=lambda _model, _cwd: self.visibility_payload,
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def test_resources_list_matches_visible_skills_and_capability_is_flagged(self) -> None:
        initialize = MODULE.handle_initialize({})
        self.assertIn("resources", initialize["capabilities"])

        result = MODULE.handle_resources_list({"cwd": str(self.root)})
        resources = result["resources"]
        self.assertEqual([item["uri"] for item in resources], ["skillbox://skills/alpha"])
        self.assertEqual(resources[0]["name"], "alpha")

        with mock.patch.dict(os.environ, {MODULE.SKILL_RESOURCE_FLAG_ENV: "0"}, clear=False):
            initialize = MODULE.handle_initialize({})
            self.assertNotIn("resources", initialize["capabilities"])

    def test_resources_read_round_trips_skill_and_bundled_reference(self) -> None:
        root = MODULE.handle_resources_read(
            {"uri": "skillbox://skills/alpha", "cwd": str(self.root)},
            request_id="req-root",
        )
        self.assertEqual(root["contents"][0]["text"], "# Alpha\n\nUse alpha.\n")
        ref_uris = [item["uri"] for item in root["refs"]]
        self.assertEqual(ref_uris, ["skillbox://skills/alpha/references/notes.md"])

        ref = MODULE.handle_resources_read(
            {"uri": ref_uris[0], "cwd": str(self.root)},
            request_id="req-ref",
        )
        self.assertEqual(ref["contents"][0]["text"], "alpha notes\n")
        self.assertEqual(ref["contents"][0]["mimeType"], "text/markdown")

    def test_resources_read_rejects_traversal(self) -> None:
        cases = (
            "skillbox://skills/alpha/../secret",
            "skillbox://skills/alpha/%2e%2e/secret",
        )
        for uri in cases:
            with self.subTest(uri=uri), self.assertRaises(MODULE.JsonRpcError) as raised:
                MODULE.handle_resources_read({"uri": uri, "cwd": str(self.root)})
            self.assertEqual(raised.exception.code, -32602)
            self.assertIn("traversal", raised.exception.message)

    def test_resources_methods_are_method_not_found_when_flag_off(self) -> None:
        request = {"jsonrpc": "2.0", "id": 7, "method": "resources/list", "params": {}}
        stdin = io.StringIO(json.dumps(request) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.dict(os.environ, {MODULE.SKILL_RESOURCE_FLAG_ENV: "0"}, clear=False), mock.patch.object(
            sys,
            "stdin",
            stdin,
        ), mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
            MODULE.main()

        response = json.loads(stdout.getvalue().strip())
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["error"]["code"], -32601)
        self.assertIn("resources/list", response["error"]["message"])

    def test_resources_read_writes_usage_log(self) -> None:
        MODULE.handle_resources_read(
            {"uri": "skillbox://skills/alpha", "cwd": str(self.root)},
            request_id="req-log",
        )

        log_path = self.state_root / MODULE.SKILL_RESOURCE_LOG_PATH
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["skill"], "alpha")
        self.assertEqual(rows[0]["uri"], "skillbox://skills/alpha")
        self.assertEqual(rows[0]["session"], "sess-alpha")
        self.assertIn("ts", rows[0])


if __name__ == "__main__":
    unittest.main()
