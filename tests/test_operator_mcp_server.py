from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = ROOT_DIR / "scripts" / "operator_mcp_server.py"
MODULE = SourceFileLoader(
    "skillbox_operator_mcp",
    str(SCRIPT.resolve()),
).load_module()


def _content_payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


class OperatorMcpServerTests(unittest.TestCase):
    def test_operator_provision_schema_surfaces_spaps_auth_blueprint(self) -> None:
        tool = next(item for item in MODULE.TOOLS if item["name"] == "operator_provision")
        blueprint_prop = tool["inputSchema"]["properties"]["blueprint"]
        description = blueprint_prop["description"]
        self.assertIn("git-repo-http-service-bootstrap-spaps-auth", description)
        self.assertIn("SPAPS local auth/RBAC fixtures", description)
        self.assertEqual(blueprint_prop["default"], MODULE.DEFAULT_FIRST_BOX_BLUEPRINT)

    def test_load_dotenv_only_sets_missing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("FOO=file\nBAR=file\nINVALID\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"FOO": "existing"}, clear=True):
                MODULE.load_dotenv(env_path)
                self.assertEqual(os.environ["FOO"], "existing")
                self.assertEqual(os.environ["BAR"], "file")

    def test_subprocess_inventory_dispatch_and_protocol_helpers_cover_core_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            env_path.write_text("FOO=file\nBAR=file\nINVALID\n# comment\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FOO": "existing"}, clear=True):
                MODULE.load_dotenv(root / "missing.env")
                MODULE.load_dotenv(env_path)
                self.assertEqual(os.environ["FOO"], "existing")
                self.assertEqual(os.environ["BAR"], "file")

            with mock.patch.object(MODULE, "REPO_ROOT", root):
                self.assertEqual(MODULE._compose_monoserver_layer(), ["-f", "docker-compose.monoserver.yml"])

                focus_path = root / "workspace" / ".focus.json"
                override_path = root / "workspace" / ".compose-overrides" / "docker-compose.client-acme.yml"
                override_path.parent.mkdir(parents=True)
                focus_path.parent.mkdir(parents=True, exist_ok=True)
                focus_path.write_text('{"client_id": "acme"}', encoding="utf-8")
                override_path.write_text("services: {}\n", encoding="utf-8")
                self.assertEqual(
                    MODULE._compose_monoserver_layer(),
                    ["-f", "workspace/.compose-overrides/docker-compose.client-acme.yml"],
                )

                focus_path.write_text("{bad json", encoding="utf-8")
                self.assertEqual(MODULE._compose_monoserver_layer(), ["-f", "docker-compose.monoserver.yml"])

            compose_script = ROOT_DIR / "docker-compose.yml"
            with mock.patch.object(MODULE.subprocess, "run", side_effect=FileNotFoundError):
                ok, _code, payload = MODULE.run_compose(["ps"])
            self.assertFalse(ok)
            self.assertEqual(payload["error"]["type"], "docker_not_found")

            with mock.patch.object(
                MODULE.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd=["docker"], timeout=1),
            ):
                ok, _code, payload = MODULE.run_compose(["ps"], timeout=1)
            self.assertFalse(ok)
            self.assertEqual(payload["error"]["type"], "timeout")

            with mock.patch.object(
                MODULE.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["docker"], 0, stdout='[{"Name": "api"}]', stderr=""),
            ) as run:
                ok, _code, payload = MODULE.run_compose(["ps", "--format", "json"])
            self.assertTrue(ok)
            self.assertEqual(payload, [{"Name": "api"}])
            self.assertIn("-f", run.call_args.args[0])
            self.assertEqual(Path(run.call_args.kwargs["cwd"]), compose_script.parent)

            with mock.patch.object(
                MODULE.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["docker"], 1, stdout="plain", stderr="err"),
            ):
                ok, code, payload = MODULE.run_compose(["down"])
            self.assertFalse(ok)
            self.assertEqual(code, 1)
            self.assertEqual(payload["stdout"], "plain")
            self.assertEqual(payload["stderr"], "err")

            with mock.patch.object(MODULE.subprocess, "run", side_effect=FileNotFoundError):
                ok, _code, payload = MODULE.run_ssh("u", "h", "pwd")
            self.assertFalse(ok)
            self.assertEqual(payload["error"]["type"], "ssh_not_found")

            with mock.patch.object(
                MODULE.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=1),
            ):
                ok, _code, payload = MODULE.run_ssh("u", "h", "pwd", timeout=1)
            self.assertFalse(ok)
            self.assertEqual(payload["error"]["type"], "timeout")

            with mock.patch.object(
                MODULE.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["ssh"], 0, stdout='{"stdout": "ok"}', stderr=""),
            ):
                ok, _code, payload = MODULE.run_ssh("u", "h", "pwd")
            self.assertTrue(ok)
            self.assertEqual(payload, {"stdout": "ok"})

            with mock.patch.object(
                MODULE.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["ssh"], 2, stdout="nope", stderr="denied"),
            ):
                ok, code, payload = MODULE.run_ssh("u", "h", "pwd")
            self.assertFalse(ok)
            self.assertEqual(code, 2)
            self.assertEqual(payload["stderr"], "denied")

            inventory_path = root / "boxes.json"
            inventory_path.write_text(
                json.dumps({"boxes": [{"id": "alpha"}, {"id": "beta"}]}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"SKILLBOX_BOX_INVENTORY": str(inventory_path)}):
                self.assertEqual([box["id"] for box in MODULE.load_inventory()], ["alpha", "beta"])
                self.assertEqual(MODULE.find_box("beta"), {"id": "beta"})
                self.assertIsNone(MODULE.find_box("gamma"))

            missing_payload = _content_payload(MODULE.dispatch_tool("missing", {}))
            self.assertEqual(missing_payload["error"]["type"], "unknown_tool")

            with mock.patch.dict(MODULE._DISPATCH, {"known": lambda params: {"content": [params]}}):
                self.assertEqual(MODULE.dispatch_tool("known", {"ok": True}), {"content": [{"ok": True}]})

            with mock.patch.object(MODULE, "REPO_ROOT", root):
                MODULE._stamp_dryrun_marker("operator_test", "alpha")
                marker = root / ".skillbox-state" / "dryrun-markers" / ".skillbox-dryrun-operator_test-alpha"
                self.assertIn("dry-run completed", marker.read_text(encoding="utf-8"))

            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                MODULE.send({"jsonrpc": "2.0", "id": 7, "result": {}})
                MODULE.send_error(8, -32000, "bad")
            sent = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(sent[0]["id"], 7)
            self.assertEqual(sent[1]["error"]["message"], "bad")

            init = MODULE.handle_initialize({})
            self.assertEqual(init["protocolVersion"], MODULE.PROTOCOL_VERSION)
            self.assertIn("operator_boxes", init["instructions"])
            self.assertEqual(MODULE.handle_tools_list()["tools"], MODULE.TOOLS)

            with mock.patch.object(MODULE, "dispatch_tool", return_value={"content": []}) as dispatch:
                self.assertEqual(MODULE.handle_tools_call({"name": "operator_boxes"}), {"content": []})
            dispatch.assert_called_once_with("operator_boxes", {})

    def test_read_only_tool_handlers_and_event_journal_use_structured_outputs(self) -> None:
        with mock.patch.object(
            MODULE,
            "run_script",
            side_effect=[
                (True, 0, {"profiles": []}),
                (False, 1, {"error": {"type": "list_failed"}}),
                (True, 0, {"status": "ready"}),
                (False, 1, {"error": {"type": "doctor_failed"}}),
                (True, 0, {"rendered": True}),
            ],
        ) as run_script:
            profiles = _content_payload(MODULE.handle_operator_profiles({}))
            boxes = _content_payload(MODULE.handle_operator_boxes({}))
            status = _content_payload(MODULE.handle_operator_box_status({"box_id": "alpha"}))
            doctor = _content_payload(MODULE.handle_operator_doctor({}))
            render = _content_payload(MODULE.handle_operator_render({"with_compose": True}))

        self.assertEqual(profiles, {"profiles": []})
        self.assertEqual(boxes["error"]["type"], "list_failed")
        self.assertEqual(status, {"status": "ready"})
        self.assertEqual(doctor["error"]["type"], "doctor_failed")
        self.assertEqual(render, {"rendered": True})
        self.assertEqual(run_script.call_args_list[2].args[1], ["status", "alpha", "--format", "json"])
        self.assertEqual(run_script.call_args_list[4].args[1], ["render", "--format", "json", "--with-compose"])

        with mock.patch.object(
            MODULE,
            "run_compose",
            return_value=(False, 9, {"stderr": "down failed"}),
        ), mock.patch.object(MODULE, "emit_event") as emit_event:
            down = MODULE.handle_operator_compose_down({})
        payload = _content_payload(down)
        self.assertTrue(down["isError"])
        self.assertEqual(payload["exit_code"], 9)
        emit_event.assert_called_once_with("operator.compose_down", "local", {"ok": False})

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.object(MODULE, "REPO_ROOT", root):
                MODULE.emit_event("operator.test", "local", {"ok": True})
                journal = root / "logs" / "runtime" / "journal.jsonl"
                entry = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual(entry["type"], "operator.test")
                self.assertEqual(entry["detail"], {"ok": True})

            with mock.patch.object(MODULE, "REPO_ROOT", root), mock.patch.object(Path, "open", side_effect=OSError):
                MODULE.emit_event("operator.ignored", "local")

    def test_handle_operator_provision_validates_required_box_id_and_runs_script(self) -> None:
        error_payload = _content_payload(MODULE.handle_operator_provision({}))
        self.assertEqual(error_payload["error"]["type"], "missing_required_parameter")

        with mock.patch.object(
            MODULE,
            "run_script",
            return_value=(True, 0, {"box_id": "alpha"}),
        ) as run_script, mock.patch.object(MODULE, "emit_event") as emit_event:
            result = MODULE.handle_operator_provision(
                {
                    "box_id": "alpha",
                    "profile": "dev-small",
                    "deploy_manifest": "/tmp/deploy.json",
                    "blueprint": "git-repo",
                    "set_vars": ["FOO=bar"],
                    "resume": True,
                    "dry_run": True,
                }
            )

        payload = _content_payload(result)
        self.assertEqual(payload["box_id"], "alpha")
        run_script.assert_called_once()
        args = run_script.call_args.args[1]
        self.assertIn("up", args)
        self.assertIn("--profile", args)
        self.assertIn("--deploy-manifest", args)
        self.assertIn("--blueprint", args)
        self.assertIn("--set", args)
        self.assertIn("--resume", args)
        self.assertIn("--dry-run", args)
        self.assertEqual(run_script.call_args.kwargs["timeout"], MODULE.PROVISION_TIMEOUT_SECONDS)
        emit_event.assert_called_once()

    def test_handle_operator_provision_relies_on_box_default_blueprint_when_unspecified(self) -> None:
        with mock.patch.object(
            MODULE,
            "run_script",
            return_value=(True, 0, {"box_id": "alpha"}),
        ) as run_script, mock.patch.object(MODULE, "emit_event"):
            MODULE.handle_operator_provision({"box_id": "alpha"})

        args = run_script.call_args.args[1]
        self.assertNotIn("--blueprint", args)

    def test_handle_operator_teardown_stamps_marker_for_dry_run(self) -> None:
        error_payload = _content_payload(MODULE.handle_operator_teardown({}))
        self.assertEqual(error_payload["error"]["type"], "missing_required_parameter")

        with mock.patch.object(
            MODULE,
            "run_script",
            return_value=(True, 0, {"box_id": "alpha"}),
        ), mock.patch.object(MODULE, "emit_event") as emit_event, mock.patch.object(
            MODULE,
            "_stamp_dryrun_marker",
        ) as stamp:
            result = MODULE.handle_operator_teardown({"box_id": "alpha", "dry_run": True})

        payload = _content_payload(result)
        self.assertEqual(payload["box_id"], "alpha")
        emit_event.assert_called_once()
        stamp.assert_called_once_with("operator_teardown", "alpha")

    def test_handle_operator_box_exec_covers_validation_and_success(self) -> None:
        missing = _content_payload(MODULE.handle_operator_box_exec({"box_id": "alpha"}))
        self.assertEqual(missing["error"]["type"], "missing_required_parameter")

        with mock.patch.object(MODULE, "find_box", return_value=None):
            missing_box = _content_payload(
                MODULE.handle_operator_box_exec({"box_id": "alpha", "command": "pwd"})
            )
        self.assertEqual(missing_box["error"]["type"], "box_not_found")

        with mock.patch.object(MODULE, "find_box", return_value={"id": "alpha", "state": "ready"}):
            no_target = _content_payload(
                MODULE.handle_operator_box_exec({"box_id": "alpha", "command": "pwd"})
            )
        self.assertEqual(no_target["error"]["type"], "no_ssh_target")

        with mock.patch.object(
            MODULE,
            "find_box",
            return_value={
                "id": "alpha",
                "state": "ready",
                "tailscale_ip": "100.64.0.8",
                "tailscale_hostname": "skillbox-alpha",
                "ssh_user": "skillbox",
            },
        ), mock.patch.object(
            MODULE,
            "run_ssh",
            return_value=(True, 0, {"stdout": "ok"}),
        ) as run_ssh:
            result = MODULE.handle_operator_box_exec(
                {"box_id": "alpha", "command": "pwd", "timeout": 15}
            )

        payload = _content_payload(result)
        self.assertEqual(payload["stdout"], "ok")
        run_ssh.assert_called_once_with("skillbox", "100.64.0.8", "pwd", timeout=15)

    def test_handle_operator_compose_up_covers_success_and_build_failure(self) -> None:
        with mock.patch.object(
            MODULE,
            "run_compose",
            side_effect=[
                (True, 0, {"step": "build"}),
                (True, 0, {"step": "up"}),
                (True, 0, {"step": "surfaces"}),
            ],
        ), mock.patch.object(MODULE, "emit_event") as emit_event:
            result = MODULE.handle_operator_compose_up({"build": True, "surfaces": True})

        payload = _content_payload(result)
        self.assertEqual([step["step"] for step in payload["steps"]], ["build", "up", "up-surfaces"])
        emit_event.assert_called_once()

        with mock.patch.object(
            MODULE,
            "run_compose",
            return_value=(False, 1, {"stderr": "boom"}),
        ):
            result = MODULE.handle_operator_compose_up({"build": True})

        payload = _content_payload(result)
        self.assertEqual(payload["error"]["type"], "build_failed")

    def test_handle_operator_compose_down_dry_run_preserves_preview_failure(self) -> None:
        with mock.patch.object(
            MODULE,
            "run_compose",
            return_value=(False, 1, {"stderr": "boom"}),
        ), mock.patch.object(MODULE, "_stamp_dryrun_marker") as stamp:
            result = MODULE.handle_operator_compose_down({"dry_run": True})

        payload = _content_payload(result)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"]["type"], "compose_preview_failed")
        self.assertEqual(payload["exit_code"], 1)
        stamp.assert_not_called()

    def test_handle_operator_compose_down_dry_run_stamps_marker_on_success(self) -> None:
        with mock.patch.object(
            MODULE,
            "run_compose",
            return_value=(True, 0, [{"service": "api"}]),
        ), mock.patch.object(MODULE, "_stamp_dryrun_marker") as stamp:
            result = MODULE.handle_operator_compose_down({"dry_run": True})

        payload = _content_payload(result)
        self.assertEqual(payload["would_stop"], [{"service": "api"}])
        stamp.assert_called_once_with("operator_compose_down", "local")

    def test_run_script_covers_missing_timeout_json_text_and_empty_output(self) -> None:
        missing_ok, missing_code, missing_payload = MODULE.run_script(Path("/missing-script.py"), [])
        self.assertFalse(missing_ok)
        self.assertEqual(missing_code, -1)
        self.assertEqual(missing_payload["error"]["type"], "script_not_found")

        script_path = ROOT_DIR / "scripts" / "box.py"
        with mock.patch.object(
            MODULE.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=1),
        ):
            timed_out = MODULE.run_script(script_path, [], timeout=1)
        self.assertFalse(timed_out[0])
        self.assertEqual(timed_out[2]["error"]["type"], "timeout")

        with mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["python"], 0, stdout='{"ok": true}', stderr="warn"),
        ):
            ok, code, payload = MODULE.run_script(script_path, [])
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"ok": True})

        with mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["python"], 2, stdout="plain text", stderr=""),
        ):
            ok, code, payload = MODULE.run_script(script_path, [])
        self.assertFalse(ok)
        self.assertEqual(code, 2)
        self.assertEqual(payload, {"text": "plain text"})

        with mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["python"], 0, stdout="", stderr=""),
        ):
            ok, code, payload = MODULE.run_script(script_path, [])
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"exit_code": 0})

    def test_main_handles_parse_errors_unknown_methods_and_success(self) -> None:
        sent: list[dict] = []
        errors: list[tuple[object, int, str]] = []
        handlers = dict(MODULE._HANDLERS)
        handlers["boom"] = lambda _params: (_ for _ in ()).throw(RuntimeError("boom"))
        stdin = io.StringIO(
            "\n".join(
                [
                    "not-json",
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                    "",
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "missing"}),
                    json.dumps({"jsonrpc": "2.0", "id": 3, "method": "initialize"}),
                    json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 5,
                            "method": "tools/call",
                            "params": {"name": "operator_profiles", "arguments": {}},
                        }
                    ),
                    json.dumps({"jsonrpc": "2.0", "id": 6, "method": "boom"}),
                ]
            )
            + "\n"
        )

        with mock.patch.object(MODULE, "load_dotenv"), \
            mock.patch.object(sys, "stdin", stdin), \
            mock.patch.object(MODULE, "send", side_effect=sent.append), \
            mock.patch.object(
                MODULE,
                "send_error",
                side_effect=lambda msg_id, code, message: errors.append((msg_id, code, message)),
            ), mock.patch.object(MODULE, "_HANDLERS", handlers), mock.patch.object(
                MODULE,
                "dispatch_tool",
                return_value={"content": [{"type": "text", "text": "{}"}]},
            ):
            MODULE.main()

        self.assertEqual(errors[0][1], -32700)
        self.assertEqual(errors[1][1], -32601)
        self.assertEqual(errors[2], (6, -32603, "Internal error in boom"))
        self.assertEqual([msg["id"] for msg in sent], [1, 3, 4, 5])
        self.assertEqual(sent[0]["result"], {})
        self.assertEqual(sent[1]["result"]["protocolVersion"], MODULE.PROTOCOL_VERSION)
        self.assertEqual(sent[2]["result"]["tools"], MODULE.TOOLS)
        self.assertEqual(sent[3]["result"]["content"][0]["text"], "{}")


if __name__ == "__main__":
    unittest.main()
