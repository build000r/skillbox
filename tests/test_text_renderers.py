"""Tests for runtime_manager.text_renderers — focused on LOCAL_RUNTIME_*
error envelope rendering parity with the JSON path."""
from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

MANAGE_MODULE = SourceFileLoader(
    "skillbox_manage_text_renderers",
    str((ROOT_DIR / ".env-manager" / "manage.py").resolve()),
).load_module()

print_local_runtime_error_text = MANAGE_MODULE.print_local_runtime_error_text
local_runtime_error = MANAGE_MODULE.local_runtime_error
emit_json = MANAGE_MODULE.emit_json


def render_to_stderr(envelope: dict) -> str:
    buf = io.StringIO()
    with redirect_stderr(buf):
        print_local_runtime_error_text(envelope)
    return buf.getvalue()


def render_to_stdout_capture(envelope: dict) -> str:
    """Capture stdout while rendering — should always be empty."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_local_runtime_error_text(envelope)
    return buf.getvalue()


class LocalRuntimeErrorRendererTests(unittest.TestCase):
    """TC-1..TC-6, TC-10 — exhaustive renderer coverage per envelope code."""

    def test_tc1_mode_unsupported_pre_validation_envelope(self) -> None:
        # TC-1: cli.py mode pre-validation. No blocked_services, has next_action.
        envelope = local_runtime_error(
            "LOCAL_RUNTIME_MODE_UNSUPPORTED",
            "Unsupported --mode value 'bogus'. Supported modes: reuse, prod, fresh.",
            recoverable=True,
            next_action="Re-run with --mode <reuse|prod|fresh>.",
        )
        envelope["error"]["requested_mode"] = "bogus"
        out = render_to_stderr(envelope)
        self.assertIn(
            "ERROR [LOCAL_RUNTIME_MODE_UNSUPPORTED]: Unsupported --mode value 'bogus'."
            " Supported modes: reuse, prod, fresh.",
            out,
        )
        self.assertIn("requested mode: bogus", out)
        self.assertIn("next action: Re-run with --mode <reuse|prod|fresh>.", out)
        self.assertNotIn("blocked services:", out)
        self.assertEqual(render_to_stdout_capture(envelope), "")

    def test_tc2_mode_unsupported_from_run_up_lists_blocked(self) -> None:
        # TC-2: workflows.run_up emits MODE_UNSUPPORTED with blocked services.
        envelope = local_runtime_error(
            "LOCAL_RUNTIME_MODE_UNSUPPORTED",
            "Mode 'prod' is not supported by all requested services: cfo",
            recoverable=True,
            blocked_services=["cfo"],
            next_action="Re-run with a mode declared by every service in local-core.",
        )
        envelope["error"]["requested_mode"] = "prod"
        out = render_to_stderr(envelope)
        lines = out.splitlines()
        self.assertEqual(
            lines[0],
            "ERROR [LOCAL_RUNTIME_MODE_UNSUPPORTED]: Mode 'prod' is not supported by"
            " all requested services: cfo",
        )
        self.assertIn("requested mode: prod", out)
        self.assertIn("blocked services:", out)
        self.assertIn("  - cfo", out)
        self.assertIn(
            "next action: Re-run with a mode declared by every service in local-core.",
            out,
        )

    def test_tc3_start_blocked_lists_every_blocked_service(self) -> None:
        # TC-3: bootstrap failure produces START_BLOCKED with full blocked list.
        blocked = [
            "spaps",
            "htma_server",
            "ingredient_server",
            "approval_feedback_api",
            "cfo",
            "htma",
        ]
        envelope = local_runtime_error(
            "LOCAL_RUNTIME_START_BLOCKED",
            "Bootstrap task failed: env bridge timed out",
            recoverable=True,
            blocked_services=blocked,
            next_action="manage.py status --client personal --profile local-core",
        )
        envelope["error"]["requested_mode"] = "reuse"
        out = render_to_stderr(envelope)
        self.assertIn(
            "ERROR [LOCAL_RUNTIME_START_BLOCKED]: Bootstrap task failed: env bridge timed out",
            out,
        )
        self.assertIn("blocked services:", out)
        for sid in blocked:
            self.assertIn(f"  - {sid}", out)
        self.assertIn(
            "next action: manage.py status --client personal --profile local-core",
            out,
        )

    def test_tc4_service_deferred_logs_short_circuit(self) -> None:
        # TC-4: logs deferred surface — has next_action, no blocked_services.
        envelope = local_runtime_error(
            "LOCAL_RUNTIME_SERVICE_DEFERRED",
            "Service ingredient_server is declared deferred by parity ledger",
            recoverable=False,
            next_action="manage.py logs --service spaps",
        )
        out = render_to_stderr(envelope)
        self.assertTrue(
            out.startswith(
                "ERROR [LOCAL_RUNTIME_SERVICE_DEFERRED]: Service ingredient_server"
                " is declared deferred by parity ledger"
            ),
            f"unexpected output: {out!r}",
        )
        self.assertIn("next action: manage.py logs --service spaps", out)
        self.assertNotIn("blocked services:", out)
        self.assertNotIn("requested mode:", out)

    def test_tc5_focus_env_bridge_failed_envelope(self) -> None:
        # TC-5 (Bug B): focus bootstrap failure must surface in text mode.
        envelope = local_runtime_error(
            "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
            "approval-feedback bridge exited with status 2",
            recoverable=True,
            next_action="re-run sync.sh manually to diagnose",
        )
        out = render_to_stderr(envelope)
        self.assertIn(
            "ERROR [LOCAL_RUNTIME_ENV_BRIDGE_FAILED]: approval-feedback bridge"
            " exited with status 2",
            out,
        )
        self.assertIn("next action: re-run sync.sh manually to diagnose", out)

    def test_tc6_profile_unknown_envelope(self) -> None:
        # TC-6: focus profile-validation envelope.
        envelope = local_runtime_error(
            "LOCAL_RUNTIME_PROFILE_UNKNOWN",
            "Profile 'local-bogus' has no declared local-runtime services.",
            recoverable=False,
        )
        out = render_to_stderr(envelope)
        self.assertIn(
            "ERROR [LOCAL_RUNTIME_PROFILE_UNKNOWN]: Profile 'local-bogus' has no"
            " declared local-runtime services.",
            out,
        )
        # No optional fields → no extra lines.
        self.assertNotIn("requested mode:", out)
        self.assertNotIn("blocked services:", out)
        self.assertNotIn("next action:", out)

    def test_tc10_empty_optional_fields_omit_their_lines(self) -> None:
        # TC-10: empty/missing optional fields print nothing for that section.
        envelope = local_runtime_error(
            "LOCAL_RUNTIME_MODE_UNSUPPORTED",
            "minimal envelope",
            recoverable=True,
            blocked_services=[],
            next_action="",
        )
        envelope["error"]["requested_mode"] = ""
        out = render_to_stderr(envelope)
        self.assertEqual(
            out.splitlines(),
            ["ERROR [LOCAL_RUNTIME_MODE_UNSUPPORTED]: minimal envelope"],
        )

    def test_tc10_missing_optional_fields_omit_their_lines(self) -> None:
        # TC-10 sibling: completely missing keys (not just empty) also omit.
        envelope = {
            "error": {
                "type": "LOCAL_RUNTIME_PROFILE_UNKNOWN",
                "detail": "no profile",
                "recoverable": False,
            }
        }
        out = render_to_stderr(envelope)
        self.assertEqual(
            out.splitlines(),
            ["ERROR [LOCAL_RUNTIME_PROFILE_UNKNOWN]: no profile"],
        )

    def test_renderer_falls_back_to_message_when_detail_missing(self) -> None:
        # Defensive: legacy envelopes that only carry `message` still render.
        envelope = {
            "error": {
                "type": "LOCAL_RUNTIME_START_BLOCKED",
                "message": "legacy message field",
                "recoverable": True,
            }
        }
        out = render_to_stderr(envelope)
        self.assertIn(
            "ERROR [LOCAL_RUNTIME_START_BLOCKED]: legacy message field", out
        )

    def test_renderer_handles_envelope_without_type_gracefully(self) -> None:
        # Envelopes built outside local_runtime_error() may lack `type`.
        envelope = {"error": {"detail": "headless envelope"}}
        out = render_to_stderr(envelope)
        self.assertEqual(out.splitlines(), ["ERROR: headless envelope"])

    def test_renderer_writes_only_to_stderr(self) -> None:
        # Stdout must remain clean across every code path the renderer takes.
        envelope = local_runtime_error(
            "LOCAL_RUNTIME_START_BLOCKED",
            "stdout isolation check",
            recoverable=True,
            blocked_services=["a", "b"],
            next_action="do something",
        )
        envelope["error"]["requested_mode"] = "reuse"
        self.assertEqual(render_to_stdout_capture(envelope), "")


class LocalRuntimeJsonRegressionTests(unittest.TestCase):
    """TC-7: JSON envelope serialization is unaffected by the renderer change."""

    def _emit(self, payload: dict) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_json(payload)
        return buf.getvalue()

    def test_emit_json_envelope_byte_stable(self) -> None:
        envelope = local_runtime_error(
            "LOCAL_RUNTIME_MODE_UNSUPPORTED",
            "byte-stability check",
            recoverable=True,
            blocked_services=["cfo"],
            next_action="re-run",
        )
        envelope["error"]["requested_mode"] = "prod"
        first = self._emit(envelope)
        second = self._emit(envelope)
        self.assertEqual(first, second)
        # The emitted JSON must round-trip and contain every envelope field.
        decoded = json.loads(first)
        err = decoded["error"]
        self.assertEqual(err["type"], "LOCAL_RUNTIME_MODE_UNSUPPORTED")
        self.assertEqual(err["detail"], "byte-stability check")
        self.assertEqual(err["recoverable"], True)
        self.assertEqual(err["blocked_services"], ["cfo"])
        self.assertEqual(err["next_action"], "re-run")
        self.assertEqual(err["requested_mode"], "prod")


if __name__ == "__main__":
    unittest.main()
