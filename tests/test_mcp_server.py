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
        stderr_capture = io.StringIO()
        completed = subprocess.CompletedProcess(
            ["python3"],
            0,
            stdout='{"ok": true}',
            stderr=(
                "watch this stderr\n"
                "SKILLBOX_DO_TOKEN=do-secret Authorization: Bearer token-secret "
                "password=secret123 api_key=api-secret"
            ),
        )

        with mock.patch.object(MODULE, "send", side_effect=sent.append), mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=completed,
        ) as run, mock.patch.object(sys, "stderr", stderr_capture):
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
        emitted_stderr = sent[0]["params"]["data"]["stderr"]
        mirrored_stderr = stderr_capture.getvalue()
        for exposed in (emitted_stderr, mirrored_stderr):
            self.assertIn("watch this stderr", exposed)
            self.assertIn("SKILLBOX_DO_TOKEN=", exposed)
            self.assertIn("Authorization: Bearer", exposed)
            self.assertIn("[REDACTED]", exposed)
            self.assertNotIn("do-secret", exposed)
            self.assertNotIn("token-secret", exposed)
            self.assertNotIn("secret123", exposed)
            self.assertNotIn("api-secret", exposed)
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

        plain = subprocess.CompletedProcess(
            ["python3"],
            1,
            stdout="not json SKILLBOX_DO_TOKEN=stdout-secret Authorization: Bearer stdout-bearer",
            stderr="",
        )
        with mock.patch.object(MODULE, "send", side_effect=sent.append), mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=plain,
        ):
            ok, exit_code, payload = MODULE.run_manage(["status"])
        self.assertFalse(ok)
        self.assertEqual(exit_code, 1)
        self.assertIn("not json", payload["text"])
        self.assertIn("[REDACTED]", payload["text"])
        self.assertNotIn("stdout-secret", payload["text"])
        self.assertNotIn("stdout-bearer", payload["text"])

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

    def test_mutating_runtime_tools_require_dry_run_before_dispatch(self) -> None:
        cases = {
            "skillbox_sync": {},
            "skillbox_up": {},
            "skillbox_down": {},
            "skillbox_restart": {},
            "skillbox_bootstrap": {},
            "skillbox_context": {},
            "skillbox_onboard": {"client_id": "acme"},
        }

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            MODULE,
            "DRYRUN_MARKER_ROOT",
            Path(tmpdir),
        ):
            for tool_name, arguments in cases.items():
                with self.subTest(tool_name=tool_name), mock.patch.object(MODULE, "run_manage") as run_manage:
                    result = MODULE.dispatch_tool(tool_name, arguments, request_id=f"req-{tool_name}")

                payload = _content_payload(result)
                self.assertTrue(result["isError"])
                self.assertEqual(payload["error"]["type"], "dry_run_required")
                self.assertTrue(payload["error"]["recoverable"])
                self.assertEqual(payload["error"]["tool"], tool_name)
                run_manage.assert_not_called()

    def test_dispatch_tool_rejects_malformed_numeric_arguments_before_manage(self) -> None:
        with mock.patch.object(MODULE, "run_manage") as run_manage:
            result = MODULE.dispatch_tool(
                "skillbox_logs",
                {"lines": "abc"},
                request_id="req-bad-lines",
            )

        payload = _content_payload(result)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"]["type"], "invalid_parameter")
        self.assertIn("lines must be an integer", payload["error"]["message"])
        run_manage.assert_not_called()

        with mock.patch.object(MODULE, "run_manage") as run_manage:
            result = MODULE.dispatch_tool(
                "skillbox_logs",
                {"lines": 0},
                request_id="req-zero-lines",
            )

        payload = _content_payload(result)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"]["type"], "invalid_parameter")
        self.assertIn("lines must be >= 1", payload["error"]["message"])
        run_manage.assert_not_called()

    def test_dispatch_tool_rejects_malformed_boolean_arguments_before_manage(self) -> None:
        cases = (
            ("skillbox_up", {"dry_run": "false"}, "dry_run"),
            ("skillbox_skill", {"action": "prune", "yes": "false"}, "yes"),
            ("skillbox_mmdx_open", {"query": "review", "open": None}, "open"),
            ("skillbox_skills", {"include_global": "false"}, "include_global"),
            ("skillbox_status", {"full": "false"}, "full"),
        )

        for tool_name, arguments, field in cases:
            with self.subTest(tool_name=tool_name, field=field), mock.patch.object(MODULE, "run_manage") as run_manage:
                result = MODULE.dispatch_tool(tool_name, arguments, request_id=f"req-{tool_name}")

            payload = _content_payload(result)
            self.assertTrue(result["isError"])
            self.assertEqual(payload["error"]["type"], "invalid_parameter")
            self.assertIn(f"{field} must be a boolean", payload["error"]["message"])
            run_manage.assert_not_called()

    def test_dispatch_tool_rejects_non_string_repeat_arguments_before_manage(self) -> None:
        cases = (
            ({"client": True}, "client must be a string or array of strings"),
            ({"client": False}, "client must be a string or array of strings"),
            ({"client": 0}, "client must be a string or array of strings"),
            ({"client": [True]}, "client values must be strings"),
            ({"profile": [1]}, "profile values must be strings"),
            ({"service": True}, "service must be a string or array of strings"),
        )

        for arguments, message in cases:
            with self.subTest(arguments=arguments), mock.patch.object(MODULE, "run_manage") as run_manage:
                result = MODULE.dispatch_tool("skillbox_status", arguments, request_id="req-repeat")

            payload = _content_payload(result)
            self.assertTrue(result["isError"])
            self.assertEqual(payload["error"]["type"], "invalid_parameter")
            self.assertIn(message, payload["error"]["message"])
            run_manage.assert_not_called()

    def test_runtime_mcp_dry_run_marker_allows_matching_real_action(self) -> None:
        arguments = {
            "client": ["personal"],
            "profile": ["local-core"],
            "service": ["api"],
        }

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            MODULE,
            "DRYRUN_MARKER_ROOT",
            Path(tmpdir),
        ):
            with mock.patch.object(MODULE, "run_manage", return_value=(True, 0, {"ok": True})) as run_manage:
                preview = MODULE.dispatch_tool(
                    "skillbox_up",
                    {**arguments, "dry_run": True},
                    request_id="req-up-preview",
                )

            preview_payload = _content_payload(preview)
            self.assertTrue(preview_payload["ok"])
            self.assertIn("mcp_dry_run_marker", preview_payload)
            self.assertIn("--dry-run", run_manage.call_args.args[0])
            self.assertEqual(len(list(Path(tmpdir).iterdir())), 1)

            with mock.patch.object(MODULE, "run_manage") as run_manage:
                mismatched = MODULE.dispatch_tool(
                    "skillbox_up",
                    {**arguments, "service": ["web"]},
                    request_id="req-up-mismatch",
                )

            mismatch_payload = _content_payload(mismatched)
            self.assertTrue(mismatched["isError"])
            self.assertEqual(mismatch_payload["error"]["type"], "dry_run_required")
            run_manage.assert_not_called()

            with mock.patch.object(MODULE, "run_manage", return_value=(True, 0, {"ok": True})) as run_manage:
                real = MODULE.dispatch_tool(
                    "skillbox_up",
                    arguments,
                    request_id="req-up-real",
                )

            real_payload = _content_payload(real)
            self.assertTrue(real_payload["ok"])
            self.assertNotIn("--dry-run", run_manage.call_args.args[0])
            self.assertEqual(list(Path(tmpdir).iterdir()), [])

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

    def test_skillbox_parity_report_requires_and_maps_client_id(self) -> None:
        tool = next(tool for tool in MODULE.handle_tools_list({})["tools"] if tool["name"] == "skillbox_parity_report")
        self.assertEqual(tool["inputSchema"]["required"], ["client_id"])
        self.assertNotIn("client", tool["inputSchema"]["properties"])

        missing = MODULE.handle_tools_call({"name": "skillbox_parity_report", "arguments": {}}, request_id="req-parity")
        missing_payload = _content_payload(missing)
        self.assertTrue(missing["isError"])
        self.assertEqual(missing_payload["error"]["type"], "missing_required_parameter")

        with mock.patch.object(
            MODULE,
            "run_manage",
            return_value=(True, 0, {"status": "ready"}),
        ) as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_parity_report",
                    "arguments": {"client_id": "personal", "profile": ["local-core"]},
                },
                request_id="req-parity",
            )

        payload = _content_payload(result)
        self.assertEqual(payload["status"], "ready")
        args = run_manage.call_args.args[0]
        self.assertEqual(args[:4], ["parity-report", "personal", "--format", "json"])
        self.assertIn("--profile", args)

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

    def test_skillbox_overlay_validates_name_as_identifier(self) -> None:
        with mock.patch.object(MODULE, "run_manage") as run_manage:
            result = MODULE.handle_tools_call(
                {
                    "name": "skillbox_overlay",
                    "arguments": {"action": "activate", "name": "../marketing"},
                },
                request_id="req-overlay-invalid",
            )

        payload = _content_payload(result)
        self.assertTrue(result.get("isError"))
        self.assertEqual(payload["error"]["type"], "invalid_parameter")
        self.assertIn("path separators", payload["error"]["message"])
        run_manage.assert_not_called()

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
                '2026-04-17T12:00:00 session.note personal:sess-1 '
                '{"client_id":"personal","session_id":"sess-1",'
                '"message":"runtime mirror password=runtime-secret",'
                '"env":"SKILLBOX_DO_TOKEN=runtime-token"}\n',
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
                        "detail": {
                            "message": "session event Authorization: Bearer session-bearer",
                            "env": "SPAPS_AUTH_ACCESS_TOKEN=session-token",
                        },
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
        messages = {item["source"]: item["message"] for item in payload["events"]}
        self.assertIn("runtime mirror", messages["runtime_log"])
        self.assertIn("session event", messages["session"])
        serialized = json.dumps(payload["events"])
        self.assertIn("[REDACTED]", serialized)
        for secret in ("runtime-secret", "runtime-token", "session-bearer", "session-token"):
            self.assertNotIn(secret, serialized)

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


