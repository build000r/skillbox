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
    def test_load_dotenv_only_sets_missing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("FOO=file\nBAR=file\nINVALID\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"FOO": "existing"}, clear=True):
                MODULE.load_dotenv(env_path)
                self.assertEqual(os.environ["FOO"], "existing")
                self.assertEqual(os.environ["BAR"], "file")

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
        self.assertIn("--dry-run", args)
        emit_event.assert_called_once()

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
        run_ssh.assert_called_once_with("skillbox", "skillbox-alpha", "pwd", timeout=15)

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
        stdin = io.StringIO(
            "\n".join(
                [
                    "not-json",
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "missing"}),
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
            ):
            MODULE.main()

        self.assertEqual(errors[0][1], -32700)
        self.assertEqual(errors[1][1], -32601)
        self.assertEqual(sent[-1]["id"], 1)
        self.assertEqual(sent[-1]["result"], {})


if __name__ == "__main__":
    unittest.main()
