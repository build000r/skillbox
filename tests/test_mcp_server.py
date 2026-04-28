from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = ROOT_DIR / ".env-manager" / "mcp_server.py"
MODULE = SourceFileLoader(
    "skillbox_mcp_server",
    str(SCRIPT.resolve()),
).load_module()


def _content_payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


class SkillboxMcpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        MODULE.CURRENT_LOG_LEVEL = "warning"

    def tearDown(self) -> None:
        MODULE.CURRENT_LOG_LEVEL = "warning"

    def test_handle_initialize_advertises_logging_capability(self) -> None:
        payload = MODULE.handle_initialize({})
        self.assertEqual(payload["capabilities"]["logging"], {})
        self.assertIn("skillbox_events", payload["instructions"])

    def test_handle_logging_set_level_updates_threshold_and_rejects_invalid_values(self) -> None:
        result = MODULE.handle_logging_set_level({"level": "debug"})
        self.assertEqual(result, {})
        self.assertEqual(MODULE.CURRENT_LOG_LEVEL, "debug")

        with self.assertRaises(MODULE.JsonRpcError) as raised:
            MODULE.handle_logging_set_level({"level": "loud"})
        self.assertEqual(raised.exception.code, -32602)

    def test_run_manage_passes_event_context_env_and_emits_stderr_notification(self) -> None:
        sent: list[dict] = []
        completed = subprocess.CompletedProcess(
            ["python3"],
            0,
            stdout='{"ok": true}',
            stderr="watch this stderr",
        )

        with mock.patch.object(MODULE, "send", side_effect=sent.append), mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=completed,
        ) as run:
            ok, exit_code, payload = MODULE.run_manage(
                ["status", "--format", "json"],
                event_context={
                    "mcp_request_id": "req-7",
                    "mcp_tool_name": "skillbox_status",
                },
            )

        self.assertTrue(ok)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload, {"ok": True})
        env = run.call_args.kwargs["env"]
        self.assertEqual(
            json.loads(env[MODULE.MCP_EVENT_CONTEXT_ENV]),
            {
                "mcp_request_id": "req-7",
                "mcp_tool_name": "skillbox_status",
            },
        )
        self.assertEqual(sent[0]["method"], "notifications/message")
        self.assertEqual(sent[0]["params"]["level"], "warning")
        self.assertEqual(sent[0]["params"]["logger"], "skillbox.manage.stderr")
        self.assertEqual(sent[0]["params"]["data"]["stderr"], "watch this stderr")
        self.assertEqual(sent[0]["params"]["data"]["mcp_tool_name"], "skillbox_status")

    def test_handle_tools_call_builds_request_scoped_event_context(self) -> None:
        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"ok": True}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_session_event",
                    "arguments": {
                        "client_id": "personal",
                        "session_id": "sess-1",
                        "event_type": "note",
                        "message": "Checkpoint",
                        "actor": "codex",
                    },
                },
                request_id="req-9",
            )

        payload = _content_payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(run_manage.call_args.args[0][:3], ["session-event", "personal", "--format"])
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"],
            {
                "mcp_tool_name": "skillbox_session_event",
                "manage_command": "session-event",
                "mcp_request_id": "req-9",
                "client_id": "personal",
                "session_id": "sess-1",
                "actor": "codex",
            },
        )

    def test_skillbox_skills_is_exposed_and_maps_scope_arguments(self) -> None:
        tool_names = {tool["name"] for tool in MODULE.handle_tools_list({})["tools"]}
        self.assertIn("skillbox_skills", tool_names)

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"summary": {"effective": 0}}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_skills",
                    "arguments": {
                        "cwd": "/tmp/repo",
                        "client": ["personal"],
                        "profile": ["local-core"],
                        "include_global": False,
                        "include_project": False,
                        "show_sources": True,
                    },
                },
                request_id="req-skills",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["summary"]["effective"], 0)
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:3], ["skills", "--format", "json"])
        self.assertIn("--cwd", args)
        self.assertIn("/tmp/repo", args)
        self.assertIn("--client", args)
        self.assertIn("personal", args)
        self.assertIn("--profile", args)
        self.assertIn("local-core", args)
        self.assertIn("--no-global", args)
        self.assertIn("--no-project", args)
        self.assertIn("--show-sources", args)
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"]["mcp_tool_name"],
            "skillbox_skills",
        )

    def test_skillbox_status_defaults_to_compact_with_full_escape_hatch(self) -> None:
        tool = next(tool for tool in MODULE.handle_tools_list({})["tools"] if tool["name"] == "skillbox_status")
        self.assertIn("full", tool["inputSchema"]["properties"])

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"ok": True}),
        ) as run_manage:
            MODULE.handle_tools_call({"name": "skillbox_status", "arguments": {}}, request_id="req-status")

        self.assertEqual(run_manage.call_args.args[0][:3], ["status", "--format", "json"])
        self.assertIn("--compact", run_manage.call_args.args[0])
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"]["mcp_tool_name"],
            "skillbox_status",
        )

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"ok": True}),
        ) as run_manage:
            MODULE.handle_tools_call(
                {"name": "skillbox_status", "arguments": {"full": True}},
                request_id="req-status-full",
            )

        self.assertNotIn("--compact", run_manage.call_args.args[0])
        self.assertNotIn("--full", run_manage.call_args.args[0])

    def test_skillbox_skill_maps_lifecycle_arguments(self) -> None:
        tool_names = {tool["name"] for tool in MODULE.handle_tools_list({})["tools"]}
        self.assertIn("skillbox_skill", tool_names)

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"summary": {"actions": 2}}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_skill",
                    "arguments": {
                        "action": "add",
                        "skill": "ui",
                        "cwd": "/tmp/repo",
                        "to": "category",
                        "category": ["frontend"],
                        "source": "/tmp/skills/ui",
                        "dry_run": True,
                    },
                },
                request_id="req-skill",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["summary"]["actions"], 2)
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:3], ["skill", "add", "--format"])
        self.assertIn("ui", args)
        self.assertIn("--cwd", args)
        self.assertIn("/tmp/repo", args)
        self.assertIn("--to", args)
        self.assertIn("category", args)
        self.assertIn("--category", args)
        self.assertIn("frontend", args)
        self.assertIn("--source", args)
        self.assertIn("/tmp/skills/ui", args)
        self.assertIn("--dry-run", args)
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"]["mcp_tool_name"],
            "skillbox_skill",
        )

    def test_skillbox_events_replays_runtime_and_session_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runtime_log = repo / "logs" / "runtime" / "runtime.log"
            runtime_log.parent.mkdir(parents=True, exist_ok=True)
            runtime_log.write_text(
                '2026-04-17T12:00:00 session.note personal:sess-1 {"client_id":"personal","session_id":"sess-1","message":"runtime mirror"}\n',
                encoding="utf-8",
            )

            session_dir = repo / ".skillbox-state" / "logs" / "clients" / "personal" / "sessions" / "sess-1"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "meta.json").write_text(
                json.dumps({"client_id": "personal", "session_id": "sess-1"}),
                encoding="utf-8",
            )
            (session_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "ts": 1776427201.0,
                        "type": "session.note",
                        "client_id": "personal",
                        "session_id": "sess-1",
                        "detail": {"message": "session event"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            runtime_manager = MODULE._runtime_manager_module()
            with mock.patch.object(runtime_manager, "DEFAULT_ROOT_DIR", repo):
                result = MODULE.handle_tools_call(
                    {
                        "name": "skillbox_events",
                        "arguments": {"session_id": "sess-1", "limit": 10},
                    },
                    request_id="req-11",
                )

        payload = _content_payload(result)
        self.assertEqual(payload["returned"], 2)
        self.assertEqual(payload["next_cursor"], "2")
        self.assertEqual({item["source"] for item in payload["events"]}, {"runtime_log", "session"})
        self.assertEqual({item["message"] for item in payload["events"]}, {"runtime mirror", "session event"})

    def test_main_maps_invalid_logging_requests_to_jsonrpc_errors(self) -> None:
        errors: list[tuple[object, int, str]] = []
        stdin = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logging/setLevel",
                    "params": {"level": "too-much"},
                }
            )
            + "\n"
        )

        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(
            MODULE,
            "send_error",
            side_effect=lambda msg_id, code, message: errors.append((msg_id, code, message)),
        ), mock.patch.object(MODULE, "send") as send:
            MODULE.main()

        send.assert_not_called()
        self.assertEqual(errors[0][0], 1)
        self.assertEqual(errors[0][1], -32602)
        self.assertIn("Invalid log level", errors[0][2])


if __name__ == "__main__":
    unittest.main()