class SkillboxMcpIdentifierValidationTests(unittest.TestCase):
    """Security guards added by the sec-hardening-20260516 slice."""

    def test_validate_identifier_rejects_path_separator(self) -> None:
        for bad in ("foo/bar", "foo\\bar", "../etc/passwd", "a/../b"):
            with self.assertRaises(ValueError, msg=f"accepted bad id: {bad!r}"):
                MODULE._validate_identifier(bad, "client_id")

    def test_validate_identifier_rejects_leading_dash(self) -> None:
        for bad in ("--help", "-x", "--config-root=/etc"):
            with self.assertRaises(ValueError, msg=f"accepted bad id: {bad!r}"):
                MODULE._validate_identifier(bad, "client_id")

    def test_validate_identifier_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            MODULE._validate_identifier("", "client_id")

    def test_validate_identifier_accepts_well_formed_slug(self) -> None:
        for good in ("personal", "acme-studio", "client_01", "a.b.c", "X"):
            self.assertEqual(MODULE._validate_identifier(good, "client_id"), good)

    def test_handle_events_rejects_traversal_client_id(self) -> None:
        result = MODULE._handle_events({"client_id": "../etc/passwd"})
        payload = _content_payload(result)
        self.assertTrue(result.get("isError"))
        self.assertEqual(payload["error"]["type"], "invalid_parameter")


if __name__ == "__main__":
    unittest.main()
