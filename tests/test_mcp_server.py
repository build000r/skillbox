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

    def test_run_manage_handles_timeout_plain_text_and_empty_failure(self) -> None:
        sent: list[dict] = []
        with mock.patch.object(MODULE, "send", side_effect=sent.append), mock.patch.object(
            MODULE.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["python3"], timeout=1),
        ):
            ok, exit_code, payload = MODULE.run_manage(["up", "--wait-seconds", "bogus"])
        self.assertFalse(ok)
        self.assertEqual(exit_code, -1)
        self.assertEqual(payload["error"]["type"], "timeout")

        plain = subprocess.CompletedProcess(["python3"], 1, stdout="not json", stderr="")
        with mock.patch.object(MODULE, "send", side_effect=sent.append), mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=plain,
        ):
            ok, exit_code, payload = MODULE.run_manage(["status"])
        self.assertFalse(ok)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["text"], "not json")

        empty = subprocess.CompletedProcess(["python3"], 1, stdout="", stderr="")
        with mock.patch.object(MODULE, "send", side_effect=sent.append), mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=empty,
        ):
            ok, exit_code, payload = MODULE.run_manage(["status"])
        self.assertFalse(ok)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["exit_code"], 1)

    def test_tool_event_context_handles_empty_and_multi_client_scope(self) -> None:
        compact = MODULE._compact_log_context(  # noqa: SLF001
            {"keep": "yes", "drop_none": None, "drop_empty": "", "drop_list": []},
            extra="value",
        )
        self.assertEqual(compact, {"keep": "yes", "extra": "value"})

        context = MODULE.build_tool_event_context(
            "skillbox_status",
            "status",
            {
                "client": ["personal", "skillbox"],
                "profile": ["core"],
                "service": ["api"],
                "task": ["bootstrap"],
                "actor": "operator",
            },
            "req-scope",
        )
        self.assertEqual(context["client_scope"], ["personal", "skillbox"])
        self.assertEqual(context["profile"], ["core"])
        self.assertEqual(context["service"], ["api"])
        self.assertEqual(context["task"], ["bootstrap"])
        self.assertEqual(context["actor"], "operator")

    def test_run_manage_reports_missing_manage_py(self) -> None:
        sent: list[dict] = []

        with mock.patch.object(MODULE, "send", side_effect=sent.append), mock.patch.object(
            MODULE,
            "MANAGE_PY",
            ROOT_DIR / "missing-manage.py",
        ):
            ok, exit_code, payload = MODULE.run_manage(["status", "--format", "json"])

        self.assertFalse(ok)
        self.assertEqual(exit_code, -1)
        self.assertEqual(payload["error"]["type"], "manage_not_found")
        self.assertEqual(sent[0]["method"], "notifications/message")
        self.assertEqual(sent[0]["params"]["level"], "error")

    def test_run_manage_reports_subprocess_timeout(self) -> None:
        sent: list[dict] = []

        with mock.patch.object(MODULE, "send", side_effect=sent.append), mock.patch.object(
            MODULE.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["python3"], timeout=120),
        ):
            ok, exit_code, payload = MODULE.run_manage(
                ["status", "--format", "json"],
                event_context={"mcp_tool_name": "skillbox_status"},
            )

        self.assertFalse(ok)
        self.assertEqual(exit_code, -1)
        self.assertEqual(payload["error"]["type"], "timeout")
        self.assertEqual(sent[0]["params"]["level"], "error")
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

    def test_tool_event_context_normalizes_scalar_and_repeated_scope(self) -> None:
        context = MODULE.build_tool_event_context(
            "skillbox_status",
            "status",
            {
                "client": "personal",
                "profile": [" local-core ", ""],
                "service": ["api-stub", "web-stub"],
            },
            request_id=123,
        )

        self.assertEqual(
            context,
            {
                "mcp_tool_name": "skillbox_status",
                "manage_command": "status",
                "mcp_request_id": "123",
                "client_id": "personal",
                "profile": ["local-core"],
                "service": ["api-stub", "web-stub"],
            },
        )

    def test_dispatch_tool_returns_structured_unknown_tool_error(self) -> None:
        result = MODULE.dispatch_tool("skillbox_missing", {}, request_id="req-missing")

        payload = _content_payload(result)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"]["type"], "unknown_tool")
        self.assertFalse(payload["error"]["recoverable"])
        self.assertIn("skillbox_status", payload["error"]["available_tools"])

    def test_dispatch_tool_rejects_missing_required_positional(self) -> None:
        result = MODULE.dispatch_tool("skillbox_focus", {}, request_id="req-focus")

        payload = _content_payload(result)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"]["type"], "missing_required_parameter")
        self.assertIn("client_id", payload["error"]["message"])

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

    def test_skillbox_skill_audit_maps_scan_arguments(self) -> None:
        tool_names = {tool["name"] for tool in MODULE.handle_tools_list({})["tools"]}
        self.assertIn("skillbox_skill_audit", tool_names)

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"summary": {"candidate_repos": 0}}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_skill_audit",
                    "arguments": {
                        "cwd": "/tmp/repo",
                        "scan_root": ["/tmp"],
                        "max_depth": 2,
                        "include_global": False,
                        "include_clean": True,
                        "limit": 5,
                    },
                },
                request_id="req-skill-audit",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["summary"]["candidate_repos"], 0)
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:3], ["skill-audit", "--format", "json"])
        self.assertIn("--cwd", args)
        self.assertIn("/tmp/repo", args)
        self.assertIn("--scan-root", args)
        self.assertIn("/tmp", args)
        self.assertIn("--max-depth", args)
        self.assertIn("2", args)
        self.assertIn("--no-global", args)
        self.assertIn("--all", args)
        self.assertIn("--limit", args)
        self.assertIn("5", args)

    def test_skillbox_mcp_audit_maps_config_root_argument(self) -> None:
        tool_names = {tool["name"] for tool in MODULE.handle_tools_list({})["tools"]}
        self.assertIn("skillbox_mcp_audit", tool_names)

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"summary": {"expected": 1}}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_mcp_audit",
                    "arguments": {
                        "cwd": "/tmp/repo",
                        "config_root": "/tmp/repo",
                        "profile": ["local-all"],
                    },
                },
                request_id="req-mcp-audit",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["summary"]["expected"], 1)
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:3], ["mcp-audit", "--format", "json"])
        self.assertIn("--cwd", args)
        self.assertIn("/tmp/repo", args)
        self.assertIn("--config-root", args)
        self.assertIn("--profile", args)
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"]["mcp_tool_name"],
            "skillbox_mcp_audit",
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
        skill_tool = next(tool for tool in MODULE.handle_tools_list({})["tools"] if tool["name"] == "skillbox_skill")
        self.assertIn("activate", skill_tool["inputSchema"]["properties"]["action"]["enum"])

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

    def test_skillbox_overlay_maps_activate_arguments(self) -> None:
        tool_names = {tool["name"] for tool in MODULE.handle_tools_list({})["tools"]}
        self.assertIn("skillbox_overlay", tool_names)
        overlay_tool = next(tool for tool in MODULE.handle_tools_list({})["tools"] if tool["name"] == "skillbox_overlay")
        self.assertEqual(overlay_tool["inputSchema"]["properties"]["to"]["default"], "project")
        self.assertEqual(overlay_tool["inputSchema"]["properties"]["scope"]["default"], "project")

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"activations": [{"skill": "marketing"}]}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_overlay",
                    "arguments": {
                        "action": "activate",
                        "name": "marketing",
                        "cwd": "/tmp/repo",
                        "to": "global",
                        "scope": "all",
                        "client": ["personal"],
                        "profile": ["local-core"],
                        "dry_run": True,
                    },
                },
                request_id="req-overlay",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["activations"][0]["skill"], "marketing")
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:3], ["overlay", "activate", "--format"])
        self.assertIn("marketing", args)
        self.assertIn("--cwd", args)
        self.assertIn("/tmp/repo", args)
        self.assertIn("--to", args)
        self.assertIn("global", args)
        self.assertIn("--scope", args)
        self.assertIn("all", args)
        self.assertIn("--client", args)
        self.assertIn("personal", args)
        self.assertIn("--profile", args)
        self.assertIn("local-core", args)
        self.assertIn("--dry-run", args)
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"]["mcp_tool_name"],
            "skillbox_overlay",
        )

    def test_skillbox_mmdx_open_maps_fuzzy_query_without_opening(self) -> None:
        tool_names = {tool["name"] for tool in MODULE.handle_tools_list({})["tools"]}
        self.assertIn("skillbox_mmdx_open", tool_names)

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"action": "resolved"}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_mmdx_open",
                    "arguments": {
                        "query": "skill review realms",
                        "cwd": "/tmp/repo",
                        "search_root": ["/tmp/repo/docs"],
                        "open": False,
                        "limit": 5,
                    },
                },
                request_id="req-mmdx",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["action"], "resolved")
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:3], ["mmdx", "--format", "json"])
        self.assertIn("skill review realms", args)
        self.assertIn("--cwd", args)
        self.assertIn("/tmp/repo", args)
        self.assertIn("--search-root", args)
        self.assertIn("/tmp/repo/docs", args)
        self.assertIn("--no-open", args)
        self.assertIn("--limit", args)
        self.assertIn("5", args)
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"]["mcp_tool_name"],
            "skillbox_mmdx_open",
        )

    def test_skillbox_operator_booking_maps_book_arguments(self) -> None:
        tool_names = {tool["name"] for tool in MODULE.handle_tools_list({})["tools"]}
        self.assertIn("skillbox_operator_booking", tool_names)
        tool = next(tool for tool in MODULE.handle_tools_list({})["tools"] if tool["name"] == "skillbox_operator_booking")
        self.assertIn("book", tool["inputSchema"]["properties"]["action"]["enum"])

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"action": "book"}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_operator_booking",
                    "arguments": {
                        "action": "book",
                        "client": ["personal"],
                        "profile": ["local-all"],
                        "date": "2026-05-06",
                        "slot": "AM",
                        "email": "customer@example.com",
                        "name": "Customer Example",
                        "access_token_env": "SPAPS_AUTH_ACCESS_TOKEN",
                        "send_magic_link": True,
                        "dry_run": True,
                    },
                },
                request_id="req-operator-booking",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["action"], "book")
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:3], ["operator-booking", "book", "--format"])
        self.assertIn("--client", args)
        self.assertIn("personal", args)
        self.assertIn("--profile", args)
        self.assertIn("local-all", args)
        self.assertIn("--date", args)
        self.assertIn("2026-05-06", args)
        self.assertIn("--slot", args)
        self.assertIn("AM", args)
        self.assertIn("--email", args)
        self.assertIn("customer@example.com", args)
        self.assertIn("--name", args)
        self.assertIn("Customer Example", args)
        self.assertIn("--access-token-env", args)
        self.assertIn("SPAPS_AUTH_ACCESS_TOKEN", args)
        self.assertIn("--send-magic-link", args)
        self.assertIn("--dry-run", args)
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"]["mcp_tool_name"],
            "skillbox_operator_booking",
        )

    def test_worker_runtime_tools_map_contract_arguments(self) -> None:
        tool_names = {tool["name"] for tool in MODULE.handle_tools_list({})["tools"]}
        self.assertIn("skillbox_worker_submit", tool_names)
        self.assertIn("skillbox_worker_status", tool_names)
        self.assertIn("skillbox_worker_artifacts", tool_names)
        self.assertIn("skillbox_worker_promote_learning", tool_names)

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"run_id": "wr_20260504_120000_abcdef"}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_worker_submit",
                    "arguments": {
                        "task_class": "analysis",
                        "instruction": "Inspect repo.",
                        "client": "skills",
                        "cwd": "/tmp/repo",
                        "runtime": "hermes",
                        "write_scope": "read_only",
                        "memory_scope": "repo",
                        "artifact_policy": "summary_and_files",
                    },
                },
                request_id="req-worker",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["run_id"], "wr_20260504_120000_abcdef")
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:3], ["worker-submit", "--format", "json"])
        self.assertIn("analysis", args)
        self.assertIn("Inspect repo.", args)
        self.assertIn("--client", args)
        self.assertIn("skills", args)
        self.assertIn("--cwd", args)
        self.assertIn("/tmp/repo", args)
        self.assertIn("--write-scope", args)
        self.assertIn("read_only", args)
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"]["mcp_tool_name"],
            "skillbox_worker_submit",
        )

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"state": "queued"}),
        ) as run_manage:
            MODULE.handle_tools_call(
                {
                    "name": "skillbox_worker_status",
                    "arguments": {"run_id": "wr_20260504_120000_abcdef"},
                },
                request_id="req-worker-status",
            )
        self.assertEqual(
            run_manage.call_args.args[0],
            ["worker-status", "--format", "json", "wr_20260504_120000_abcdef"],
        )

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"proposal_id": "lp_001", "status": "promoted"}),
        ) as run_manage:
            MODULE.handle_tools_call(
                {
                    "name": "skillbox_worker_promote_learning",
                    "arguments": {
                        "proposal_id": "lp_001",
                        "approved_by": "operator",
                        "target_kind": "skill",
                        "target_location": "opensource/skills/report-analyst",
                    },
                },
                request_id="req-worker-promote",
            )
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:4], ["worker-promote-learning", "--format", "json", "lp_001"])
        self.assertIn("--approved-by", args)
        self.assertIn("operator", args)
        self.assertIn("--target-kind", args)
        self.assertIn("skill", args)

    def test_skillbox_operator_booking_defaults_to_availability(self) -> None:
        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"action": "availability"}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {"name": "skillbox_operator_booking", "arguments": {"client": ["personal"]}},
                request_id="req-operator-booking-default",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["action"], "availability")
        self.assertEqual(run_manage.call_args.args[0][:3], ["operator-booking", "availability", "--format"])
        self.assertEqual(
            run_manage.call_args.kwargs["event_context"]["mcp_tool_name"],
            "skillbox_operator_booking",
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

    def test_main_handles_success_parse_error_notification_and_unknown_method(self) -> None:
        sent: list[dict] = []
        errors: list[tuple[object, int, str]] = []
        stdin = io.StringIO(
            "\n"
            "{not-json}\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 4, "method": "missing/method"}) + "\n"
        )

        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(
            MODULE,
            "send",
            side_effect=sent.append,
        ), mock.patch.object(
            MODULE,
            "send_error",
            side_effect=lambda msg_id, code, message: errors.append((msg_id, code, message)),
        ):
            MODULE.main()

        self.assertEqual(errors[0][0], None)
        self.assertEqual(errors[0][1], -32700)
        self.assertEqual(errors[1], (4, -32601, "Method not found: missing/method"))
        self.assertEqual(sent[0]["id"], 2)
        self.assertEqual(sent[0]["result"], {})
        self.assertEqual(sent[1]["id"], 3)
        self.assertIn("tools", sent[1]["result"])

    def test_main_maps_unhandled_handler_exception_to_internal_error(self) -> None:
        errors: list[tuple[object, int, str]] = []
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "boom"}) + "\n"
        )

        def boom(_params: dict, _request_id: object = None) -> dict:
            raise RuntimeError("boom")

        stderr = io.StringIO()
        with mock.patch.dict(MODULE._HANDLERS, {"boom": boom}), mock.patch.object(
            sys,
            "stdin",
            stdin,
        ), mock.patch.object(sys, "stderr", stderr), mock.patch.object(
            MODULE,
            "send_error",
            side_effect=lambda msg_id, code, message: errors.append((msg_id, code, message)),
        ), mock.patch.object(
            MODULE,
            "send",
        ):
            MODULE.main()

        self.assertEqual(errors[0], (5, -32603, "Internal error in boom"))


if __name__ == "__main__":
    unittest.main()
